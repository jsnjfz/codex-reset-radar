"""适配器统一返回结构（方案 §5.3）。"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Optional

from ..httpclient import HttpClient
from ..normalize import NormalizedPost

# 上游源的退避键。JSON 与 RSS 是**同一个上游**，共用一个键 ——
# 被 429 之后换格式继续请求同一服务是错的（方案 §5.2）。
SOURCE_KEY_FXTWITTER = "fxtwitter"


class FetchPage:
    __slots__ = (
        "source",
        "fetched_at",
        "posts",
        "next_cursor",
        "supports_pagination",
        "warnings",
        "raw_response_ref",
        "ok",
        "error_kind",
        "error_detail",
        "retry_after_seconds",
        "parse_failures",
        "returned_count",
        "http_status",
    )

    def __init__(
        self,
        source: str,
        fetched_at: _dt.datetime,
        posts: Optional[List[NormalizedPost]] = None,
        next_cursor: Optional[str] = None,
        supports_pagination: bool = False,
        warnings: Optional[List[str]] = None,
        raw_response_ref: Optional[str] = None,
        ok: bool = True,
        error_kind: Optional[str] = None,
        error_detail: str = "",
        retry_after_seconds: Optional[int] = None,
        parse_failures: int = 0,
        returned_count: int = 0,
        http_status: Optional[int] = None,
    ) -> None:
        self.source = source
        self.fetched_at = fetched_at
        self.posts = posts or []
        self.next_cursor = next_cursor
        self.supports_pagination = supports_pagination
        self.warnings = warnings or []
        self.raw_response_ref = raw_response_ref
        self.ok = ok
        self.error_kind = error_kind
        self.error_detail = error_detail
        self.retry_after_seconds = retry_after_seconds
        self.parse_failures = parse_failures
        self.returned_count = returned_count
        self.http_status = http_status

    def __repr__(self) -> str:  # pragma: no cover - 仅调试
        if not self.ok:
            return "FetchPage(FAILED %s: %s)" % (self.error_kind, self.error_detail)
        return "FetchPage(%s, %d posts, cursor=%s)" % (
            self.source,
            len(self.posts),
            "yes" if self.next_cursor else "no",
        )


class Provider:
    """适配器接口。"""

    name = "base"
    source_key = SOURCE_KEY_FXTWITTER
    supports_pagination = False

    def __init__(self, client: HttpClient, source_config: Dict[str, Any]) -> None:
        self.client = client
        self.config = source_config

    def fetch_page(self, handle: str, cursor: Optional[str] = None) -> FetchPage:
        raise NotImplementedError

    def probe_url(self, handle: str) -> str:
        """`doctor` 用来展示实际会请求什么地址。"""
        raise NotImplementedError
