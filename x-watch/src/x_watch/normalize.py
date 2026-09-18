"""把上游响应标准化成本项目自己的结构（方案 §5.3）。

两条硬规则来自阶段 0 的真实样本（见 docs/source-validation.md）：

1. **不看 `type` 字段判断类型** —— 实测 36 条全是 `status`。类型由
   `reposted_by` / `replying_to` / `quote` 的结构推导。
2. **作者 ≠ 监控账号** —— 监控 `bcherny` 时首页只有 17/36 条是他发的。
   `posts` 存真实作者，监控账号的关系存在 `account_posts`（方案 §7.2）。

关联类型（relation）取值：

| relation  | 判据 | 说明 |
|---|---|---|
| `self`    | `author.screen_name == 监控账号` | 原创 / 本人回复 / 本人引用 |
| `repost`  | `reposted_by.screen_name == 监控账号` | 明确证据，作者仍是原作者 |
| `context` | 出现在监控账号时间线里，但作者不是他、也没有转发标记 | 上游主动带出的对话上下文 |
| `quoted`  | 从已保存帖子的 `quote` 里取出的被引用原帖 | 非时间线条目 |
| `unknown` | 有转发标记但转发者不是监控账号，或结构无法判断 | 方案 §7.2：没有明确证据不得断言 |
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from .config import normalize_handle
from .util import parse_upstream_timestamp, safe_url

POST_ID_RE = re.compile(r"^\d{1,32}$")
STATUS_URL_RE = re.compile(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/(\d{1,32})")

RELATION_SELF = "self"
RELATION_REPOST = "repost"
RELATION_CONTEXT = "context"
RELATION_QUOTED = "quoted"
RELATION_UNKNOWN = "unknown"

TYPE_ORIGINAL = "original"
TYPE_REPLY = "reply"
TYPE_QUOTE = "quote"
TYPE_UNKNOWN = "unknown"

STATUS_NORMAL = "normal"
STATUS_UNKNOWN = "unknown"


class ParseFailure(Exception):
    """单个条目无法标准化。调用方保留其余有效条目并累计失败数（方案 §10）。"""


class NormalizedPost:
    __slots__ = (
        "post_id",
        "author_id",
        "author_handle",
        "url",
        "text",
        "published_at",
        "post_type",
        "reply_to_id",
        "quote_post_id",
        "media",
        "metrics",
        "content_hash",
        "content_status",
        "source",
        "raw",
        "relation",
        "reposted_by_handle",
        "is_timeline_entry",
        "warnings",
    )

    def __init__(self, **kwargs: Any) -> None:
        for name in self.__slots__:
            setattr(self, name, kwargs.get(name))
        if self.warnings is None:
            self.warnings = []
        if self.media is None:
            self.media = []

    @property
    def media_json(self) -> Optional[str]:
        if not self.media:
            return None
        return json.dumps(self.media, ensure_ascii=False, sort_keys=True)

    @property
    def metrics_json(self) -> Optional[str]:
        if not self.metrics:
            return None
        return json.dumps(self.metrics, ensure_ascii=False, sort_keys=True)

    @property
    def raw_json(self) -> Optional[str]:
        if self.raw is None:
            return None
        return json.dumps(self.raw, ensure_ascii=False, sort_keys=True)

    def __repr__(self) -> str:  # pragma: no cover - 仅调试
        return "NormalizedPost(%s, @%s, %s/%s)" % (
            self.post_id,
            self.author_handle,
            self.post_type,
            self.relation,
        )


# --- JSON 主模式 -------------------------------------------------------------


def normalize_json_post(
    item: Any,
    monitored_handle: str,
    source: str,
    is_timeline_entry: bool = True,
    relation_override: Optional[str] = None,
) -> Tuple[NormalizedPost, List[NormalizedPost]]:
    """标准化一条 JSON 帖子。

    返回 `(帖子, 附带取出的嵌套帖子)`。嵌套部分目前是被引用的原帖 —— 它有完整的
    id/作者/时间/正文，丢掉等于丢数据；但它不是监控账号时间线里的条目。
    """
    if not isinstance(item, dict):
        raise ParseFailure("条目不是对象：%r" % type(item).__name__)

    post_id = _coerce_post_id(item.get("id"))
    author = item.get("author")
    if not isinstance(author, dict):
        raise ParseFailure("帖子 %s 缺少 author 对象" % post_id)
    author_handle = normalize_handle(author.get("screen_name") or "")
    if not author_handle:
        raise ParseFailure("帖子 %s 的 author.screen_name 为空" % post_id)

    warnings: List[str] = []
    monitored = normalize_handle(monitored_handle)

    published_at = parse_upstream_timestamp(item.get("created_timestamp"), item.get("created_at"))
    content_status = STATUS_NORMAL
    if published_at is None:
        warnings.append("published_at_unparsable")
        content_status = STATUS_UNKNOWN

    # --- 类型推导：只看结构，不看 type 字段 ---
    replying_to = item.get("replying_to")
    reply_to_id: Optional[str] = None
    if isinstance(replying_to, dict):
        try:
            reply_to_id = _coerce_post_id(replying_to.get("status"))
        except ParseFailure:
            reply_to_id = None
            warnings.append("replying_to_without_status_id")
    elif replying_to not in (None, False, ""):
        warnings.append("replying_to_unexpected_shape")

    quote = item.get("quote")
    quote_post_id: Optional[str] = None
    nested: List[NormalizedPost] = []
    if isinstance(quote, dict):
        try:
            quote_post_id = _coerce_post_id(quote.get("id"))
        except ParseFailure:
            warnings.append("quote_without_id")
        else:
            if _is_tombstone(quote):
                # 实测形态：{"type":"tombstone","reason":"unavailable",
                #            "message":"This post is unavailable","author":null}
                # 只记下"引用了某帖但上游说它不可用"，绝不据此造一条空帖 ——
                # 那条帖子可能已经由别的路径正常入库了，不能把它覆盖成空。
                warnings.append("quote_unavailable:%s" % (quote.get("reason") or "unknown"))
            else:
                try:
                    quoted_post, _ = normalize_json_post(
                        quote,
                        monitored_handle,
                        source,
                        is_timeline_entry=False,
                        relation_override=RELATION_QUOTED,
                    )
                    nested.append(quoted_post)
                except ParseFailure as exc:
                    warnings.append("quote_unparsable: %s" % exc)
    elif quote not in (None, False, ""):
        warnings.append("quote_unexpected_shape")

    if reply_to_id:
        post_type = TYPE_REPLY
        if quote_post_id:
            # 实测存在“回复里带引用”的帖子。post_type 取回复，引用关系仍然记下来。
            warnings.append("reply_with_quote")
    elif quote_post_id:
        post_type = TYPE_QUOTE
    elif isinstance(replying_to, dict) or isinstance(quote, dict):
        post_type = TYPE_UNKNOWN  # 结构存在但没解析出 ID，不猜
    else:
        post_type = TYPE_ORIGINAL

    # --- 关联类型 ---
    reposted_by = item.get("reposted_by")
    reposted_by_handle: Optional[str] = None
    if relation_override is not None:
        relation = relation_override
    else:
        if isinstance(reposted_by, dict):
            reposted_by_handle = normalize_handle(reposted_by.get("screen_name") or "") or None
        elif isinstance(reposted_by, str) and reposted_by.strip():
            reposted_by_handle = normalize_handle(reposted_by)
        elif reposted_by not in (None, False, ""):
            warnings.append("reposted_by_unexpected_shape")

        if reposted_by_handle and reposted_by_handle == monitored:
            relation = RELATION_REPOST
        elif reposted_by_handle:
            # 有转发标记但转发者不是监控账号 —— 没有明确证据，不硬归类
            relation = RELATION_UNKNOWN
            warnings.append("reposted_by_other_account")
        elif author_handle == monitored:
            relation = RELATION_SELF
        else:
            relation = RELATION_CONTEXT

    # --- 正文 ---
    text = item.get("text")
    if text is None:
        raw_text = item.get("raw_text")
        if isinstance(raw_text, dict) and isinstance(raw_text.get("text"), str):
            text = raw_text["text"]
            warnings.append("text_from_raw_text")
    if text is None:
        text = ""
        warnings.append("text_missing")
    elif not isinstance(text, str):
        raise ParseFailure("帖子 %s 的 text 不是字符串" % post_id)
    # 纯媒体帖允许正文为空 —— 这是有效帖子，不算解析失败（方案 §14）

    url = safe_url(item.get("url")) or "https://x.com/%s/status/%s" % (author_handle, post_id)

    media = _extract_media(item.get("media"), warnings)
    metrics = _extract_metrics(item)

    post = NormalizedPost(
        post_id=post_id,
        author_id=_coerce_optional_id(author.get("id")),
        author_handle=author_handle,
        url=url,
        text=text,
        published_at=published_at,
        post_type=post_type,
        reply_to_id=reply_to_id,
        quote_post_id=quote_post_id,
        media=media,
        metrics=metrics,
        content_hash=None,
        content_status=content_status,
        source=source,
        raw=item,
        relation=relation,
        reposted_by_handle=reposted_by_handle,
        is_timeline_entry=is_timeline_entry,
        warnings=warnings,
    )
    post.content_hash = compute_content_hash(post)
    return post, nested


def _coerce_post_id(value: Any) -> str:
    """帖子 ID 一律当字符串处理（方案 §7.1）。JSON 数字会超出安全整数范围。"""
    if isinstance(value, bool) or value is None:
        raise ParseFailure("缺少帖子 ID")
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        raise ParseFailure("帖子 ID 类型异常：%r" % type(value).__name__)
    candidate = value.strip()
    if not POST_ID_RE.match(candidate):
        raise ParseFailure("帖子 ID 格式异常：%r" % candidate[:64])
    return candidate


def _coerce_optional_id(value: Any) -> Optional[str]:
    try:
        return _coerce_post_id(value)
    except ParseFailure:
        return None


def _is_tombstone(entry: Dict[str, Any]) -> bool:
    """上游对不可访问的帖子返回墓碑对象，而不是完整帖子。"""
    return entry.get("type") == "tombstone" or entry.get("reason") == "unavailable"


def _extract_media(media: Any, warnings: List[str]) -> List[Dict[str, Any]]:
    """只保存链接与描述，不下载媒体文件（方案 §2）。缺失字段不伪造。"""
    if media in (None, False, ""):
        return []
    if not isinstance(media, dict):
        warnings.append("media_unexpected_shape")
        return []
    # 实测：无媒体的帖子上游返回 `"media": {}`。这是"没有媒体"，不是结构异常。
    if not any(key in media for key in ("all", "photos", "videos")):
        return []
    items = media.get("all")
    if items is None:
        items = media.get("photos") or media.get("videos")
    if not isinstance(items, list):
        warnings.append("media_all_unexpected_shape")
        return []

    out: List[Dict[str, Any]] = []
    for entry in items:
        if not isinstance(entry, dict):
            warnings.append("media_item_unexpected_shape")
            continue
        url = safe_url(entry.get("url"))
        if url is None:
            warnings.append("media_item_without_safe_url")
            continue
        record: Dict[str, Any] = {"url": url}
        media_type = entry.get("type")
        record["type"] = media_type if isinstance(media_type, str) else "unknown"
        thumb = safe_url(entry.get("thumbnail_url"))
        if thumb:
            record["thumbnail_url"] = thumb
        for key in ("width", "height", "duration"):
            value = entry.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                record[key] = value
        for key in ("altText", "alt_text"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                record["alt_text"] = value.strip()
                break
        out.append(record)
    return out


_METRIC_KEYS = ("likes", "reposts", "replies", "quotes", "views", "bookmarks")


def _extract_metrics(item: Dict[str, Any]) -> Dict[str, Optional[int]]:
    """缺失用 null，不用 0 —— 0 和“上游没给”是两件事（方案 §7.1）。"""
    metrics: Dict[str, Optional[int]] = {}
    for key in _METRIC_KEYS:
        value = item.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            metrics[key] = value
        elif isinstance(value, float):
            metrics[key] = int(value)
        elif key in item:
            metrics[key] = None
    return metrics


def content_hash_from_parts(
    post_id: str,
    post_type: Optional[str],
    author_handle: Optional[str],
    text: Optional[str],
    reply_to_id: Optional[str],
    quote_post_id: Optional[str],
    media_urls: Any,
) -> str:
    """只覆盖稳定内容字段。互动数变化不应造成正文归档反复更新（方案 §7.1）。

    存储层合并字段后会用**合并结果**重算哈希，所以这里接受散装参数而不是对象。
    """
    parts = [
        post_id,
        post_type or "",
        author_handle or "",
        text or "",
        reply_to_id or "",
        quote_post_id or "",
        "|".join(sorted(url for url in media_urls if url)),
    ]
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def compute_content_hash(post: NormalizedPost) -> str:
    return content_hash_from_parts(
        post.post_id,
        post.post_type,
        post.author_handle,
        post.text,
        post.reply_to_id,
        post.quote_post_id,
        [m.get("url", "") for m in (post.media or [])],
    )


def normalize_json_page(
    payload: Any, monitored_handle: str, source: str
) -> Tuple[List[NormalizedPost], List[str], int, Optional[str]]:
    """标准化整页。

    返回 `(帖子列表, 警告列表, 解析失败条数, cursor.bottom)`。
    有效条目照常入库，失败只累计数量（方案 §10）。
    """
    warnings: List[str] = []
    if not isinstance(payload, dict):
        raise ParseFailure("响应顶层不是对象")

    code = payload.get("code")
    if code != 200:
        raise ParseFailure("上游业务 code=%r（不能记作空列表）" % code)

    results = payload.get("results")
    if not isinstance(results, list):
        raise ParseFailure("响应缺少 results 数组")

    posts: List[NormalizedPost] = []
    failures = 0
    seen_ids = set()
    for entry in results:
        try:
            post, nested = normalize_json_post(entry, monitored_handle, source)
        except ParseFailure as exc:
            failures += 1
            warnings.append("item_parse_failed: %s" % exc)
            continue
        for candidate in [post] + nested:
            if candidate.post_id in seen_ids:
                continue  # 同页重复（实测存在），只留一条
            seen_ids.add(candidate.post_id)
            posts.append(candidate)

    cursor = payload.get("cursor")
    next_cursor: Optional[str] = None
    if isinstance(cursor, dict):
        bottom = cursor.get("bottom")
        if isinstance(bottom, str) and bottom.strip():
            next_cursor = bottom.strip()  # 不透明字符串，不解码、不拼装
    elif cursor not in (None, False, ""):
        warnings.append("cursor_unexpected_shape")

    if failures:
        warnings.append("parse_failures=%d" % failures)
    return posts, warnings, failures, next_cursor


# --- RSS 兼容模式 -------------------------------------------------------------


def normalize_rss_item(
    item: Dict[str, Any], monitored_handle: str, source: str
) -> NormalizedPost:
    """RSS 条目字段远少于 JSON：没有转发标记、没有回复结构、没有互动数。"""
    link = safe_url(item.get("link")) or ""
    guid = item.get("guid") or ""
    match = STATUS_URL_RE.search(link) or STATUS_URL_RE.search(str(guid))
    if not match:
        raise ParseFailure("RSS 条目无法解析出帖子 ID：link=%r" % link[:120])
    author_handle = normalize_handle(match.group(1))
    post_id = match.group(2)

    warnings = ["rss_limited_fields"]
    published_at = parse_upstream_timestamp(None, item.get("pubDate"))
    content_status = STATUS_NORMAL
    if published_at is None:
        warnings.append("published_at_unparsable")
        content_status = STATUS_UNKNOWN

    text = (item.get("text") or "").strip()
    if not text:
        warnings.append("text_missing")

    monitored = normalize_handle(monitored_handle)
    # RSS 没有 reposted_by，无法区分转帖与上下文 —— 按方案 §7.2 记 unknown，不猜。
    if author_handle == monitored:
        relation = RELATION_SELF
    else:
        relation = RELATION_UNKNOWN
        warnings.append("rss_cannot_determine_relation")

    post = NormalizedPost(
        post_id=post_id,
        author_id=None,
        author_handle=author_handle,
        url=link or "https://x.com/%s/status/%s" % (author_handle, post_id),
        text=text,
        published_at=published_at,
        post_type=TYPE_UNKNOWN,  # RSS 判不出原创/回复/引用
        reply_to_id=None,
        quote_post_id=None,
        media=item.get("media") or [],
        metrics=None,
        content_hash=None,
        content_status=content_status,
        source=source,
        raw={"rss_item": item},
        relation=relation,
        reposted_by_handle=None,
        is_timeline_entry=True,
        warnings=warnings,
    )
    post.content_hash = compute_content_hash(post)
    return post


def usable_self_times(posts: List[NormalizedPost]) -> List[_dt.datetime]:
    """取出可以参与时间边界判断的帖子发布时间。

    只有**时间线里该账号自己发的、类型明确、时间可解析**的帖子算数。

    方案 §6.4.2：置顶帖、转帖、被引用原帖、未知类型不得单独参与时间边界判断。
    转帖的 `published_at` 是原帖发布时间，可能是几年前的，用它判边界会直接误判。
    """
    return [
        p.published_at
        for p in posts
        if p.relation == RELATION_SELF
        and p.is_timeline_entry
        and p.post_type != TYPE_UNKNOWN
        and p.content_status == STATUS_NORMAL
        and p.published_at is not None
    ]


def summarize_published_bounds(
    posts: List[NormalizedPost],
) -> Tuple[Optional[_dt.datetime], Optional[_dt.datetime], int]:
    usable = usable_self_times(posts)
    if not usable:
        return None, None, 0
    return min(usable), max(usable), len(usable)
