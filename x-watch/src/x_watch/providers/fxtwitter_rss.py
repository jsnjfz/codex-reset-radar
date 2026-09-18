"""FxTwitter RSS 适配器（兼容模式）。

能力明确低于 JSON —— 阶段 0 实测：无 `reposted_by`、无 `replying_to` 结构、
无互动数、无游标。因此 `supports_pagination = False`，不假装能翻历史。

方案 §5.2：RSS 与 JSON 是**同一个上游**，共用退避键。换格式不是高可用手段，
被限流时不得靠切格式继续冲击同一服务。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from html import unescape
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional

from ..httpclient import HttpClient
from ..normalize import ParseFailure, normalize_rss_item
from ..util import now_utc, safe_url
from .base import SOURCE_KEY_FXTWITTER, FetchPage, Provider

FEED_BASE = "https://fxtwitter.com/%s/feed.xml"
_MEDIA_NS = "{http://search.yahoo.com/mrss/}"
# 标准库 ElementTree 会展开内部实体（billion laughs）。这里直接拒绝带 DTD/实体
# 声明的文档 —— 正常的 RSS 不需要它们。不引入 defusedxml 依赖。
_DANGEROUS_XML = re.compile(rb"<!(?:DOCTYPE|ENTITY)", re.IGNORECASE)


class FxTwitterRssProvider(Provider):
    name = "fxtwitter_rss"
    source_key = SOURCE_KEY_FXTWITTER
    supports_pagination = False

    def __init__(self, client: HttpClient, source_config: Dict[str, Any]) -> None:
        super().__init__(client, source_config)
        self.page_size = int(source_config.get("page_size", 50))
        self.include_replies = bool(source_config.get("include_replies", True))

    def probe_url(self, handle: str) -> str:
        return FEED_BASE % handle

    def fetch_page(self, handle: str, cursor: Optional[str] = None) -> FetchPage:
        fetched_at = now_utc()
        warnings: List[str] = ["provider_rss_limited"]
        if cursor:
            # 不实现假分页：明确拒绝，而不是默默只抓最新一页（方案 §5.2 / §11.1）
            return FetchPage(
                source=self.name,
                fetched_at=fetched_at,
                supports_pagination=False,
                ok=False,
                error_kind="unsupported",
                error_detail="RSS 适配器不支持游标分页",
                warnings=warnings,
            )

        params = [("count", str(self.page_size))]
        if self.include_replies:
            params.append(("with_replies", "1"))
        result = self.client.get(self.probe_url(handle), params, accept="application/rss+xml")

        def failed(kind: str, detail: str) -> FetchPage:
            return FetchPage(
                source=self.name,
                fetched_at=fetched_at,
                supports_pagination=False,
                ok=False,
                error_kind=kind,
                error_detail=detail,
                retry_after_seconds=result.retry_after_seconds,
                http_status=result.status,
                warnings=warnings,
            )

        if result.error_kind is not None:
            return failed(result.error_kind, result.describe())
        if result.status != 200:
            return failed("http_other", result.describe())

        content_type = (result.content_type or "").split(";")[0].strip().lower()
        if content_type and "xml" not in content_type:
            return failed(
                "content_type",
                "期望 XML，实际 %s；开头：%r" % (content_type, result.text()[:200]),
            )
        if _DANGEROUS_XML.search(result.body):
            return failed("blocked_content", "RSS 中出现 DTD/实体声明，拒绝解析")

        try:
            root = ET.fromstring(result.body)
        except ET.ParseError as exc:
            return failed("parse", "XML 解析失败：%s" % exc)

        channel = root.find("channel")
        if channel is None:
            return failed("structure", "RSS 缺少 channel 节点")

        raw_items = channel.findall("item")
        posts = []
        failures = 0
        seen = set()
        for element in raw_items:
            try:
                item = _read_item(element)
                post = normalize_rss_item(item, handle, self.name)
            except ParseFailure as exc:
                failures += 1
                warnings.append("item_parse_failed: %s" % exc)
                continue
            if post.post_id in seen:
                continue
            seen.add(post.post_id)
            posts.append(post)

        if not raw_items:
            warnings.append("source_returned_empty")
        if failures:
            warnings.append("parse_failures=%d" % failures)

        return FetchPage(
            source=self.name,
            fetched_at=fetched_at,
            posts=posts,
            next_cursor=None,
            supports_pagination=False,
            warnings=warnings,
            raw_response_ref="items=%d" % len(raw_items),
            ok=True,
            parse_failures=failures,
            returned_count=len(raw_items),
            http_status=result.status,
        )


def _read_item(element: ET.Element) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "link": _text_of(element.find("link")),
        "guid": _text_of(element.find("guid")),
        "pubDate": _text_of(element.find("pubDate")),
        "title": _text_of(element.find("title")),
    }
    description = _text_of(element.find("description"))
    item["text"] = html_to_text(description) if description else (item["title"] or "")

    media: List[Dict[str, Any]] = []
    for enclosure in element.findall("enclosure"):
        url = safe_url(enclosure.get("url"))
        if url:
            media.append({"url": url, "type": enclosure.get("type") or "unknown"})
    for thumb in element.findall(_MEDIA_NS + "thumbnail"):
        url = safe_url(thumb.get("url"))
        if url:
            media.append({"url": url, "type": "thumbnail"})
    item["media"] = media
    return item


def _text_of(element: Optional[ET.Element]) -> str:
    if element is None or element.text is None:
        return ""
    return element.text.strip()


class _TextExtractor(HTMLParser):
    """把 RSS description 的 HTML 安全地提取成纯文本。

    只收集文本节点，`<br>`/`<p>` 转换成换行。标签本身一律丢弃 —— 不执行、不转发
    HTML，也不保留属性（方案 §3.1 / §9.1）。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style"):
            self._skip_depth += 1
        elif tag in ("br", "p", "div", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in ("p", "div", "li"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(unescape(html) if "&lt;" in html else html)
        parser.close()
    except Exception:
        # 解析器出错也不能把原始 HTML 当正文吐出去
        return re.sub(r"<[^>]*>", " ", html).strip()
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()
