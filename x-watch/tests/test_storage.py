"""存储层测试：去重、合并策略、状态推进。对应方案 §7 与 §14。"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from support import make_post, utc  # noqa: E402

from x_watch.normalize import (  # noqa: E402
    RELATION_CONTEXT,
    RELATION_QUOTED,
    RELATION_REPOST,
    RELATION_SELF,
    TYPE_ORIGINAL,
    TYPE_REPLY,
    TYPE_UNKNOWN,
)
from x_watch.storage import Storage  # noqa: E402
from x_watch.util import now_utc  # noqa: E402


class StorageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="x-watch-test-")
        self.storage = Storage(os.path.join(self.tmp, "test.sqlite3"))
        self.now = now_utc()

    def tearDown(self) -> None:
        self.storage.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def save(self, post, handle: str = "alice", bootstrap: bool = False):
        with self.storage.transaction():
            action = self.storage.upsert_post(post, self.now)
            link = self.storage.upsert_account_post(
                handle, post.post_id, post.relation, self.now, "run1", bootstrap
            )
        return action, link


class DedupeTests(StorageTestCase):
    def test_same_post_twice_inserts_once(self) -> None:
        post = make_post("1")
        self.assertEqual(self.save(post)[0], "inserted")
        self.assertEqual(self.save(post)[0], "seen")
        self.assertEqual(self.storage.counts()["posts"], 1)

    def test_two_accounts_one_post_two_links(self) -> None:
        """正文一条、账号关联两条（方案 §14）。"""
        post = make_post("1", author="carol", relation=RELATION_REPOST)
        self.save(post, handle="alice")
        self.save(post, handle="bob")
        self.assertEqual(self.storage.counts()["posts"], 1)
        self.assertEqual(self.storage.counts()["links"], 2)
        row = self.storage.get_post("1")
        self.assertEqual(row["author_handle"], "carol")  # 作者没被改成监控账号

    def test_content_update_does_not_add_a_row(self) -> None:
        self.save(make_post("1", text="before"))
        action, _ = self.save(make_post("1", text="after"))
        self.assertEqual(action, "content_updated")
        self.assertEqual(self.storage.counts()["posts"], 1)
        row = self.storage.get_post("1")
        self.assertEqual(row["text"], "after")
        self.assertIsNotNone(row["content_updated_at"])

    def test_metrics_change_is_not_a_content_update(self) -> None:
        """互动数字变化不应造成正文归档反复更新（方案 §7.1）。"""
        self.save(make_post("1", text="same", metrics={"likes": 1}))
        action, _ = self.save(make_post("1", text="same", metrics={"likes": 5000}))
        self.assertEqual(action, "seen")
        row = self.storage.get_post("1")
        self.assertIsNone(row["content_updated_at"])
        self.assertIn("5000", row["metrics_json"])  # 快照仍然更新了

    def test_first_seen_is_preserved(self) -> None:
        self.save(make_post("1"))
        first = self.storage.get_post("1")["first_seen_at"]
        self.now = now_utc()
        self.save(make_post("1", text="changed"))
        self.assertEqual(self.storage.get_post("1")["first_seen_at"], first)


class MergePolicyTests(StorageTestCase):
    def test_rss_downgrade_does_not_blank_text(self) -> None:
        """RSS 降级缺少原有字段时不得把已有完整内容覆盖为空（方案 §14）。"""
        self.save(make_post("1", text="完整正文", source="fxtwitter_json"))
        action, _ = self.save(make_post("1", text="", source="fxtwitter_rss"))
        self.assertEqual(action, "seen")
        row = self.storage.get_post("1")
        self.assertEqual(row["text"], "完整正文")
        self.assertEqual(row["source"], "fxtwitter_json")  # 来源不被降级覆盖

    def test_rss_downgrade_does_not_blank_media(self) -> None:
        media = [{"url": "https://pbs.twimg.com/a.jpg", "type": "photo"}]
        self.save(make_post("1", media=media, source="fxtwitter_json"))
        self.save(make_post("1", media=[], source="fxtwitter_rss"))
        self.assertIn("a.jpg", self.storage.get_post("1")["media_json"])

    def test_unknown_type_does_not_erase_known_type(self) -> None:
        self.save(make_post("1", post_type=TYPE_REPLY, reply_to_id="7"))
        self.save(make_post("1", post_type=TYPE_UNKNOWN, source="fxtwitter_rss"))
        self.assertEqual(self.storage.get_post("1")["post_type"], TYPE_REPLY)

    def test_absent_reply_structure_does_not_downgrade_to_original(self) -> None:
        """实测：同一帖子在不同账号时间线里 replying_to 有无不一致。

        帖子 2098217573276131577 在 @bcherny 的时间线带 replying_to（reply），
        在 @simonw 的时间线里是 null（original）。若允许覆盖，它会来回翻转并
        每轮重写归档文件。
        """
        self.save(make_post("1", post_type=TYPE_REPLY, reply_to_id="7"), handle="alice")
        action, _ = self.save(
            make_post("1", post_type=TYPE_ORIGINAL, reply_to_id=None), handle="bob"
        )
        self.assertEqual(action, "seen")
        row = self.storage.get_post("1")
        self.assertEqual(row["post_type"], TYPE_REPLY)
        self.assertEqual(row["reply_to_id"], "7")
        self.assertIsNone(row["content_updated_at"])

    def test_no_flip_flop_across_repeated_alternating_views(self) -> None:
        """反复交替观察也不能产生持续的"内容更新"。"""
        rich = make_post("1", post_type=TYPE_REPLY, reply_to_id="7")
        thin = make_post("1", post_type=TYPE_ORIGINAL, reply_to_id=None)
        actions = []
        for _ in range(3):
            actions.append(self.save(rich, handle="alice")[0])
            actions.append(self.save(thin, handle="bob")[0])
        # 第一次插入，之后全部是 seen —— 没有一次误报的 content_updated
        self.assertEqual(actions[0], "inserted")
        self.assertEqual(set(actions[1:]), {"seen"})

    def test_type_upgrade_happens_once(self) -> None:
        self.save(make_post("1", post_type=TYPE_ORIGINAL))
        action, _ = self.save(make_post("1", post_type=TYPE_REPLY, reply_to_id="7"))
        self.assertEqual(action, "content_updated")
        self.assertEqual(self.save(make_post("1", post_type=TYPE_REPLY, reply_to_id="7"))[0], "seen")


class RelationTests(StorageTestCase):
    def test_relation_upgrades_with_stronger_evidence(self) -> None:
        post = make_post("1", author="carol", relation=RELATION_CONTEXT)
        self.assertEqual(self.save(post)[1], "linked")
        post.relation = RELATION_REPOST
        self.assertEqual(self.save(post)[1], "relation_upgraded")
        links = self.storage.links_for_post("1")
        self.assertEqual(links[0]["relation"], RELATION_REPOST)

    def test_relation_never_downgrades(self) -> None:
        post = make_post("1", relation=RELATION_SELF)
        self.save(post)
        post.relation = RELATION_QUOTED
        self.assertEqual(self.save(post)[1], "existing")
        self.assertEqual(self.storage.links_for_post("1")[0]["relation"], RELATION_SELF)

    def test_known_ids_snapshot(self) -> None:
        self.save(make_post("1"))
        self.assertEqual(self.storage.known_post_ids("alice"), {"1"})
        self.assertEqual(self.storage.known_post_ids("bob"), set())


class AccountStateTests(StorageTestCase):
    def test_scope_change_invalidates_boundary(self) -> None:
        """改变采集范围后旧边界不能直接视为新范围已覆盖（方案 §7.3）。"""
        with self.storage.transaction():
            self.storage.ensure_account_state("alice", "scope-a")
            self.storage.save_account_state(
                "alice",
                last_scan_boundary_at="2026-09-17T00:00:00+00:00",
                coverage_status="overlap_observed",
            )
        with self.storage.transaction():
            state = self.storage.ensure_account_state("alice", "scope-b")
        self.assertTrue(state["scope_changed"])
        self.assertIsNone(state["last_scan_boundary_at"])
        row = self.storage.get_account_state("alice")
        self.assertIsNone(row["last_scan_boundary_at"])
        self.assertIsNone(row["coverage_status"])

    def test_same_scope_keeps_boundary(self) -> None:
        with self.storage.transaction():
            self.storage.ensure_account_state("alice", "scope-a")
            self.storage.save_account_state(
                "alice", last_scan_boundary_at="2026-09-17T00:00:00+00:00"
            )
        with self.storage.transaction():
            state = self.storage.ensure_account_state("alice", "scope-a")
        self.assertFalse(state["scope_changed"])
        self.assertEqual(state["last_scan_boundary_at"], "2026-09-17T00:00:00+00:00")

    def test_unknown_state_field_rejected(self) -> None:
        with self.storage.transaction():
            self.storage.ensure_account_state("alice", "s")
        with self.assertRaises(ValueError):
            with self.storage.transaction():
                self.storage.save_account_state("alice", nonexistent=1)

    def test_source_backoff_roundtrip(self) -> None:
        until = utc(2026, 9, 17, 12)
        with self.storage.transaction():
            self.storage.set_source_backoff("fxtwitter", until, "429")
        row = self.storage.get_source_state("fxtwitter")
        self.assertEqual(row["consecutive_throttle"], 1)
        with self.storage.transaction():
            self.storage.set_source_backoff("fxtwitter", until, "429 again")
        self.assertEqual(self.storage.get_source_state("fxtwitter")["consecutive_throttle"], 2)
        with self.storage.transaction():
            self.storage.clear_source_backoff("fxtwitter")
        row = self.storage.get_source_state("fxtwitter")
        self.assertIsNone(row["next_allowed_at"])
        self.assertEqual(row["consecutive_throttle"], 0)


class TransactionTests(StorageTestCase):
    def test_rollback_on_error(self) -> None:
        """数据库写入失败要回滚当前事务（方案 §10）。"""
        try:
            with self.storage.transaction():
                self.storage.upsert_post(make_post("1"), self.now)
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        self.assertEqual(self.storage.counts()["posts"], 0)

    def test_foreign_key_enforced(self) -> None:
        import sqlite3

        with self.assertRaises(sqlite3.IntegrityError):
            with self.storage.transaction():
                self.storage.upsert_account_post(
                    "alice", "does-not-exist", RELATION_SELF, self.now, "run1"
                )


if __name__ == "__main__":
    unittest.main()
