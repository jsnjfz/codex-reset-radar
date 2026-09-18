"""时间、文件与文本工具。

时间约定（方案 §2）：数据库一律存 UTC ISO-8601（带 `+00:00`），只有展示层才转本地时区。
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import tempfile
from typing import Optional

UTC = _dt.timezone.utc

# 上游 created_at 形如 "Wed Sep 16 17:26:17 +0000 2026"
_TWITTER_TS_RE = re.compile(
    r"^[A-Za-z]{3} ([A-Za-z]{3}) (\d{1,2}) (\d{2}):(\d{2}):(\d{2}) ([+-]\d{4}) (\d{4})$"
)
_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def now_utc() -> _dt.datetime:
    return _dt.datetime.now(tz=UTC)


def to_iso(moment: Optional[_dt.datetime]) -> Optional[str]:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def parse_iso(text: Optional[str]) -> Optional[_dt.datetime]:
    """解析本项目写出的 ISO 时间；顺带容忍末尾 `Z`（CLI 参数常这么写）。"""
    if not text:
        return None
    candidate = text.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = _dt.datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None  # 明确要求带时区，不替用户猜
    return parsed.astimezone(UTC)


def parse_upstream_timestamp(
    created_timestamp: object, created_at: object
) -> Optional[_dt.datetime]:
    """优先用数值时间戳，退回文本格式；都不行返回 None（由调用方告警，不伪造时间）。"""
    if isinstance(created_timestamp, (int, float)) and not isinstance(created_timestamp, bool):
        # 合理区间：2006（X 上线）到 2100，防止毫秒/秒单位混用被静默接受
        if 1_100_000_000 <= float(created_timestamp) <= 4_100_000_000:
            return _dt.datetime.fromtimestamp(float(created_timestamp), tz=UTC)
    if isinstance(created_at, str):
        match = _TWITTER_TS_RE.match(created_at.strip())
        if match:
            mon, day, hour, minute, sec, offset, year = match.groups()
            month = _MONTHS.get(mon)
            if month:
                sign = 1 if offset[0] == "+" else -1
                delta = _dt.timedelta(
                    hours=int(offset[1:3]) * sign, minutes=int(offset[3:5]) * sign
                )
                try:
                    naive = _dt.datetime(
                        int(year), month, int(day), int(hour), int(minute), int(sec)
                    )
                except ValueError:
                    return None
                return (naive - delta).replace(tzinfo=UTC)
        # 也接受标准 RFC/ISO 写法（RSS 走这条）
        iso = parse_iso(created_at)
        if iso is not None:
            return iso
        rfc = parse_rfc2822(created_at)
        if rfc is not None:
            return rfc
    return None


def parse_rfc2822(text: str) -> Optional[_dt.datetime]:
    """RSS pubDate，如 "Thu, 17 Sep 2026 04:35:18 GMT"。"""
    from email.utils import parsedate_to_datetime

    try:
        parsed = parsedate_to_datetime(text.strip())
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def display_tzinfo(name: str) -> _dt.tzinfo:
    """把配置里的 display_timezone 解析成 tzinfo。无法识别时回落本地时区并不静默假装成功。"""
    if name.strip().lower() == "local":
        return _dt.datetime.now().astimezone().tzinfo or UTC
    if name.strip().upper() == "UTC":
        return UTC
    try:
        from zoneinfo import ZoneInfo  # Python 3.9+

        return ZoneInfo(name)
    except Exception as exc:  # 时区库缺失或名称错误
        raise ValueError("无法识别的 display_timezone %r：%s" % (name, exc)) from exc


def local_date_key(moment: _dt.datetime, tz: _dt.tzinfo) -> str:
    """按展示时区算出 YYYY-MM-DD，用于每日索引归属。"""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(tz).strftime("%Y-%m-%d")


def format_display(moment: Optional[_dt.datetime], tz: _dt.tzinfo) -> str:
    if moment is None:
        return "（时间未知）"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z")


# --- 文件 -------------------------------------------------------------------

_SAFE_ID_RE = re.compile(r"^[0-9A-Za-z_-]{1,64}$")


def is_safe_filename_token(token: str) -> bool:
    """帖子 ID 用作文件名前必须通过这里，避免 `../` 之类进入路径（方案 §9.1）。"""
    return bool(_SAFE_ID_RE.match(token or ""))


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def atomic_write_text(path: str, content: str) -> None:
    """临时文件 + 原子替换。避免重跑时读到半截文件（方案 §9.2）。"""
    ensure_dir(os.path.dirname(os.path.abspath(path)))
    directory = os.path.dirname(os.path.abspath(path))
    handle, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# --- 文本安全 ---------------------------------------------------------------

_ALLOWED_SCHEMES = ("http://", "https://")


def safe_url(raw: object) -> Optional[str]:
    """只接受 http/https，且不含换行 —— 防止外部文本把链接拼成别的东西（方案 §9.1）。"""
    if not isinstance(raw, str):
        return None
    url = raw.strip()
    if not url or any(ch in url for ch in "\r\n\t <>\"'()[]"):
        return None
    lowered = url.lower()
    if not lowered.startswith(_ALLOWED_SCHEMES):
        return None
    return url


_MD_ESCAPE_RE = re.compile(r"([\\`*_{}\[\]()#+\-.!|>~])")


def escape_markdown_inline(text: str) -> str:
    """用于表格单元格、标题等行内位置。正文本身走引用块，不做这种转义。"""
    return _MD_ESCAPE_RE.sub(r"\\\1", text or "").replace("\n", " ")


def as_blockquote(text: str) -> str:
    """把正文放进引用块。逐行加 `>`，空行也加，避免正文里的标记破坏文件结构。"""
    if not text:
        return "> （无文字内容）"
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join("> " + line if line else ">" for line in lines)


def truncate(text: str, limit: int) -> str:
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: max(0, limit - 1)] + "…"
