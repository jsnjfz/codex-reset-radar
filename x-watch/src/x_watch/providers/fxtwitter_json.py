"""FxTwitter JSON 适配器（主模式）。

接口与字段以 docs/source-validation.md 里的**真实响应**为准，不凭经验捏造字段。

一次完整请求的处理顺序（方案 §5.1）：
HTTP 状态 → Content-Type → JSON 解析 → 业务 code 与字段 → 标准化。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..httpclient import HttpClient
from ..normalize import ParseFailure, normalize_json_page
from ..util import now_utc
from .base import SOURCE_KEY_FXTWITTER, FetchPage, Provider

API_BASE = "https://api.fxtwitter.com/2/profile/%s/statuses"


class FxTwitterJsonProvider(Provider):
    name = "fxtwitter_json"
    source_key = SOURCE_KEY_FXTWITTER
    # 阶段 0 已实测：第二页返回 32 条首页没有的帖子、游标变化、时间向历史推进。
    supports_pagination = True

    def __init__(self, client: HttpClient, source_config: Dict[str, Any]) -> None:
        super().__init__(client, source_config)
        self.page_size = int(source_config.get("page_size", 50))
        self.include_replies = bool(source_config.get("include_replies", True))

    def probe_url(self, handle: str) -> str:
        return API_BASE % handle

    def _params(self, cursor: Optional[str]) -> List[Any]:
        # count 被上游校验（>100 返回 400）但不影响输出条数：实测不带 with_replies
        # 恒返回 20 条，带上返回 35~36 条，请求 5 或 50 都一样。
        # 所以 page_size 的唯一作用是保持在 1..100 内；
        # 绝不能用“返回条数 < page_size”推断历史结束。
        params = [("count", str(self.page_size))]
        if self.include_replies:
            params.append(("with_replies", "1"))
        if cursor:
            params.append(("cursor", cursor))  # 不透明字符串，原样回传
        return params

    def fetch_page(self, handle: str, cursor: Optional[str] = None) -> FetchPage:
        fetched_at = now_utc()
        result = self.client.get(
            self.probe_url(handle), self._params(cursor), accept="application/json"
        )

        def failed(kind: str, detail: str) -> FetchPage:
            return FetchPage(
                source=self.name,
                fetched_at=fetched_at,
                supports_pagination=self.supports_pagination,
                ok=False,
                error_kind=kind,
                error_detail=detail,
                retry_after_seconds=result.retry_after_seconds,
                http_status=result.status,
            )

        if result.error_kind is not None:
            # 404 在上游是 `{"code":404,"results":[]}`。必须记成“账号当前不可访问”，
            # 不能因为 results 是空数组就记成“没有新帖”（方案 §10）。
            return failed(result.error_kind, result.describe())

        if result.status != 200:
            return failed("http_other", result.describe())

        content_type = (result.content_type or "").split(";")[0].strip().lower()
        if content_type and content_type != "application/json":
            body_head = result.text()[:200].replace("\n", " ")
            return failed(
                "content_type",
                "期望 application/json，实际 %s；开头：%r" % (content_type, body_head),
            )

        try:
            payload = json.loads(result.text())
        except ValueError as exc:
            body_head = result.text()[:200].replace("\n", " ")
            # HTTP 200 但返回 HTML 会走到这里 —— 记失败，不记“没有新帖”
            return failed("parse", "JSON 解析失败：%s；开头：%r" % (exc, body_head))

        try:
            posts, warnings, failures, next_cursor = normalize_json_page(
                payload, handle, self.name
            )
        except ParseFailure as exc:
            return failed("structure", str(exc))

        raw_ref = _raw_ref(payload)
        returned = len(payload.get("results") or [])
        if returned == 0:
            # 合法空列表：只记录“源返回空”，不能据此断言账号没发帖或历史已覆盖
            warnings.append("source_returned_empty")

        return FetchPage(
            source=self.name,
            fetched_at=fetched_at,
            posts=posts,
            next_cursor=next_cursor,
            supports_pagination=self.supports_pagination,
            warnings=warnings,
            raw_response_ref=raw_ref,
            ok=True,
            parse_failures=failures,
            returned_count=returned,
            http_status=result.status,
        )


def _raw_ref(payload: Any) -> str:
    """给原始响应一个可定位的摘要标识。完整响应由 collector 落盘到 raw_dir。"""
    try:
        results = payload.get("results") or []
        ids = [str(item.get("id")) for item in results[:1] if isinstance(item, dict)]
        return "results=%d first_id=%s" % (len(results), ids[0] if ids else "none")
    except Exception:  # pragma: no cover - 只是诊断信息
        return "unavailable"
