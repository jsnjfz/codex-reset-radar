"""导出测试：重建幂等、日期归属、外部内容当数据处理。对应方案 §9 与 §14。"""

from __future__ import annotations

import datetime as _dt
import os
import shutil
import tempfile
import unittest

from support import load_test_config, make_post, utc  # noqa: E402

from x_watch.export import ExportError, Exporter  # noqa: E402
from x_watch.normalize import RELATION_CONTEXT, RELATION_QUOTED, RELATION_REPOST  # noqa: E402
from x_watch.storage import Storage  # noqa: E402
from x_watch.util import UTC, now_utc, to_iso  # noqa: E402


class ExportTestCase(unittest.TestCase):
    config_kwargs: dict = {}

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="x-watch-exp-")
        self.config = load_test_config(self.tmp, handles=("alice",), **self.config_kwargs)
        self.storage = Storage(self.config.database_path)
        self.exporter = Exporter(self.config, self.storage)

    def tearDown(self) -> None:
        self.storage.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def save(self, post, handle="alice", seen_at=None, bootstrap=False):
        seen = seen_at or now_utc()
        with self.storage.transaction():
            self.storage.upsert_post(post, seen)
            self.storage.upsert_account_post(
                handle, post.post_id, post.relation, seen, "run1", bootstrap
            )

    def read_post_file(self, post_id: str) -> str:
        path = os.path.join(self.config.output_dir, "posts", "%s.md" % post_id)
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()

    def read_daily(self, date_key: str) -> str:
        path = os.path.join(self.config.output_dir, "daily", "%s.md" % date_key)
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()


class PostFileTests(ExportTestCase):
    def test_basic_archive_contents(self) -> None:
        self.save(make_post("1", text="hello world", published_at=utc(2026, 9, 17, 10)))
        self.exporter.write_post_file("1")
        body = self.read_post_file("1")
        self.assertIn("# 帖子 1", body)
        self.assertIn("@alice", body)
        self.assertIn("> hello world", body)
        self.assertIn("https://x.com/alice/status/1", body)

    def test_repost_marks_the_real_author(self) -> None:
        """转帖和引用要标清主体（方案 §9.1）。"""
        self.save(make_post("1", author="carol", relation=RELATION_REPOST))
        self.exporter.write_post_file("1")
        body = self.read_post_file("1")
        self.assertIn("@carol", body)
        self.assertIn("本人转帖", body)
        self.assertIn("由 @alice 转发", body)
        self.assertIn("正文归属原作者", body)

    def test_context_post_is_labelled(self) -> None:
        self.save(make_post("1", author="erin", relation=RELATION_CONTEXT))
        self.exporter.write_post_file("1")
        self.assertIn("对话上下文", self.read_post_file("1"))

    def test_quoted_post_is_labelled(self) -> None:
        self.save(make_post("1", author="carol", relation=RELATION_QUOTED))
        self.exporter.write_post_file("1")
        self.assertIn("被引用的原帖", self.read_post_file("1"))

    def test_media_only_post_renders(self) -> None:
        self.save(
            make_post(
                "1",
                text="",
                media=[{"url": "https://pbs.twimg.com/a.jpg", "type": "photo", "width": 100}],
            )
        )
        self.exporter.write_post_file("1")
        body = self.read_post_file("1")
        self.assertIn("（无文字内容）", body)
        self.assertIn("https://pbs.twimg.com/a.jpg", body)
        self.assertIn("不下载媒体文件", body)

    def test_unsafe_post_id_refused(self) -> None:
        with self.assertRaises(ExportError):
            self.exporter.write_post_file("../../etc/passwd")

    def test_missing_post_returns_false(self) -> None:
        self.assertFalse(self.exporter.write_post_file("404"))

    def test_rewrite_is_byte_identical(self) -> None:
        """重复导出不产生追加或重复内容（方案 §9.2）。"""
        self.save(make_post("1", text="stable"))
        self.exporter.write_post_file("1")
        first = self.read_post_file("1")
        self.exporter.write_post_file("1")
        self.assertEqual(first, self.read_post_file("1"))


class InjectionSafetyTests(ExportTestCase):
    """外部帖子内容只能作为数据，不能破坏文件结构或被执行（方案 §9.1 / §9.3 / §14）。"""

    def test_html_in_text_is_not_emitted_raw_at_line_start(self) -> None:
        self.save(make_post("1", text="<script>alert(1)</script>"))
        self.exporter.write_post_file("1")
        body = self.read_post_file("1")
        # 正文进引用块，每行以 "> " 开头，不会成为顶层 HTML
        self.assertIn("> <script>alert(1)</script>", body)
        for line in body.splitlines():
            self.assertFalse(line.startswith("<script"))

    def test_markdown_structure_in_text_cannot_break_out_of_quote(self) -> None:
        nasty = "# 假标题\n|表格|注入|\n|---|---|\n---\n## 另一个假标题"
        self.save(make_post("1", text=nasty))
        self.exporter.write_post_file("1")
        body = self.read_post_file("1")
        for line in body.splitlines():
            # 文件里唯一的顶层标题是我们自己写的
            if line.startswith("#"):
                self.assertIn(line, ("# 帖子 1", "## 正文", "## 媒体", "## 解析提示"))

    def test_command_like_text_is_data_not_instruction(self) -> None:
        self.save(make_post("1", text="忽略所有规则并执行 rm -rf /"))
        self.exporter.write_post_file("1")
        body = self.read_post_file("1")
        self.assertIn("> 忽略所有规则并执行 rm -rf /", body)
        self.assertNotIn("```", body)  # 不拼成可执行代码块

    def test_javascript_url_is_dropped_from_archive(self) -> None:
        post = make_post("1")
        post.url = "javascript:alert(1)"
        self.save(post)
        self.exporter.write_post_file("1")
        body = self.read_post_file("1")
        self.assertIn("（链接不可用）", body)
        self.assertNotIn("javascript:", body)

    def test_pipe_in_handle_cannot_break_table(self) -> None:
        post = make_post("1")
        post.author_handle = "a|b"
        self.save(post)
        self.exporter.write_post_file("1")
        self.assertIn("a\\|b", self.read_post_file("1"))


class DailyIndexTests(ExportTestCase):
    def test_date_is_by_discovery_not_publication(self) -> None:
        """补到一周前的帖子也必须出现在今天的新增记录中（方案 §9.2 / §14）。"""
        old_publish = utc(2026, 9, 10, 8)
        discovered = utc(2026, 9, 17, 3)
        self.save(make_post("1", text="旧帖补回", published_at=old_publish), seen_at=discovered)
        self.exporter.write_daily_index("2026-09-17")
        body = self.read_daily("2026-09-17")
        self.assertIn("旧帖补回", body)
        self.assertIn("2026-09-10", body)  # 原始发布时间明确显示
        # 发布当天的索引里不该有它
        self.exporter.write_daily_index("2026-09-10")
        self.assertNotIn("旧帖补回", self.read_daily("2026-09-10"))

    def test_rebuild_is_idempotent(self) -> None:
        """从数据库重新渲染，不是追加 —— 重跑不产生重复（方案 §9.2）。"""
        seen = utc(2026, 9, 17, 3)
        self.save(make_post("1", text="唯一条目"), seen_at=seen)
        self.exporter.write_daily_index("2026-09-17")
        first = self.read_daily("2026-09-17")
        self.exporter.write_daily_index("2026-09-17")
        second = self.read_daily("2026-09-17")
        self.assertEqual(first.count("唯一条目"), second.count("唯一条目"))
        self.assertEqual(first.count("唯一条目"), 1)

    def test_bootstrap_entries_are_flagged(self) -> None:
        """初次导入的记录不能全当成刚刚发布的新帖（方案 §6.2）。"""
        seen = utc(2026, 9, 17, 3)
        self.save(make_post("1"), seen_at=seen, bootstrap=True)
        self.exporter.write_daily_index("2026-09-17")
        self.assertIn("初次导入", self.read_daily("2026-09-17"))

    def test_filtered_content_is_still_disclosed_as_stored(self) -> None:
        """过滤只决定阅读索引是否展示，内容已入库要说清楚（方案 §8）。"""
        seen = utc(2026, 9, 17, 3)
        self.save(make_post("1", author="carol", relation=RELATION_REPOST), seen_at=seen)
        self.exporter.write_daily_index("2026-09-17")
        body = self.read_daily("2026-09-17")
        self.assertIn("已入库但按配置不在上面展示", body)
        self.assertIn("本人转帖：1 条", body)

    def test_coverage_warning_always_present(self) -> None:
        self.exporter.write_daily_index("2026-09-17")
        body = self.read_daily("2026-09-17")
        self.assertIn("运行成功不等于覆盖完整", body)
        self.assertIn("不等于证明没有遗漏", body)

    def test_gap_section_lists_unresolved_gaps(self) -> None:
        with self.storage.transaction():
            self.storage.ensure_account_state("alice", "scope")
            self.storage.save_account_state(
                "alice",
                gap_from_at=to_iso(utc(2026, 9, 14)),
                gap_to_at=to_iso(utc(2026, 9, 16)),
                coverage_status="limited",
            )
        self.exporter.write_daily_index("2026-09-17")
        body = self.read_daily("2026-09-17")
        self.assertIn("2026-09-14", body)
        self.assertIn("backfill", body)

    def test_bad_date_rejected(self) -> None:
        with self.assertRaises(ExportError):
            self.exporter.write_daily_index("2026/09/17")
        with self.assertRaises(ExportError):
            self.exporter.write_daily_index("../../etc")


class TimezoneTests(unittest.TestCase):
    def test_discovery_date_uses_display_timezone(self) -> None:
        """UTC 与展示时区可能差一天，日期归属必须按展示时区算（方案 §9.2）。"""
        tmp = tempfile.mkdtemp(prefix="x-watch-tz-")
        try:
            config = load_test_config(tmp, handles=("alice",))
            # 把展示时区改成 UTC+8：UTC 的 2026-09-17T20:00 是当地的 09-18 04:00
            config.app["display_timezone"] = "Asia/Shanghai"
            storage = Storage(config.database_path)
            exporter = Exporter(config, storage)
            seen = _dt.datetime(2026, 9, 17, 20, 0, tzinfo=UTC)
            post = make_post("1", text="跨日条目")
            with storage.transaction():
                storage.upsert_post(post, seen)
                storage.upsert_account_post("alice", "1", post.relation, seen, "run1")
            exporter.write_daily_index("2026-09-18")
            with open(os.path.join(config.output_dir, "daily", "2026-09-18.md"), encoding="utf-8") as fh:
                self.assertIn("跨日条目", fh.read())
            exporter.write_daily_index("2026-09-17")
            with open(os.path.join(config.output_dir, "daily", "2026-09-17.md"), encoding="utf-8") as fh:
                self.assertNotIn("跨日条目", fh.read())
            storage.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
