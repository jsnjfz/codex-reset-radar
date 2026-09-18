"""HTTP GET 客户端（仅标准库）。

职责：
- 精确区分故障类型，供方案 §10 的错误处理表使用（DNS / 连接 / 超时 / 429 / 5xx / 401403 ...）。
- 统计请求预算：**分页与重试都计入** `max_total_requests_per_run`（方案 §8）。
- 遵守运行时长上限，不在进程里挂着睡几小时（方案 §10）。

代理：通过 `urllib.request.getproxies()` 读取 `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY`
环境变量，不在代码里硬编码代理地址（方案 §11.3）。
"""

from __future__ import annotations

import email.utils
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Optional, Sequence, Tuple

# 值得重试的临时故障
TRANSIENT_KINDS = frozenset({"dns", "connect", "timeout", "read", "http_5xx"})
# 必须立刻停止、不得换格式或绕过的故障（方案 §10）
HARD_STOP_KINDS = frozenset({"http_401_403", "blocked_content"})


class BudgetExhausted(Exception):
    """本轮请求预算或运行时长已用尽。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class HttpResult:
    __slots__ = (
        "url",
        "status",
        "headers",
        "body",
        "content_type",
        "error_kind",
        "error_detail",
        "retry_after_seconds",
        "attempts",
        "elapsed_seconds",
    )

    def __init__(
        self,
        url: str,
        status: Optional[int] = None,
        headers: Optional[Dict[str, str]] = None,
        body: bytes = b"",
        content_type: str = "",
        error_kind: Optional[str] = None,
        error_detail: str = "",
        retry_after_seconds: Optional[int] = None,
        attempts: int = 0,
        elapsed_seconds: float = 0.0,
    ) -> None:
        self.url = url
        self.status = status
        self.headers = headers or {}
        self.body = body
        self.content_type = content_type
        self.error_kind = error_kind
        self.error_detail = error_detail
        self.retry_after_seconds = retry_after_seconds
        self.attempts = attempts
        self.elapsed_seconds = elapsed_seconds

    @property
    def ok(self) -> bool:
        return self.error_kind is None and self.status == 200

    def text(self) -> str:
        charset = "utf-8"
        if "charset=" in self.content_type:
            charset = self.content_type.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    def describe(self) -> str:
        if self.error_kind:
            return "%s: %s" % (self.error_kind, self.error_detail)
        return "HTTP %s (%s, %d 字节)" % (self.status, self.content_type or "无类型", len(self.body))


class HttpClient:
    def __init__(
        self,
        user_agent: str,
        connect_timeout: int,
        read_timeout: int,
        max_attempts: int = 2,
        retry_wait_seconds: int = 30,
        max_total_requests: int = 30,
        max_run_seconds: int = 600,
        max_body_bytes: int = 16 * 1024 * 1024,
        sleeper=time.sleep,
    ) -> None:
        self.user_agent = user_agent
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.max_attempts = max_attempts
        self.retry_wait_seconds = retry_wait_seconds
        self.max_total_requests = max_total_requests
        self.max_run_seconds = max_run_seconds
        self.max_body_bytes = max_body_bytes
        self._sleep = sleeper
        self.requests_used = 0
        self._started = time.monotonic()
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(),  # 无参数即读取环境变量
            _NoRedirectHandler(),
        )

    # --- 预算 ---------------------------------------------------------------
    def seconds_left(self) -> float:
        return self.max_run_seconds - (time.monotonic() - self._started)

    def requests_left(self) -> int:
        return self.max_total_requests - self.requests_used

    def check_budget(self, need: int = 1) -> None:
        if self.requests_left() < need:
            raise BudgetExhausted(
                "请求预算已用尽（%d/%d）" % (self.requests_used, self.max_total_requests)
            )
        if self.seconds_left() <= 0:
            raise BudgetExhausted("运行时长已达上限 %d 秒" % self.max_run_seconds)

    def sleep_within_budget(self, seconds: float) -> bool:
        """按预算睡眠。返回 False 表示时间不够，调用方应放弃而不是硬等。"""
        if seconds <= 0:
            return True
        if self.seconds_left() - seconds <= 1:
            return False
        self._sleep(seconds)
        return True

    # --- 请求 ---------------------------------------------------------------
    def get(
        self,
        url: str,
        params: Optional[Sequence[Tuple[str, str]]] = None,
        accept: str = "*/*",
    ) -> HttpResult:
        full_url = url
        if params:
            query = urllib.parse.urlencode([(k, v) for k, v in params if v is not None])
            if query:
                full_url = "%s?%s" % (url, query)

        started = time.monotonic()
        attempts = 0
        last = HttpResult(full_url, error_kind="unknown", error_detail="未执行任何请求")

        while attempts < self.max_attempts:
            self.check_budget(1)
            attempts += 1
            self.requests_used += 1
            last = self._attempt(full_url, accept)
            last.attempts = attempts
            last.elapsed_seconds = time.monotonic() - started

            if last.ok or last.error_kind not in TRANSIENT_KINDS:
                return last
            if attempts >= self.max_attempts:
                break
            if not self.sleep_within_budget(self.retry_wait_seconds):
                last.error_detail += "（剩余运行时长不足，放弃重试）"
                break

        last.attempts = attempts
        last.elapsed_seconds = time.monotonic() - started
        return last

    def _attempt(self, full_url: str, accept: str) -> HttpResult:
        parsed = urllib.parse.urlsplit(full_url)
        if parsed.scheme != "https":
            return HttpResult(
                full_url, error_kind="blocked_content", error_detail="只允许 https 请求"
            )

        # 先做一次带 connect_timeout 的 TCP 预连接，让“连不上”和“读得慢”能区分开。
        # urllib 的 timeout 参数无法分别设置连接与读取超时。
        target = self._connect_target(parsed)
        if target is not None:
            probe_error = self._probe_tcp(target)
            if probe_error is not None:
                kind, detail = probe_error
                return HttpResult(full_url, error_kind=kind, error_detail=detail)

        request = urllib.request.Request(
            full_url,
            method="GET",
            headers={
                "User-Agent": self.user_agent,
                "Accept": accept,
                "Accept-Encoding": "identity",  # 不解压，省一层不确定性
                "Connection": "close",
            },
        )
        try:
            with self._opener.open(request, timeout=self.read_timeout) as response:
                body = response.read(self.max_body_bytes + 1)
                if len(body) > self.max_body_bytes:
                    return HttpResult(
                        full_url,
                        error_kind="blocked_content",
                        error_detail="响应超过 %d 字节上限" % self.max_body_bytes,
                    )
                headers = {k.lower(): v for k, v in response.headers.items()}
                return HttpResult(
                    full_url,
                    status=response.status,
                    headers=headers,
                    body=body,
                    content_type=headers.get("content-type", ""),
                )
        except urllib.error.HTTPError as exc:
            headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
            try:
                body = exc.read(self.max_body_bytes)
            except Exception:
                body = b""
            return HttpResult(
                full_url,
                status=exc.code,
                headers=headers,
                body=body,
                content_type=headers.get("content-type", ""),
                error_kind=_classify_status(exc.code),
                error_detail="HTTP %d %s" % (exc.code, exc.reason),
                retry_after_seconds=parse_retry_after(headers.get("retry-after")),
            )
        except socket.timeout as exc:
            return HttpResult(full_url, error_kind="read", error_detail="读取超时：%s" % exc)
        except ssl.SSLError as exc:
            return HttpResult(full_url, error_kind="tls", error_detail="TLS 错误：%s" % exc)
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, socket.gaierror):
                return HttpResult(full_url, error_kind="dns", error_detail="DNS 解析失败：%s" % reason)
            if isinstance(reason, socket.timeout):
                return HttpResult(full_url, error_kind="timeout", error_detail="连接超时：%s" % reason)
            return HttpResult(full_url, error_kind="connect", error_detail="连接失败：%s" % reason)
        except OSError as exc:
            return HttpResult(full_url, error_kind="connect", error_detail="网络错误：%s" % exc)

    def _connect_target(self, parsed: urllib.parse.SplitResult) -> Optional[Tuple[str, int]]:
        """预连接目标：有代理时连代理，否则连站点。NO_PROXY 命中时也直连站点。"""
        proxies = urllib.request.getproxies()
        proxy = proxies.get("https")
        if proxy and not urllib.request.proxy_bypass(parsed.hostname or ""):
            proxy_parts = urllib.parse.urlsplit(
                proxy if "//" in proxy else "//" + proxy, scheme="http"
            )
            if proxy_parts.hostname:
                return (proxy_parts.hostname, proxy_parts.port or 8080)
            return None  # 代理地址解析不了就别预检，交给 urllib 报错
        if not parsed.hostname:
            return None
        return (parsed.hostname, parsed.port or 443)

    def _probe_tcp(self, target: Tuple[str, int]) -> Optional[Tuple[str, str]]:
        try:
            sock = socket.create_connection(target, timeout=self.connect_timeout)
        except socket.gaierror as exc:
            return ("dns", "DNS 解析失败 %s:%d：%s" % (target[0], target[1], exc))
        except socket.timeout:
            return (
                "timeout",
                "连接 %s:%d 超过 %d 秒" % (target[0], target[1], self.connect_timeout),
            )
        except OSError as exc:
            return ("connect", "无法连接 %s:%d：%s" % (target[0], target[1], exc))
        sock.close()
        return None


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """不跟随跳转。被重定向到登录页/验证码页时应当明确失败，而不是抓回一堆 HTML。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _classify_status(code: int) -> str:
    if code == 429:
        return "http_429"
    if code in (401, 403):
        return "http_401_403"
    if code == 404:
        return "http_404"
    if 500 <= code <= 599:
        return "http_5xx"
    return "http_other"


def parse_retry_after(value: Optional[str]) -> Optional[int]:
    """支持秒数与 HTTP 日期两种写法（方案 §10）。"""
    if not value:
        return None
    text = value.strip()
    if text.isdigit():
        seconds = int(text)
        return max(0, min(seconds, 24 * 3600))
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    import datetime as _dt

    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    delta = (when - _dt.datetime.now(tz=_dt.timezone.utc)).total_seconds()
    if delta <= 0:
        return 0
    return int(min(delta, 24 * 3600))
