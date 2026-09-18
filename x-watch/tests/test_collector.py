"""采集流程测试：覆盖判定、停止条件、故障处理。

对应方案 §6、§10 与 §14 的"必须覆盖的测试场景"。全部离线，用 FakeProvider 精确
重现真实接口无法按需触发的故障（429、重复游标、第二页失败）。
"""

from __future__ import annotations

import datetime as _dt
import os
import shutil
import tempfile
import unittest

from support import (  # noqa: E402
    FakeProvider,
    hours_ago,
    load_test_config,
    make_client,
    make_failed_page,
    make_page,
    make_post,
)

from x_watch.collector import (  # noqa: E402
    COVERAGE_BOOTSTRAP,
    COVERAGE_EXHAUSTED,
    COVERAGE_LIMITED,
    COVERAGE_OVERLAP,
    COVERAGE_UNKNOWN,
    RUN_DEFERRED,
    RUN_FAILED,
    RUN_PARTIAL,
    RUN_SKIPPED,
    RUN_SUCCESS,
    Collector,
)
from x_watch.lock import LockBusy, SingleInstanceLock  # noqa: E402
from x_watch.normalize import RELATION_CONTEXT, RELATION_REPOST, TYPE_UNKNOWN  # noqa: E402
from x_watch.storage import Storage  # noqa: E402
from x_watch.util import now_utc, parse_iso, to_iso  # noqa: E402


class CollectorTestCase(unittest.TestCase):
    handles = ("alice",)
    config_kwargs: dict = {}

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="x-watch-col-")
        self.config = load_test_config(self.tmp, handles=self.handles, **self.config_kwargs)
        self.storage = Storage(self.config.database_path)

    def tearDown(self) -> None:
        self.storage.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, script, max_requests: int = 30, supports_pagination: bool = True):
        client = make_client(max_requests=max_requests)
        provider = FakeProvider(client, script, supports_pagination=supports_pagination)
        return Collector(self.config, self.storage, client=client, provider=provider), provider

    def seed_boundary(self, handle: str = "alice", hours: float = 1.0) -> _dt.datetime:
        """把账号置为"已完成初次导入、边界在 N 小时前"的状态。"""
        # to_iso 只保留到秒，测试里也用同样精度，避免比较微秒差
        boundary = parse_iso(to_iso(hours_ago(hours)))
        account = self.config.find_account(handle)
        with self.storage.transaction():
            self.storage.ensure_account_state(handle, account.scope_hash)
            self.storage.save_account_state(
                handle,
                bootstrap_done=1,
                last_scan_boundary_at=to_iso(boundary),
                coverage_status=COVERAGE_OVERLAP,
            )
        return boundary

    def seed_known_post(self, post, handle: str = "alice") -> None:
        with self.storage.transaction():
            self.storage.upsert_post(post, now_utc())
            self.storage.upsert_account_post(
                handle, post.post_id, post.relation, now_utc(), "seed"
            )

    def state(self, handle: str = "alice"):
        return self.storage.get_account_state(handle)


class BootstrapTests(CollectorTestCase):
    def test_bootstrap_reaching_window_sets_boundary_without_gap(self) -> None:
        posts = [
            make_post("1", published_at=hours_ago(1)),
            make_post("2", published_at=hours_ago(80)),
            make_post("3", published_at=hours_ago(90)),
        ]
        collector, provider = self.build({"alice": [make_page(posts, next_cursor="c1")]})
        summary = collector.run_once()
        outcome = summary.outcomes[0]
        self.assertEqual(outcome.status, RUN_SUCCESS)
        self.assertEqual(outcome.coverage, COVERAGE_BOOTSTRAP)
        self.assertEqual(outcome.new_count, 3)
        self.assertEqual(len(provider.calls), 1)
        row = self.state()
        self.assertEqual(row["bootstrap_done"], 1)
        self.assertIsNotNone(row["last_scan_boundary_at"])
        self.assertIsNone(row["gap_from_at"])

    def test_bootstrap_short_of_window_records_gap_but_still_advances(self) -> None:
        """预算不足以覆盖 72 小时 → 保存数据、记录缺口，但不阻塞后续新帖收集（§6.2）。"""
        pages = [
            make_page([make_post(str(i), published_at=hours_ago(i))], next_cursor="c%d" % i)
            for i in range(1, 5)
        ]
        collector, provider = self.build({"alice": pages})
        outcome = collector.run_once().outcomes[0]
        self.assertEqual(outcome.status, RUN_SUCCESS)
        self.assertEqual(outcome.coverage, COVERAGE_BOOTSTRAP)
        self.assertEqual(len(provider.calls), 3)  # bootstrap_max_pages
        row = self.state()
        self.assertIsNotNone(row["last_scan_boundary_at"])
        self.assertIsNotNone(row["gap_from_at"])

    def test_bootstrap_records_bootstrap_flag_on_links(self) -> None:
        collector, _ = self.build(
            {"alice": [make_page([make_post("1", published_at=hours_ago(100))])]}
        )
        collector.run_once()
        link = self.storage.links_for_post("1")[0]
        self.assertEqual(link["is_bootstrap"], 1)


class StopConditionTests(CollectorTestCase):
    def test_old_pinned_post_alone_does_not_stop_pagination(self) -> None:
        """场景：第一条是很旧的置顶帖 → 不因此停止分页（方案 §6.4.2 / §14）。"""
        self.seed_boundary(hours=1)
        pinned = make_post("pinned", published_at=hours_ago(24 * 365))
        recent = make_post("r1", published_at=hours_ago(0.5))
        page1 = make_page([pinned, recent], next_cursor="c1")
        page2 = make_page([make_post("r2", published_at=hours_ago(0.6))], next_cursor="c2")
        page3 = make_page([make_post("r3", published_at=hours_ago(0.7))], next_cursor="c3")
        collector, provider = self.build({"alice": [page1, page2, page3]})
        outcome = collector.run_once().outcomes[0]
        # 只有一条早于目标时间（置顶帖），证据不足 → 继续翻页直到预算用尽
        self.assertEqual(len(provider.calls), 3)
        self.assertEqual(outcome.coverage, COVERAGE_LIMITED)

    def test_two_old_posts_plus_reseen_known_stops_with_overlap(self) -> None:
        boundary = self.seed_boundary(hours=1)
        target = boundary - _dt.timedelta(hours=6)
        known = make_post("known", published_at=target - _dt.timedelta(hours=1))
        self.seed_known_post(known)
        posts = [
            make_post("new1", published_at=hours_ago(0.1)),
            known,
            make_post("old2", published_at=target - _dt.timedelta(hours=2)),
        ]
        collector, provider = self.build({"alice": [make_page(posts, next_cursor="c1")]})
        outcome = collector.run_once().outcomes[0]
        self.assertEqual(outcome.coverage, COVERAGE_OVERLAP)
        self.assertEqual(len(provider.calls), 1)
        self.assertTrue(outcome.boundary_advanced)

    def test_old_enough_but_no_known_post_reseen_does_not_claim_overlap(self) -> None:
        """时间够旧但没重见任何已知帖 → 重叠未成立，继续取页。"""
        boundary = self.seed_boundary(hours=1)
        target = boundary - _dt.timedelta(hours=6)
        posts = [
            make_post("a", published_at=target - _dt.timedelta(hours=1)),
            make_post("b", published_at=target - _dt.timedelta(hours=2)),
        ]
        collector, provider = self.build(
            {"alice": [make_page(posts, next_cursor="c1"), make_page([], next_cursor="c2"),
                       make_page([], next_cursor="c3")]}
        )
        outcome = collector.run_once().outcomes[0]
        self.assertGreater(len(provider.calls), 1)
        self.assertEqual(outcome.coverage, COVERAGE_LIMITED)

    def test_reposts_and_context_never_trigger_stop(self) -> None:
        """转帖的 published_at 是原帖时间，不得参与边界判断（方案 §6.4.2）。"""
        self.seed_boundary(hours=1)
        ancient = hours_ago(24 * 400)
        posts = [
            make_post("rp1", author="carol", relation=RELATION_REPOST, published_at=ancient),
            make_post("rp2", author="dave", relation=RELATION_REPOST, published_at=ancient),
            make_post("ctx", author="erin", relation=RELATION_CONTEXT, published_at=ancient),
        ]
        collector, provider = self.build(
            {"alice": [make_page(posts, next_cursor="c1"), make_page([], next_cursor="c2"),
                       make_page([], next_cursor="c3")]}
        )
        outcome = collector.run_once().outcomes[0]
        self.assertEqual(len(provider.calls), 3)  # 没有任何可信自有帖 → 停不下来，跑满预算
        self.assertEqual(outcome.coverage, COVERAGE_LIMITED)

    def test_unknown_type_posts_never_trigger_stop(self) -> None:
        self.seed_boundary(hours=1)
        posts = [
            make_post("u1", post_type=TYPE_UNKNOWN, published_at=hours_ago(500)),
            make_post("u2", post_type=TYPE_UNKNOWN, published_at=hours_ago(600)),
        ]
        collector, provider = self.build(
            {"alice": [make_page(posts, next_cursor="c1"), make_page([], next_cursor="c2"),
                       make_page([], next_cursor="c3")]}
        )
        collector.run_once()
        self.assertEqual(len(provider.calls), 3)

    def test_no_cursor_means_source_exhausted(self) -> None:
        self.seed_boundary(hours=1)
        collector, provider = self.build(
            {"alice": [make_page([make_post("1", published_at=hours_ago(0.2))], next_cursor=None)]}
        )
        outcome = collector.run_once().outcomes[0]
        self.assertEqual(outcome.coverage, COVERAGE_EXHAUSTED)
        self.assertTrue(outcome.boundary_advanced)

    def test_duplicate_cursor_stops_as_limited(self) -> None:
        """重复游标 / 分页循环 → 有限停止，不无限请求（方案 §14）。"""
        self.seed_boundary(hours=1)
        page = make_page([make_post("1", published_at=hours_ago(0.2))], next_cursor="same")
        page_again = make_page([make_post("2", published_at=hours_ago(0.3))], next_cursor="same")
        collector, provider = self.build({"alice": [page, page_again]})
        outcome = collector.run_once().outcomes[0]
        self.assertEqual(outcome.coverage, COVERAGE_LIMITED)
        self.assertEqual(len(provider.calls), 2)
        self.assertTrue(any("重复游标" in note for note in outcome.notes))
        self.assertFalse(outcome.boundary_advanced)

    def test_few_results_with_cursor_does_not_end_history(self) -> None:
        """返回条数远少于 page_size 但仍有游标 → 不能据此判定历史结束（方案 §14）。"""
        self.seed_boundary(hours=1)
        pages = [
            make_page([make_post("1", published_at=hours_ago(0.2))], next_cursor="c1",
                      returned_count=1),
            make_page([make_post("2", published_at=hours_ago(0.3))], next_cursor="c2",
                      returned_count=1),
            make_page([make_post("3", published_at=hours_ago(0.4))], next_cursor="c3",
                      returned_count=1),
        ]
        collector, provider = self.build({"alice": pages})
        outcome = collector.run_once().outcomes[0]
        self.assertEqual(len(provider.calls), 3)
        self.assertEqual(outcome.coverage, COVERAGE_LIMITED)

    def test_provider_without_pagination_is_always_limited(self) -> None:
        self.seed_boundary(hours=1)
        page = make_page(
            [make_post("1", published_at=hours_ago(0.2))],
            next_cursor=None,
            supports_pagination=False,
        )
        collector, provider = self.build({"alice": [page]}, supports_pagination=False)
        outcome = collector.run_once().outcomes[0]
        self.assertEqual(outcome.coverage, COVERAGE_LIMITED)
        self.assertEqual(len(provider.calls), 1)
        self.assertFalse(outcome.boundary_advanced)


class FailureTests(CollectorTestCase):
    def test_first_page_ok_second_page_fails_keeps_data_as_partial(self) -> None:
        """场景：第一页成功、第二页失败 → 保留第一页，标记 partial，保留缺口（§14）。"""
        self.seed_boundary(hours=1)
        collector, _ = self.build(
            {
                "alice": [
                    make_page([make_post("1", published_at=hours_ago(0.2))], next_cursor="c1"),
                    make_failed_page("http_5xx", "上游 500"),
                ]
            }
        )
        outcome = collector.run_once().outcomes[0]
        self.assertEqual(outcome.status, RUN_PARTIAL)
        self.assertEqual(outcome.coverage, COVERAGE_LIMITED)
        self.assertEqual(outcome.new_count, 1)
        self.assertIsNotNone(self.storage.get_post("1"))  # 已入库的不丢
        self.assertFalse(outcome.boundary_advanced)
        self.assertIsNotNone(self.state()["gap_from_at"])

    def test_total_failure_does_not_advance_boundary(self) -> None:
        boundary = self.seed_boundary(hours=1)
        collector, _ = self.build({"alice": [make_failed_page("dns", "DNS 解析失败")]})
        outcome = collector.run_once().outcomes[0]
        self.assertEqual(outcome.status, RUN_FAILED)
        self.assertEqual(outcome.coverage, COVERAGE_UNKNOWN)
        row = self.state()
        self.assertEqual(parse_iso(row["last_scan_boundary_at"]), boundary)
        self.assertEqual(row["consecutive_failures"], 1)

    def test_parse_failures_make_run_partial(self) -> None:
        """部分条目无法解析 → 有效条目入库、状态 partial、不静默推进边界（§10）。"""
        self.seed_boundary(hours=1)
        page = make_page(
            [make_post("1", published_at=hours_ago(0.2))], next_cursor=None, parse_failures=2
        )
        collector, _ = self.build({"alice": [page]})
        outcome = collector.run_once().outcomes[0]
        self.assertEqual(outcome.status, RUN_PARTIAL)
        self.assertEqual(outcome.parse_failures, 2)
        self.assertEqual(outcome.new_count, 1)
        self.assertFalse(outcome.boundary_advanced)

    def test_legal_empty_list_is_recorded_not_interpreted(self) -> None:
        """合法空列表 → 记录"源返回空"，不能据此证明账号没发帖（§10）。"""
        self.seed_boundary(hours=1)
        page = make_page([], next_cursor=None, warnings=["source_returned_empty"])
        collector, _ = self.build({"alice": [page]})
        outcome = collector.run_once().outcomes[0]
        self.assertEqual(outcome.status, RUN_SUCCESS)
        self.assertEqual(outcome.new_count, 0)
        self.assertTrue(any("source_returned_empty" in n for n in outcome.notes))

    def test_404_does_not_conclude_account_deleted(self) -> None:
        self.seed_boundary(hours=1)
        collector, _ = self.build({"alice": [make_failed_page("http_404", "HTTP 404")]})
        outcome = collector.run_once().outcomes[0]
        self.assertEqual(outcome.status, RUN_FAILED)
        self.assertTrue(any("不可访问" in n for n in outcome.notes))
        self.assertTrue(any("未判断是删号" in n for n in outcome.notes))

    def test_error_saves_diagnostic_sample(self) -> None:
        self.seed_boundary(hours=1)
        collector, _ = self.build({"alice": [make_failed_page("parse", "返回了 HTML")]})
        collector.run_once()
        files = os.listdir(self.config.raw_dir)
        self.assertTrue(any(name.endswith("-error.txt") for name in files))


class ThrottleTests(CollectorTestCase):
    handles = ("alice", "bob")

    def test_429_with_retry_after_pauses_whole_source(self) -> None:
        """一次 429 影响同源所有账号，不能只跳过当前账号继续请求其余账号（§10）。"""
        collector, provider = self.build(
            {
                "alice": [make_failed_page("http_429", "429", retry_after_seconds=120)],
                "bob": [make_page([make_post("b1", handle="bob")])],
            }
        )
        summary = collector.run_once()
        self.assertEqual(summary.outcomes[0].status, RUN_FAILED)
        self.assertEqual(summary.outcomes[1].status, RUN_SKIPPED)
        self.assertEqual(len(provider.calls), 1)  # bob 完全没有被请求
        row = self.storage.get_source_state("fxtwitter")
        until = parse_iso(row["next_allowed_at"])
        self.assertGreater(until, now_utc())
        self.assertLess(until, now_utc() + _dt.timedelta(seconds=200))

    def test_429_backoff_survives_restart(self) -> None:
        """重启后仍遵守退避 —— 退避写在数据库里，不靠进程挂着等（§10）。"""
        collector, _ = self.build(
            {"alice": [make_failed_page("http_429", "429", retry_after_seconds=3600)]}
        )
        collector.run_once()
        collector2, provider2 = self.build({"alice": [make_page([make_post("1")])]})
        summary = collector2.run_once()
        self.assertEqual(summary.outcomes[0].status, RUN_SKIPPED)
        self.assertEqual(len(provider2.calls), 0)
        self.assertTrue(any("退避" in n for n in summary.outcomes[0].notes))

    def test_429_without_retry_after_uses_project_policy(self) -> None:
        collector, _ = self.build({"alice": [make_failed_page("http_429", "429")]})
        outcome = collector.run_once().outcomes[0]
        until = parse_iso(self.storage.get_source_state("fxtwitter")["next_allowed_at"])
        # 本项目策略：初始 2 小时
        self.assertGreater(until, now_utc() + _dt.timedelta(minutes=110))
        self.assertTrue(any("本项目策略" in n for n in outcome.notes))

    def test_401_stops_without_trying_other_formats(self) -> None:
        collector, provider = self.build(
            {
                "alice": [make_failed_page("http_401_403", "HTTP 403")],
                "bob": [make_page([make_post("b1", handle="bob")])],
            }
        )
        summary = collector.run_once()
        self.assertEqual(summary.outcomes[0].status, RUN_FAILED)
        self.assertEqual(summary.outcomes[1].status, RUN_SKIPPED)
        self.assertTrue(any("不尝试绕过" in n for n in summary.outcomes[0].notes))


class BudgetTests(CollectorTestCase):
    handles = ("alice", "bob")

    def test_budget_exhaustion_defers_remaining_accounts(self) -> None:
        """达到总预算 → 未执行账号标记 deferred（方案 §8）。"""
        pages = [make_page([make_post(str(i), published_at=hours_ago(0.1))],
                           next_cursor="c%d" % i) for i in range(1, 4)]
        collector, provider = self.build({"alice": pages, "bob": []}, max_requests=3)
        summary = collector.run_once()
        self.assertEqual(summary.outcomes[1].status, RUN_DEFERRED)
        self.assertEqual(self.storage.get_account_state("bob")["deferred"], 1)
        self.assertEqual(len(provider.calls), 3)

    def test_deferred_account_goes_first_next_round(self) -> None:
        """下轮优先处理上次被延后的账号，防止列表尾部长期饥饿（方案 §8）。"""
        with self.storage.transaction():
            self.storage.ensure_account_state("bob", self.config.find_account("bob").scope_hash)
            self.storage.save_account_state("bob", deferred=1)
        collector, provider = self.build(
            {"alice": [make_page([])], "bob": [make_page([])]}
        )
        collector.run_once()
        self.assertEqual(provider.calls[0]["handle"], "bob")


class BackfillTests(CollectorTestCase):
    def test_backfill_does_not_touch_scan_boundary(self) -> None:
        boundary = self.seed_boundary(hours=1)
        collector, _ = self.build(
            {"alice": [make_page([make_post("old", published_at=hours_ago(200))])]}
        )
        outcome = collector.backfill("alice", hours_ago(300), hours_ago(100), max_pages=1)
        self.assertEqual(outcome.mode, "backfill")
        self.assertEqual(parse_iso(self.state()["last_scan_boundary_at"]), boundary)
        self.assertIsNotNone(self.storage.get_post("old"))

    def test_backfill_rejected_when_pagination_unverified(self) -> None:
        collector, _ = self.build({"alice": [make_page([])]}, supports_pagination=False)
        with self.assertRaises(ValueError) as ctx:
            collector.backfill("alice", hours_ago(300), hours_ago(100), max_pages=5)
        self.assertIn("不支持已验证的历史分页", str(ctx.exception))

    def test_late_arriving_old_post_is_still_stored(self) -> None:
        """故障后拿到早于最大 ID 的漏帖 → 仍能入库（方案 §14）。"""
        self.seed_boundary(hours=1)
        self.seed_known_post(make_post("999999", published_at=hours_ago(0.1)))
        collector, _ = self.build(
            {"alice": [make_page([make_post("111", published_at=hours_ago(100))])]}
        )
        outcome = collector.run_once().outcomes[0]
        self.assertEqual(outcome.new_count, 1)
        self.assertIsNotNone(self.storage.get_post("111"))


class ScopeChangeTests(CollectorTestCase):
    def test_scope_change_forces_fresh_coverage_verification(self) -> None:
        """切换数据源或启用回复 → 重新验证范围，不能沿用旧边界（方案 §14）。"""
        self.seed_boundary(hours=1)
        account = self.config.find_account("alice")
        account.scope_hash = "different-scope"
        collector, _ = self.build(
            {"alice": [make_page([make_post("1", published_at=hours_ago(100))])]}
        )
        outcome = collector.run_once().outcomes[0]
        self.assertEqual(outcome.mode, "bootstrap")  # 回到一次有限的初次导入
        self.assertTrue(any("scope_hash" in n for n in outcome.notes))


class GapTests(CollectorTestCase):
    def test_existing_gap_not_cleared_by_shallow_run(self) -> None:
        """存在缺口时，之后某轮只收到最新一页，不得自动清除旧缺口（方案 §6.5）。"""
        boundary = self.seed_boundary(hours=1)
        with self.storage.transaction():
            self.storage.save_account_state(
                "alice",
                gap_from_at=to_iso(hours_ago(200)),
                gap_to_at=to_iso(hours_ago(100)),
            )
        target = boundary - _dt.timedelta(hours=6)
        known = make_post("known", published_at=target - _dt.timedelta(hours=1))
        self.seed_known_post(known)
        posts = [known, make_post("x", published_at=target - _dt.timedelta(hours=2))]
        collector, _ = self.build({"alice": [make_page(posts, next_cursor=None)]})
        outcome = collector.run_once().outcomes[0]
        self.assertEqual(outcome.status, RUN_SUCCESS)
        row = self.state()
        self.assertIsNotNone(row["gap_from_at"])  # 旧缺口还在
        self.assertTrue(any("保留既有缺口" in n for n in outcome.notes))

    def test_deep_run_clears_covered_gap(self) -> None:
        boundary = self.seed_boundary(hours=1)
        with self.storage.transaction():
            self.storage.save_account_state(
                "alice", gap_from_at=to_iso(hours_ago(50)), gap_to_at=to_iso(hours_ago(40))
            )
        target = boundary - _dt.timedelta(hours=6)
        known = make_post("known", published_at=target - _dt.timedelta(hours=1))
        self.seed_known_post(known)
        posts = [known, make_post("deep", published_at=hours_ago(100))]
        collector, _ = self.build({"alice": [make_page(posts, next_cursor=None)]})
        outcome = collector.run_once().outcomes[0]
        self.assertIsNone(self.state()["gap_from_at"])
        self.assertTrue(any("清除缺口记录" in n for n in outcome.notes))


class LockTests(CollectorTestCase):
    def test_second_instance_cannot_acquire(self) -> None:
        """已有进程持有锁 → 本轮不重复执行（方案 §10）。"""
        with SingleInstanceLock(self.config.lock_path):
            with self.assertRaises(LockBusy):
                SingleInstanceLock(self.config.lock_path).acquire()

    def test_lock_released_after_use(self) -> None:
        with SingleInstanceLock(self.config.lock_path):
            pass
        with SingleInstanceLock(self.config.lock_path):
            pass  # 不抛异常即通过


if __name__ == "__main__":
    unittest.main()
