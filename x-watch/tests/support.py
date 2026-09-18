"""测试辅助：离线配置、假数据源、构造帖子。

所有测试都不联网。假数据源用来精确重现方案 §14 要求的故障场景 ——
真实接口无法按需触发 429、重复游标或第二页失败。
"""

from __future__ import annotations

import datetime as _dt
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from x_watch.config import load_config  # noqa: E402
from x_watch.httpclient import HttpClient  # noqa: E402
from x_watch.normalize import (  # noqa: E402
    RELATION_SELF,
    TYPE_ORIGINAL,
    NormalizedPost,
    compute_content_hash,
)
from x_watch.providers.base import FetchPage, Provider  # noqa: E402
from x_watch.util import UTC, now_utc  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

CONFIG_TEMPLATE = """\
[app]
database_path = "data/test.sqlite3"
output_dir = "output"
raw_dir = "data/raw"
log_dir = "logs"
display_timezone = "UTC"

[source]
provider = "fxtwitter_json"
auto_fallback = false
page_size = 50
include_replies = true
user_agent = "x-watch-test/0.1"

[collection]
interval_minutes = 60
max_pages_per_account = {max_pages}
bootstrap_hours = {bootstrap_hours}
bootstrap_max_pages = {bootstrap_max_pages}
overlap_hours = {overlap_hours}
request_gap_seconds = 0
connect_timeout_seconds = 10
read_timeout_seconds = 30
max_attempts_per_request = 1
max_total_requests_per_run = {max_requests}
max_run_seconds = 600

[export]
include_reposts = {include_reposts}
include_unknown = true
include_context = false
include_quoted = false
write_post_files = true
write_daily_index = true

[retention]
raw_days = 7
log_days = 30
"""

ACCOUNT_TEMPLATE = """
[[accounts]]
handle = "{handle}"
enabled = true
"""


def write_config(
    directory: str,
    handles: Sequence[str] = ("alice",),
    max_pages: int = 3,
    bootstrap_hours: int = 72,
    bootstrap_max_pages: int = 3,
    overlap_hours: int = 6,
    max_requests: int = 30,
    include_reposts: bool = False,
) -> str:
    body = CONFIG_TEMPLATE.format(
        max_pages=max_pages,
        bootstrap_hours=bootstrap_hours,
        bootstrap_max_pages=bootstrap_max_pages,
        overlap_hours=overlap_hours,
        max_requests=max_requests,
        include_reposts="true" if include_reposts else "false",
    )
    for handle in handles:
        body += ACCOUNT_TEMPLATE.format(handle=handle)
    path = os.path.join(directory, "config.toml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


def load_test_config(directory: str, **kwargs: Any):
    return load_config(write_config(directory, **kwargs))


def make_client(max_requests: int = 30, max_run_seconds: int = 600) -> HttpClient:
    """不联网的客户端：只用来记账与"睡眠"。"""
    return HttpClient(
        user_agent="x-watch-test/0.1",
        connect_timeout=1,
        read_timeout=1,
        max_attempts=1,
        max_total_requests=max_requests,
        max_run_seconds=max_run_seconds,
        sleeper=lambda seconds: None,
    )


def make_post(
    post_id: str,
    handle: str = "alice",
    author: Optional[str] = None,
    published_at: Optional[_dt.datetime] = None,
    text: str = "hello",
    post_type: str = TYPE_ORIGINAL,
    relation: str = RELATION_SELF,
    source: str = "fxtwitter_json",
    media: Optional[List[Dict[str, Any]]] = None,
    metrics: Optional[Dict[str, Any]] = None,
    reply_to_id: Optional[str] = None,
    quote_post_id: Optional[str] = None,
    content_status: str = "normal",
    is_timeline_entry: bool = True,
    warnings: Optional[List[str]] = None,
) -> NormalizedPost:
    author_handle = author or handle
    post = NormalizedPost(
        post_id=post_id,
        author_id="id-" + author_handle,
        author_handle=author_handle,
        url="https://x.com/%s/status/%s" % (author_handle, post_id),
        text=text,
        published_at=published_at or now_utc(),
        post_type=post_type,
        reply_to_id=reply_to_id,
        quote_post_id=quote_post_id,
        media=media or [],
        metrics=metrics,
        content_hash=None,
        content_status=content_status,
        source=source,
        raw={"id": post_id},
        relation=relation,
        reposted_by_handle=handle if relation == "repost" else None,
        is_timeline_entry=is_timeline_entry,
        warnings=warnings or [],
    )
    post.content_hash = compute_content_hash(post)
    return post


def make_page(
    posts: Sequence[NormalizedPost],
    next_cursor: Optional[str] = None,
    supports_pagination: bool = True,
    returned_count: Optional[int] = None,
    parse_failures: int = 0,
    warnings: Optional[List[str]] = None,
) -> FetchPage:
    return FetchPage(
        source="fxtwitter_json",
        fetched_at=now_utc(),
        posts=list(posts),
        next_cursor=next_cursor,
        supports_pagination=supports_pagination,
        warnings=warnings or [],
        ok=True,
        parse_failures=parse_failures,
        returned_count=returned_count if returned_count is not None else len(posts),
        http_status=200,
    )


def make_failed_page(
    error_kind: str,
    detail: str = "测试故障",
    retry_after_seconds: Optional[int] = None,
    supports_pagination: bool = True,
    http_status: Optional[int] = None,
) -> FetchPage:
    return FetchPage(
        source="fxtwitter_json",
        fetched_at=now_utc(),
        supports_pagination=supports_pagination,
        ok=False,
        error_kind=error_kind,
        error_detail=detail,
        retry_after_seconds=retry_after_seconds,
        http_status=http_status,
    )


class FakeProvider(Provider):
    """按脚本返回预设页面。

    `script` 是 `{handle: [FetchPage, ...]}`；也可以给 `default` 兜底。
    每次 fetch_page 都会占用一次请求预算，和真实适配器一致。
    """

    name = "fxtwitter_json"
    source_key = "fxtwitter"
    supports_pagination = True

    def __init__(
        self,
        client: HttpClient,
        script: Dict[str, List[FetchPage]],
        supports_pagination: bool = True,
    ) -> None:
        super().__init__(client, {"page_size": 50, "include_replies": True})
        self.script = {key: list(value) for key, value in script.items()}
        self.supports_pagination = supports_pagination
        self.calls: List[Dict[str, Any]] = []

    def probe_url(self, handle: str) -> str:
        return "https://example.invalid/%s" % handle

    def fetch_page(self, handle: str, cursor: Optional[str] = None) -> FetchPage:
        self.client.check_budget(1)
        self.client.requests_used += 1
        self.calls.append({"handle": handle, "cursor": cursor})
        queue = self.script.get(handle)
        if queue is None:
            queue = self.script.get("default", [])
        if not queue:
            return make_page([], next_cursor=None)
        return queue.pop(0)


def hours_ago(hours: float) -> _dt.datetime:
    return now_utc() - _dt.timedelta(hours=hours)


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> _dt.datetime:
    return _dt.datetime(year, month, day, hour, minute, tzinfo=UTC)
