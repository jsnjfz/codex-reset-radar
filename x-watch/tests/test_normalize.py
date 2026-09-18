"""标准化测试。关键断言都基于 tests/fixtures 里的**真实响应**。"""

from __future__ import annotations

import json
import os
import unittest

from support import FIXTURES  # noqa: E402

from x_watch.normalize import (  # noqa: E402
    RELATION_CONTEXT,
    RELATION_QUOTED,
    RELATION_REPOST,
    RELATION_SELF,
    TYPE_ORIGINAL,
    TYPE_QUOTE,
    TYPE_REPLY,
    TYPE_UNKNOWN,
    ParseFailure,
    compute_content_hash,
    normalize_json_page,
    normalize_json_post,
    normalize_rss_item,
    usable_self_times,
)
from x_watch.providers.fxtwitter_rss import html_to_text  # noqa: E402
from x_watch.util import parse_upstream_timestamp  # noqa: E402


def load_fixture(name: str):
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


class RealResponseTests(unittest.TestCase):
    """用阶段 0 抓下来的真实响应做回归。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = load_fixture("probe_page1.raw.json")

    def test_page_normalizes_without_failures(self) -> None:
        posts, warnings, failures, cursor = normalize_json_page(
            self.payload, "bcherny", "fxtwitter_json"
        )
        self.assertEqual(failures, 0)
        # 36 条时间线条目 + 4 条嵌套被引用原帖
        self.assertEqual(len(posts), 40)
        self.assertTrue(cursor)

    def test_relation_counts_match_manual_audit(self) -> None:
        """人工核对结论见 docs/source-validation.md §C。"""
        posts, _, _, _ = normalize_json_page(self.payload, "bcherny", "fxtwitter_json")
        counts = {}
        for post in posts:
            counts[post.relation] = counts.get(post.relation, 0) + 1
        self.assertEqual(counts[RELATION_SELF], 17)
        self.assertEqual(counts[RELATION_REPOST], 4)
        self.assertEqual(counts[RELATION_CONTEXT], 15)
        self.assertEqual(counts[RELATION_QUOTED], 4)

    def test_type_field_is_never_used_for_classification(self) -> None:
        """真实数据里 36 条的 type 全是 status，但类型必须分化出来。"""
        raw_types = {item["type"] for item in self.payload["results"]}
        self.assertEqual(raw_types, {"status"})
        posts, _, _, _ = normalize_json_page(self.payload, "bcherny", "fxtwitter_json")
        derived = {post.post_type for post in posts}
        self.assertIn(TYPE_REPLY, derived)
        self.assertIn(TYPE_QUOTE, derived)
        self.assertIn(TYPE_ORIGINAL, derived)
        self.assertNotIn("status", derived)

    def test_repost_keeps_original_author(self) -> None:
        """转帖不能归错作者（方案 §7.2）。"""
        posts, _, _, _ = normalize_json_page(self.payload, "bcherny", "fxtwitter_json")
        reposts = [p for p in posts if p.relation == RELATION_REPOST]
        self.assertEqual(len(reposts), 4)
        for post in reposts:
            self.assertNotEqual(post.author_handle, "bcherny")
            self.assertEqual(post.reposted_by_handle, "bcherny")

    def test_timeline_is_not_reverse_chronological(self) -> None:
        """实测上游按对话聚合返回，停止条件不能依赖顺序（docs §B）。"""
        stamps = [item["created_timestamp"] for item in self.payload["results"]]
        self.assertNotEqual(stamps, sorted(stamps, reverse=True))

    def test_self_times_exclude_reposts_and_context(self) -> None:
        posts, _, _, _ = normalize_json_page(self.payload, "bcherny", "fxtwitter_json")
        times = usable_self_times(posts)
        self.assertEqual(len(times), 17)

    def test_empty_media_object_is_not_a_warning(self) -> None:
        """实测无媒体的帖子返回 `"media": {}`，这是没有媒体，不是结构异常。"""
        posts, _, _, _ = normalize_json_page(self.payload, "bcherny", "fxtwitter_json")
        for post in posts:
            self.assertNotIn("media_all_unexpected_shape", post.warnings)
            self.assertNotIn("media_unexpected_shape", post.warnings)

    def test_media_links_are_captured(self) -> None:
        posts, _, _, _ = normalize_json_page(self.payload, "bcherny", "fxtwitter_json")
        with_media = [p for p in posts if p.media]
        self.assertTrue(with_media)
        for item in with_media[0].media:
            self.assertTrue(item["url"].startswith("https://"))
            self.assertIn("type", item)

    def test_second_page_brings_new_posts(self) -> None:
        """分页有效性：真实第二页带来首页没有的帖子。"""
        page2 = load_fixture("probe_page2.raw.json")
        first, _, _, cursor1 = normalize_json_page(self.payload, "bcherny", "fxtwitter_json")
        second, _, _, cursor2 = normalize_json_page(page2, "bcherny", "fxtwitter_json")
        ids1 = {p.post_id for p in first}
        ids2 = {p.post_id for p in second}
        self.assertGreater(len(ids2 - ids1), 20)
        self.assertNotEqual(cursor1, cursor2)


class ClassificationTests(unittest.TestCase):
    def _post(self, **fields):
        item = {
            "id": "1",
            "author": {"screen_name": "alice", "id": "9"},
            "text": "hi",
            "created_timestamp": 1789579577,
            "url": "https://x.com/alice/status/1",
        }
        item.update(fields)
        return normalize_json_post(item, "alice", "fxtwitter_json")

    def test_reply_detection(self) -> None:
        post, _ = self._post(replying_to={"screen_name": "bob", "status": "77"})
        self.assertEqual(post.post_type, TYPE_REPLY)
        self.assertEqual(post.reply_to_id, "77")

    def test_quote_detection_and_nested_extraction(self) -> None:
        post, nested = self._post(
            quote={
                "id": "55",
                "author": {"screen_name": "carol", "id": "3"},
                "text": "quoted body",
                "created_timestamp": 1789579000,
                "url": "https://x.com/carol/status/55",
            }
        )
        self.assertEqual(post.post_type, TYPE_QUOTE)
        self.assertEqual(post.quote_post_id, "55")
        self.assertEqual(len(nested), 1)
        self.assertEqual(nested[0].relation, RELATION_QUOTED)
        self.assertFalse(nested[0].is_timeline_entry)
        self.assertEqual(nested[0].author_handle, "carol")

    def test_reply_with_quote_keeps_both_facts(self) -> None:
        post, _ = self._post(
            replying_to={"screen_name": "bob", "status": "77"},
            quote={
                "id": "55",
                "author": {"screen_name": "carol"},
                "text": "q",
                "created_timestamp": 1789579000,
            },
        )
        self.assertEqual(post.post_type, TYPE_REPLY)
        self.assertEqual(post.quote_post_id, "55")
        self.assertIn("reply_with_quote", post.warnings)

    def test_tombstone_quote_does_not_fabricate_a_post(self) -> None:
        """实测形态：引用的帖子不可用时上游返回墓碑对象（docs §未验证/真实样本）。"""
        post, nested = self._post(
            quote={
                "id": "55",
                "type": "tombstone",
                "reason": "unavailable",
                "message": "This post is unavailable",
                "author": None,
                "url": "https://x.com/i/status/55",
            }
        )
        self.assertEqual(post.quote_post_id, "55")
        self.assertEqual(nested, [])  # 绝不造一条空帖去覆盖可能已存在的真帖
        self.assertTrue(any(w.startswith("quote_unavailable") for w in post.warnings))

    def test_repost_by_other_account_is_unknown_not_repost(self) -> None:
        """没有明确证据不得断言转帖（方案 §7.2）。"""
        post, _ = self._post(reposted_by={"screen_name": "someone_else"})
        self.assertEqual(post.relation, "unknown")
        self.assertIn("reposted_by_other_account", post.warnings)

    def test_media_only_post_with_empty_text_is_valid(self) -> None:
        post, _ = self._post(
            text="",
            media={"all": [{"url": "https://pbs.twimg.com/a.jpg", "type": "photo"}]},
        )
        self.assertEqual(post.text, "")
        self.assertEqual(post.content_status, "normal")
        self.assertEqual(len(post.media), 1)

    def test_unparsable_timestamp_is_flagged_not_faked(self) -> None:
        post, _ = self._post(created_timestamp=None, created_at="不是时间")
        self.assertIsNone(post.published_at)
        self.assertIn("published_at_unparsable", post.warnings)
        self.assertEqual(post.content_status, "unknown")

    def test_unsafe_media_url_is_dropped(self) -> None:
        post, _ = self._post(media={"all": [{"url": "javascript:alert(1)", "type": "photo"}]})
        self.assertEqual(post.media, [])
        self.assertIn("media_item_without_safe_url", post.warnings)

    def test_missing_id_is_parse_failure(self) -> None:
        with self.assertRaises(ParseFailure):
            self._post(id=None)

    def test_post_id_stays_a_string(self) -> None:
        post, _ = self._post(id=2100275089359163441)
        self.assertEqual(post.post_id, "2100275089359163441")
        self.assertIsInstance(post.post_id, str)

    def test_path_traversal_id_rejected(self) -> None:
        with self.assertRaises(ParseFailure):
            self._post(id="../../etc/passwd")

    def test_duplicate_ids_in_one_page_collapse(self) -> None:
        item = {
            "id": "1",
            "author": {"screen_name": "alice"},
            "text": "hi",
            "created_timestamp": 1789579577,
        }
        payload = {"code": 200, "results": [item, dict(item)], "cursor": {"bottom": "c"}}
        posts, _, failures, _ = normalize_json_page(payload, "alice", "fxtwitter_json")
        self.assertEqual(len(posts), 1)
        self.assertEqual(failures, 0)

    def test_partial_item_failures_keep_valid_items(self) -> None:
        good = {
            "id": "1",
            "author": {"screen_name": "alice"},
            "text": "hi",
            "created_timestamp": 1789579577,
        }
        payload = {"code": 200, "results": [good, {"id": None}, "garbage"], "cursor": {}}
        posts, warnings, failures, _ = normalize_json_page(payload, "alice", "fxtwitter_json")
        self.assertEqual(len(posts), 1)
        self.assertEqual(failures, 2)
        self.assertIn("parse_failures=2", warnings)

    def test_business_error_code_is_not_empty_list(self) -> None:
        """HTTP 200 但业务 code 出错必须按失败处理（方案 §10）。"""
        with self.assertRaises(ParseFailure):
            normalize_json_page(
                {"code": 404, "results": [], "cursor": {}}, "alice", "fxtwitter_json"
            )

    def test_metrics_missing_is_null_not_zero(self) -> None:
        post, _ = self._post(likes=5, views=None)
        self.assertEqual(post.metrics["likes"], 5)
        self.assertIsNone(post.metrics["views"])
        self.assertNotIn("reposts", post.metrics)

    def test_content_hash_ignores_metrics(self) -> None:
        a, _ = self._post(likes=1)
        b, _ = self._post(likes=999999)
        self.assertEqual(a.content_hash, b.content_hash)

    def test_content_hash_tracks_text(self) -> None:
        a, _ = self._post(text="one")
        b, _ = self._post(text="two")
        self.assertNotEqual(a.content_hash, b.content_hash)


class RssTests(unittest.TestCase):
    def test_rss_item_parsing(self) -> None:
        item = {
            "link": "https://x.com/alice/status/123",
            "guid": "https://x.com/alice/status/123",
            "pubDate": "Wed, 16 Sep 2026 17:26:17 GMT",
            "text": "hello world",
            "media": [{"url": "https://video.twimg.com/a.mp4", "type": "video/mp4"}],
        }
        post = normalize_rss_item(item, "alice", "fxtwitter_rss")
        self.assertEqual(post.post_id, "123")
        self.assertEqual(post.author_handle, "alice")
        self.assertEqual(post.relation, RELATION_SELF)
        self.assertEqual(post.post_type, TYPE_UNKNOWN)  # RSS 判不出类型
        self.assertIn("rss_limited_fields", post.warnings)
        self.assertIsNotNone(post.published_at)

    def test_rss_cannot_determine_relation_for_other_authors(self) -> None:
        """RSS 没有 reposted_by，不能猜转帖（方案 §7.2）。"""
        item = {
            "link": "https://x.com/bob/status/9",
            "pubDate": "Wed, 16 Sep 2026 17:26:17 GMT",
            "text": "x",
        }
        post = normalize_rss_item(item, "alice", "fxtwitter_rss")
        self.assertEqual(post.relation, "unknown")
        self.assertIn("rss_cannot_determine_relation", post.warnings)

    def test_rss_without_status_id_fails(self) -> None:
        with self.assertRaises(ParseFailure):
            normalize_rss_item({"link": "https://example.com/x"}, "alice", "fxtwitter_rss")

    def test_html_is_converted_to_text_not_executed(self) -> None:
        html = '<p>hello</p><p><a href="https://x.com/a">Media</a></p><script>bad()</script>'
        text = html_to_text(html)
        self.assertIn("hello", text)
        self.assertNotIn("<script>", text)
        self.assertNotIn("bad()", text)
        self.assertNotIn("<p>", text)


class TimestampTests(unittest.TestCase):
    def test_twitter_format(self) -> None:
        moment = parse_upstream_timestamp(None, "Wed Sep 16 17:26:17 +0000 2026")
        self.assertIsNotNone(moment)
        self.assertEqual(moment.year, 2026)
        self.assertEqual(moment.hour, 17)

    def test_numeric_timestamp_preferred(self) -> None:
        moment = parse_upstream_timestamp(1789579577, "garbage")
        self.assertIsNotNone(moment)
        self.assertEqual(int(moment.timestamp()), 1789579577)

    def test_millisecond_timestamp_rejected(self) -> None:
        """毫秒被当成秒会把时间推到几万年后 —— 必须落到文本解析或 None。"""
        moment = parse_upstream_timestamp(1789579577000, None)
        self.assertIsNone(moment)

    def test_rfc2822_for_rss(self) -> None:
        moment = parse_upstream_timestamp(None, "Thu, 17 Sep 2026 04:35:18 GMT")
        self.assertIsNotNone(moment)
        self.assertEqual(moment.day, 17)

    def test_garbage_returns_none(self) -> None:
        self.assertIsNone(parse_upstream_timestamp(None, None))
        self.assertIsNone(parse_upstream_timestamp("abc", "xyz"))


if __name__ == "__main__":
    unittest.main()
