"""单轮采集与覆盖判定（方案 §6）。

两条贯穿全文的原则：

1. **每轮都从最新一页开始**，先把拿到的有效数据存下来，再判断要不要往历史翻页。
   绝不是"只记最大帖子 ID 然后丢掉更小的"。
2. **运行状态与覆盖结论分开**。请求成功 ≠ 完整性得到证明；退出码 0 不代表没遗漏。

阶段 0 的实测结果直接决定了停止条件的写法：上游时间线**按对话聚合、不是严格倒序**
（见 docs/source-validation.md §B），所以不能"遇到一条更旧的帖子就停"，只能用整轮
可信自有帖的时间下界 + 多条证据来判断。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import uuid
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .config import Account, Config
from .httpclient import HARD_STOP_KINDS, BudgetExhausted, HttpClient
from .logsetup import get_logger
from .normalize import NormalizedPost, usable_self_times
from .providers import FetchPage, Provider, build_provider
from .storage import Storage
from .util import ensure_dir, is_safe_filename_token, now_utc, parse_iso, to_iso

# 覆盖状态（方案 §6.4）
COVERAGE_BOOTSTRAP = "bootstrap"
COVERAGE_OVERLAP = "overlap_observed"
COVERAGE_EXHAUSTED = "source_exhausted"
COVERAGE_LIMITED = "limited"
COVERAGE_UNKNOWN = "unknown"

# 运行状态
RUN_SUCCESS = "success"
RUN_PARTIAL = "partial"
RUN_FAILED = "failed"
RUN_SKIPPED = "skipped"
RUN_DEFERRED = "deferred"

# 覆盖结论可以推进扫描边界的状态
_ADVANCING_COVERAGE = (COVERAGE_BOOTSTRAP, COVERAGE_OVERLAP, COVERAGE_EXHAUSTED)

# 判定时间下界需要的最少证据条数。
# 方案 §6.4.2：置顶帖不得**单独**触发停止 —— 一条很旧的置顶帖是单点异常，
# 要求两条独立的可信自有帖才承认"已经翻到这么旧"。
MIN_BOUNDARY_EVIDENCE = 2

# 没有 Retry-After 的 429，本项目自己的保护策略（方案 §10）
DEFAULT_429_BACKOFF_SECONDS = 2 * 3600
MAX_429_BACKOFF_SECONDS = 12 * 3600
# 401/403/验证码：停下来报告，不绕过。给一个冷静期避免反复撞墙。
HARD_STOP_BACKOFF_SECONDS = 3600
# 连续失败告警阈值（方案 §10）
FAILURE_ALERT_THRESHOLD = 3


class AccountOutcome:
    """单个账号本轮的结果。"""

    __slots__ = (
        "handle",
        "mode",
        "status",
        "coverage",
        "pages_fetched",
        "returned_count",
        "valid_count",
        "new_count",
        "updated_count",
        "seen_count",
        "parse_failures",
        "error_kind",
        "error_detail",
        "notes",
        "affected_post_ids",
        "oldest_self_seen",
        "newest_self_seen",
        "boundary_advanced",
        "gap_from",
        "gap_to",
        "started_at",
    )

    def __init__(self, handle: str, mode: str, started_at: _dt.datetime) -> None:
        self.handle = handle
        self.mode = mode
        self.started_at = started_at
        self.status = RUN_FAILED
        self.coverage = COVERAGE_UNKNOWN
        self.pages_fetched = 0
        self.returned_count = 0
        self.valid_count = 0
        self.new_count = 0
        self.updated_count = 0
        self.seen_count = 0
        self.parse_failures = 0
        self.error_kind: Optional[str] = None
        self.error_detail = ""
        self.notes: List[str] = []
        self.affected_post_ids: List[str] = []
        self.oldest_self_seen: Optional[_dt.datetime] = None
        self.newest_self_seen: Optional[_dt.datetime] = None
        self.boundary_advanced = False
        self.gap_from: Optional[_dt.datetime] = None
        self.gap_to: Optional[_dt.datetime] = None

    def note(self, text: str) -> None:
        self.notes.append(text)

    def summary_line(self) -> str:
        return (
            "@%s %s/%s 页=%d 返回=%d 新增=%d 内容更新=%d 已知=%d 解析失败=%d%s"
            % (
                self.handle,
                self.status,
                self.coverage,
                self.pages_fetched,
                self.returned_count,
                self.new_count,
                self.updated_count,
                self.seen_count,
                self.parse_failures,
                "" if not self.error_kind else " 错误=%s" % self.error_kind,
            )
        )


class RunSummary:
    def __init__(self, run_uid: str, started_at: _dt.datetime) -> None:
        self.run_uid = run_uid
        self.started_at = started_at
        self.outcomes: List[AccountOutcome] = []
        self.notes: List[str] = []
        self.aborted_reason: Optional[str] = None

    @property
    def affected_post_ids(self) -> List[str]:
        seen: Set[str] = set()
        out: List[str] = []
        for outcome in self.outcomes:
            for post_id in outcome.affected_post_ids:
                if post_id not in seen:
                    seen.add(post_id)
                    out.append(post_id)
        return out

    @property
    def worst_status(self) -> str:
        """最差的账号状态决定进程退出码（方案 §11.1）。"""
        order = [RUN_FAILED, RUN_PARTIAL, RUN_DEFERRED, RUN_SKIPPED, RUN_SUCCESS]
        statuses = {o.status for o in self.outcomes}
        for status in order:
            if status in statuses:
                return status
        return RUN_SUCCESS

    def totals(self) -> Dict[str, int]:
        return {
            "accounts": len(self.outcomes),
            "new": sum(o.new_count for o in self.outcomes),
            "updated": sum(o.updated_count for o in self.outcomes),
            "pages": sum(o.pages_fetched for o in self.outcomes),
            "parse_failures": sum(o.parse_failures for o in self.outcomes),
        }


class Collector:
    def __init__(
        self,
        config: Config,
        storage: Storage,
        client: Optional[HttpClient] = None,
        provider: Optional[Provider] = None,
    ) -> None:
        self.config = config
        self.storage = storage
        self.log = get_logger()
        collection = config.collection
        self.client = client or HttpClient(
            user_agent=config.source["user_agent"],
            connect_timeout=collection["connect_timeout_seconds"],
            read_timeout=collection["read_timeout_seconds"],
            max_attempts=collection["max_attempts_per_request"],
            max_total_requests=collection["max_total_requests_per_run"],
            max_run_seconds=collection["max_run_seconds"],
        )
        self.provider = provider or build_provider(
            config.source["provider"], self.client, config.source
        )

    # --- 入口 ---------------------------------------------------------------
    def run_once(self, handles: Optional[Sequence[str]] = None) -> RunSummary:
        run_uid = uuid.uuid4().hex[:16]
        summary = RunSummary(run_uid, now_utc())
        accounts = self._select_accounts(handles)
        self.log.info(
            "本轮 run_uid=%s，账号 %d 个，来源 %s，请求预算 %d",
            run_uid,
            len(accounts),
            self.provider.name,
            self.config.collection["max_total_requests_per_run"],
        )

        for index, account in enumerate(accounts):
            blocked = self._source_blocked_until()
            if blocked is not None:
                reason = "上游 %s 处于退避中，下次允许时间 %s" % (
                    self.provider.source_key,
                    to_iso(blocked),
                )
                self._mark_remaining(summary, accounts[index:], run_uid, RUN_SKIPPED, reason)
                summary.aborted_reason = reason
                break

            if self.client.requests_left() <= 0 or self.client.seconds_left() <= 0:
                reason = "本轮预算用尽（请求 %d/%d，剩余 %.0f 秒）" % (
                    self.client.requests_used,
                    self.client.max_total_requests,
                    self.client.seconds_left(),
                )
                self._mark_remaining(summary, accounts[index:], run_uid, RUN_DEFERRED, reason)
                summary.notes.append(reason)
                break

            if index > 0:
                # 串行处理，不并发轰击接口（方案 §2）
                self.client.sleep_within_budget(self.config.collection["request_gap_seconds"])

            outcome = self._collect_account(account, run_uid)
            summary.outcomes.append(outcome)
            self.log.info(outcome.summary_line())

            if outcome.error_kind == "http_429" or outcome.error_kind in HARD_STOP_KINDS:
                # 一次 429 影响同源所有账号，不能只跳过当前账号继续请求其余账号（方案 §10）
                remaining = accounts[index + 1 :]
                if remaining:
                    reason = "因 %s 暂停本轮同源剩余账号" % outcome.error_kind
                    self._mark_remaining(summary, remaining, run_uid, RUN_SKIPPED, reason)
                    summary.aborted_reason = reason
                break

        self._report_alerts(summary)
        return summary

    def backfill(
        self, handle: str, since: _dt.datetime, until: _dt.datetime, max_pages: int
    ) -> AccountOutcome:
        """有界补漏（方案 §6.5 / §11.1）。

        不承诺一定能取回该时间段。源不支持历史分页时明确拒绝，而不是默默只抓最新一页。
        """
        account = self.config.find_account(handle)
        if account is None:
            raise ValueError("配置中没有账号 %r" % handle)
        if not self.provider.supports_pagination:
            raise ValueError(
                "当前 provider %s 不支持已验证的历史分页，拒绝执行 backfill"
                % self.provider.name
            )
        run_uid = uuid.uuid4().hex[:16]
        return self._collect_account(
            account,
            run_uid,
            forced_mode="backfill",
            page_budget=max_pages,
            target_time=since,
            backfill_until=until,
        )

    # --- 账号排序与预算 -----------------------------------------------------
    def _select_accounts(self, handles: Optional[Sequence[str]]) -> List[Account]:
        if handles:
            selected: List[Account] = []
            for raw in handles:
                account = self.config.find_account(raw)
                if account is None:
                    raise ValueError("配置中没有账号 %r" % raw)
                selected.append(account)
            return selected

        enabled = self.config.enabled_accounts()
        # 上轮被延后的账号本轮优先，防止列表尾部长期饥饿（方案 §8）
        deferred_first: List[Account] = []
        rest: List[Account] = []
        for account in enabled:
            state = self.storage.get_account_state(account.handle)
            if state is not None and int(state["deferred"] or 0):
                deferred_first.append(account)
            else:
                rest.append(account)
        if deferred_first:
            self.log.info(
                "优先处理上轮被延后的账号：%s",
                ", ".join("@" + a.handle for a in deferred_first),
            )
        return deferred_first + rest

    def _mark_remaining(
        self,
        summary: RunSummary,
        accounts: Sequence[Account],
        run_uid: str,
        status: str,
        reason: str,
    ) -> None:
        for account in accounts:
            outcome = AccountOutcome(account.handle, "skipped", now_utc())
            outcome.status = status
            outcome.coverage = COVERAGE_UNKNOWN
            outcome.note(reason)
            summary.outcomes.append(outcome)
            with self.storage.transaction():
                self.storage.ensure_account_state(account.handle, account.scope_hash)
                run_id = self.storage.start_run(
                    run_uid, account.handle, outcome.started_at, self.provider.name, "skipped"
                )
                self.storage.finish_run(
                    run_id,
                    finished_at=to_iso(now_utc()),
                    status=status,
                    coverage_status=COVERAGE_UNKNOWN,
                    error_detail=reason,
                    notes_json=json.dumps(outcome.notes, ensure_ascii=False),
                )
                self.storage.save_account_state(
                    account.handle, deferred=1 if status == RUN_DEFERRED else 0
                )
            self.log.warning("@%s %s：%s", account.handle, status, reason)

    def _source_blocked_until(self) -> Optional[_dt.datetime]:
        row = self.storage.get_source_state(self.provider.source_key)
        if row is None:
            return None
        until = parse_iso(row["next_allowed_at"])
        if until is None:
            return None
        if until <= now_utc():
            with self.storage.transaction():
                self.storage.clear_source_backoff(self.provider.source_key)
            return None
        return until

    # --- 单账号采集 ---------------------------------------------------------
    def _collect_account(
        self,
        account: Account,
        run_uid: str,
        forced_mode: Optional[str] = None,
        page_budget: Optional[int] = None,
        target_time: Optional[_dt.datetime] = None,
        backfill_until: Optional[_dt.datetime] = None,
    ) -> AccountOutcome:
        collection = self.config.collection
        handle = account.handle
        started_at = now_utc()

        with self.storage.transaction():
            state = self.storage.ensure_account_state(handle, account.scope_hash)

        # 账号级退避（方案 §6.3：先检查是否处于退避或暂停状态）
        account_block = parse_iso(state.get("next_allowed_at"))
        if forced_mode is None and account_block is not None and account_block > started_at:
            outcome = AccountOutcome(handle, "skipped", started_at)
            outcome.status = RUN_SKIPPED
            outcome.note("账号处于退避中，下次允许时间 %s" % to_iso(account_block))
            self._record_run(run_uid, outcome, state)
            return outcome

        boundary = parse_iso(state.get("last_scan_boundary_at"))
        bootstrap_done = bool(int(state.get("bootstrap_done") or 0))
        scope_changed = bool(state.get("scope_changed"))

        # 模式与页数预算
        if forced_mode == "backfill":
            mode = "backfill"
            budget = page_budget or collection["max_pages_per_account"]
        elif not bootstrap_done or boundary is None:
            # 首次导入，或采集范围变了导致旧边界作废 —— 都按一次有限的初次导入处理
            mode = "bootstrap"
            budget = collection["bootstrap_max_pages"]
            target_time = started_at - _dt.timedelta(hours=collection["bootstrap_hours"])
        else:
            mode = "incremental"
            budget = collection["max_pages_per_account"]
            # 扫到"上次已确认边界之前 overlap_hours"，而不是遇到旧记录就结束（方案 §6.4.3）
            target_time = boundary - _dt.timedelta(hours=collection["overlap_hours"])

        outcome = AccountOutcome(handle, mode, started_at)
        if scope_changed:
            outcome.note("采集范围（scope_hash）已变化，旧扫描边界作废，本轮重新做覆盖验证")
        if mode == "bootstrap" and not bootstrap_done:
            outcome.note(
                "首次导入：目标窗口 %d 小时 / 最多 %d 页，不承诺导入完整历史"
                % (collection["bootstrap_hours"], budget)
            )

        with self.storage.transaction():
            self.storage.save_account_state(handle, last_attempt_at=to_iso(started_at))

        # 本轮之前的已知帖子集合 —— 重叠锚点必须取自这里（方案 §6.4.1）
        known_before = self.storage.known_post_ids(handle)
        run_id = self._begin_run(run_uid, handle, started_at, mode)

        cursor: Optional[str] = None
        seen_cursors: Set[str] = set()
        page_index = 0
        had_failure = False
        got_any_page = False
        reseen_known = 0
        all_self_times: List[_dt.datetime] = []
        self_evidence_total = 0
        stop_reason = "未开始"

        while page_index < budget:
            try:
                self.client.check_budget(1)
            except BudgetExhausted as exc:
                stop_reason = "预算中断：%s" % exc.reason
                outcome.note(stop_reason)
                had_failure = had_failure or False
                outcome.coverage = COVERAGE_LIMITED
                break

            if page_index > 0:
                if not self.client.sleep_within_budget(collection["request_gap_seconds"]):
                    stop_reason = "剩余运行时长不足，停止分页"
                    outcome.note(stop_reason)
                    outcome.coverage = COVERAGE_LIMITED
                    break

            page = self.provider.fetch_page(handle, cursor)
            page_index += 1

            if not page.ok:
                had_failure = True
                outcome.error_kind = page.error_kind
                outcome.error_detail = page.error_detail
                outcome.note("第 %d 页失败：%s / %s" % (page_index, page.error_kind, page.error_detail))
                self._save_diagnostic(handle, page_index, page)
                self._handle_page_failure(handle, page, outcome)
                stop_reason = "分页中断于失败"
                break

            got_any_page = True
            outcome.pages_fetched += 1
            outcome.returned_count += page.returned_count
            outcome.parse_failures += page.parse_failures
            for warning in page.warnings:
                outcome.note("第 %d 页提示：%s" % (page_index, warning))

            self._save_raw(handle, page_index, page)

            with self.storage.transaction():
                self.storage.save_account_state(
                    handle, last_valid_response_at=to_iso(page.fetched_at)
                )
                stats = self._store_page(handle, page, run_uid, known_before, mode)
            outcome.new_count += stats["new"]
            outcome.updated_count += stats["updated"]
            outcome.seen_count += stats["seen"]
            outcome.valid_count += stats["valid"]
            outcome.affected_post_ids.extend(stats["affected"])
            reseen_known += stats["reseen_known"]

            # 累计**全部**可信自有帖的时间，而不是每页只留 min/max ——
            # 证据条数要真实，否则"两条证据"这条防置顶帖的规则会变成每轮多取一页。
            page_self_times = usable_self_times(page.posts)
            all_self_times.extend(page_self_times)
            self_evidence_total += len(page_self_times)

            # 判断是否可以停止
            decision = self._should_stop(
                mode=mode,
                posts_pages_self_times=all_self_times,
                page_posts=page.posts,
                target_time=target_time,
                reseen_known=reseen_known,
                page=page,
                cursor=cursor,
                seen_cursors=seen_cursors,
            )
            stop_reason = decision["reason"]
            if decision["stop"]:
                outcome.coverage = decision["coverage"]
                outcome.note("停止分页：%s" % stop_reason)
                break

            if not page.next_cursor:
                outcome.coverage = COVERAGE_EXHAUSTED if page.supports_pagination else COVERAGE_LIMITED
                stop_reason = (
                    "上游未返回下一页游标（该源已到末页）"
                    if page.supports_pagination
                    else "该源不支持已验证的分页，只能取到最近内容"
                )
                outcome.note("停止分页：%s" % stop_reason)
                break

            if page.next_cursor in seen_cursors:
                # 重复游标 = 分页循环，立刻停止并标记 limited（方案 §6.4.5）
                outcome.coverage = COVERAGE_LIMITED
                stop_reason = "出现重复游标，判定为分页循环"
                outcome.note("停止分页：%s" % stop_reason)
                break

            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor
        else:
            outcome.coverage = COVERAGE_LIMITED
            stop_reason = "达到页数上限 %d，未能确认覆盖" % budget
            outcome.note(stop_reason)

        # --- 结论 ---
        oldest_self = min(all_self_times) if all_self_times else None
        newest_self = max(all_self_times) if all_self_times else None
        outcome.oldest_self_seen = oldest_self
        outcome.newest_self_seen = newest_self

        if not got_any_page:
            outcome.status = RUN_FAILED
            outcome.coverage = COVERAGE_UNKNOWN
        elif had_failure:
            # 第一页成功、第二页失败 → 保留已入库数据，标记 partial（方案 §14）。
            # 这里至少取到了一页有效数据，所以覆盖结论是 limited（无法确认覆盖），
            # 而不是 unknown（证据不足无法判断）。两者都不会推进边界。
            outcome.status = RUN_PARTIAL
            if outcome.coverage in _ADVANCING_COVERAGE or outcome.coverage == COVERAGE_UNKNOWN:
                outcome.coverage = COVERAGE_LIMITED
        elif outcome.parse_failures > 0:
            # 有条目解析失败 → partial，不静默推进边界（方案 §10）
            outcome.status = RUN_PARTIAL
            if outcome.coverage in _ADVANCING_COVERAGE:
                outcome.coverage = COVERAGE_LIMITED
        else:
            outcome.status = RUN_SUCCESS

        if mode == "bootstrap" and outcome.status == RUN_SUCCESS:
            outcome.coverage = COVERAGE_BOOTSTRAP

        if mode == "backfill":
            outcome.note(
                "backfill 请求范围 %s .. %s（不保证该区间被完整取回）"
                % (to_iso(target_time), to_iso(backfill_until))
            )
            self._finish_run(run_id, outcome)
            return outcome

        self._apply_state(handle, state, outcome, target_time, oldest_self, mode)
        self._finish_run(run_id, outcome)
        return outcome

    # --- 停止条件 -----------------------------------------------------------
    def _should_stop(
        self,
        mode: str,
        posts_pages_self_times: List[_dt.datetime],
        page_posts: List[NormalizedPost],
        target_time: Optional[_dt.datetime],
        reseen_known: int,
        page: FetchPage,
        cursor: Optional[str],
        seen_cursors: Set[str],
    ) -> Dict[str, Any]:
        """只在**有证据**时停止（方案 §6.4）。

        参与判断的只有"时间线里该账号自己发的、类型明确、时间可解析"的帖子；
        置顶/转帖/引用原帖/未知类型都不算 —— 转帖的发布时间是原帖时间，用它判边界
        会直接误判。
        """
        if not page.supports_pagination:
            return {
                "stop": True,
                "coverage": COVERAGE_LIMITED,
                "reason": "%s 不支持已验证的分页" % page.source,
            }
        if target_time is None:
            return {"stop": False, "coverage": COVERAGE_UNKNOWN, "reason": "无时间目标，继续按预算取页"}

        # 累计所有页里"比目标时间更旧"的可信自有帖数量
        older = [t for t in posts_pages_self_times if t <= target_time]
        if len(older) < MIN_BOUNDARY_EVIDENCE:
            return {
                "stop": False,
                "coverage": COVERAGE_UNKNOWN,
                "reason": "仅 %d 条可信自有帖早于目标时间 %s，证据不足（需 %d 条，防止置顶帖误判）"
                % (len(older), to_iso(target_time), MIN_BOUNDARY_EVIDENCE),
            }

        if mode == "bootstrap":
            return {
                "stop": True,
                "coverage": COVERAGE_BOOTSTRAP,
                "reason": "已回溯到首次导入目标窗口 %s" % to_iso(target_time),
            }

        if reseen_known <= 0:
            # 时间上够旧了，但一条已知帖都没重新见到 —— 不能声称重叠成立
            return {
                "stop": False,
                "coverage": COVERAGE_UNKNOWN,
                "reason": "已回溯到 %s，但未重新见到任何本轮之前的已知帖子，重叠未成立"
                % to_iso(target_time),
            }

        return {
            "stop": True,
            "coverage": COVERAGE_OVERLAP,
            "reason": "已覆盖重叠目标 %s 且重新见到 %d 条已知帖子"
            % (to_iso(target_time), reseen_known),
        }

    # --- 入库 ---------------------------------------------------------------
    def _store_page(
        self,
        handle: str,
        page: FetchPage,
        run_uid: str,
        known_before: Set[str],
        mode: str,
    ) -> Dict[str, Any]:
        """把一页标准化结果写库。调用方已开启事务。

        新增量按**实际插入结果**统计，不用返回条数代替（方案 §7.4）。
        """
        stats = {
            "new": 0,
            "updated": 0,
            "seen": 0,
            "valid": 0,
            "reseen_known": 0,
            "affected": [],
        }
        is_bootstrap = mode == "bootstrap"
        for post in page.posts:
            stats["valid"] += 1
            action = self.storage.upsert_post(post, page.fetched_at)
            if action == "inserted":
                stats["new"] += 1
                stats["affected"].append(post.post_id)
            elif action == "content_updated":
                stats["updated"] += 1
                stats["affected"].append(post.post_id)
            else:
                stats["seen"] += 1

            link = self.storage.upsert_account_post(
                handle,
                post.post_id,
                post.relation,
                page.fetched_at,
                run_uid,
                is_bootstrap=is_bootstrap,
            )
            if link == "linked" and post.post_id not in stats["affected"]:
                # 帖子已存在（别的监控账号带出来的），但本账号的关联是新的
                stats["affected"].append(post.post_id)
            if post.post_id in known_before:
                stats["reseen_known"] += 1
        return stats

    # --- 失败处理 -----------------------------------------------------------
    def _handle_page_failure(self, handle: str, page: FetchPage, outcome: AccountOutcome) -> None:
        kind = page.error_kind or "unknown"
        if kind == "http_429":
            retry_after = page.retry_after_seconds
            row = self.storage.get_source_state(self.provider.source_key)
            streak = int(row["consecutive_throttle"]) if row else 0
            if retry_after is None:
                # 本项目的保护策略：初始 2 小时，连续出现再加长
                seconds = min(
                    DEFAULT_429_BACKOFF_SECONDS * (2 ** streak), MAX_429_BACKOFF_SECONDS
                )
                note = "429 未带 Retry-After，按本项目策略退避 %d 秒" % seconds
            else:
                seconds = max(retry_after, 1)
                note = "429 带 Retry-After=%d 秒，按上游要求退避" % seconds
            until = now_utc() + _dt.timedelta(seconds=seconds)
            outcome.note(note)
            with self.storage.transaction():
                self.storage.set_source_backoff(self.provider.source_key, until, note)
            self.log.warning("上游 %s 退避至 %s：%s", self.provider.source_key, to_iso(until), note)
            return

        if kind in HARD_STOP_KINDS:
            # 401/403/验证码/登录页：停止并明确报告，不绕过、不换格式（方案 §10）
            until = now_utc() + _dt.timedelta(seconds=HARD_STOP_BACKOFF_SECONDS)
            note = "遇到 %s，停止请求并报告，不尝试绕过；冷静期至 %s" % (kind, to_iso(until))
            outcome.note(note)
            with self.storage.transaction():
                self.storage.set_source_backoff(self.provider.source_key, until, note)
            self.log.error("@%s %s", handle, note)
            return

        if kind == "http_404":
            # 不武断判断是删号、改名还是上游错误（方案 §10）
            outcome.note("账号当前不可访问（HTTP 404）；未判断是删号、改名还是上游错误")
            return

        if kind in ("content_type", "parse", "structure"):
            outcome.note("响应结构异常，已保存诊断样本；记为失败，不记作“没有新帖”")
            return

    def _report_alerts(self, summary: RunSummary) -> None:
        for outcome in summary.outcomes:
            state = self.storage.get_account_state(outcome.handle)
            if state is None:
                continue
            failures = int(state["consecutive_failures"] or 0)
            if failures >= FAILURE_ALERT_THRESHOLD:
                message = "@%s 连续失败 %d 次（阈值 %d）。告警只写入日志与每日索引，未配置任何外部通知。" % (
                    outcome.handle,
                    failures,
                    FAILURE_ALERT_THRESHOLD,
                )
                summary.notes.append(message)
                self.log.error(message)

    # --- 状态推进 -----------------------------------------------------------
    def _apply_state(
        self,
        handle: str,
        state: Dict[str, Any],
        outcome: AccountOutcome,
        target_time: Optional[_dt.datetime],
        oldest_self: Optional[_dt.datetime],
        mode: str,
    ) -> None:
        """写回账号状态。**只有符合条件才推进扫描边界**（方案 §6.3 / §6.5）。"""
        fields: Dict[str, Any] = {"deferred": 0, "coverage_status": outcome.coverage}
        existing_gap_from = parse_iso(state.get("gap_from_at"))
        existing_gap_to = parse_iso(state.get("gap_to_at"))

        if outcome.status in (RUN_FAILED,):
            failures = int(state.get("consecutive_failures") or 0) + 1
            fields["consecutive_failures"] = failures
            if failures >= FAILURE_ALERT_THRESHOLD:
                # 连续失败后给账号一个递增退避，避免每小时重复撞同一个墙
                minutes = min(
                    self.config.collection["interval_minutes"] * failures, 6 * 60
                )
                fields["next_allowed_at"] = to_iso(
                    now_utc() + _dt.timedelta(minutes=minutes)
                )
                outcome.note("连续失败 %d 次，账号退避 %d 分钟" % (failures, minutes))
            # 失败不推进边界，也不清除既有缺口
            outcome.gap_from, outcome.gap_to = existing_gap_from, existing_gap_to
            with self.storage.transaction():
                self.storage.save_account_state(handle, **fields)
            return

        fields["consecutive_failures"] = 0
        fields["next_allowed_at"] = None
        if mode == "bootstrap":
            fields["bootstrap_done"] = 1

        advance = outcome.status == RUN_SUCCESS and outcome.coverage in _ADVANCING_COVERAGE
        if advance:
            # 边界用成功一轮的**开始时间**，不是最大帖子发布时间，也不是任务结束时间（方案 §6.5）
            fields["last_scan_boundary_at"] = to_iso(outcome.started_at)
            outcome.boundary_advanced = True
        else:
            outcome.note("覆盖结论为 %s，本轮不推进扫描边界" % outcome.coverage)

        # --- 缺口 ---
        gap_from, gap_to = existing_gap_from, existing_gap_to
        desired_from = target_time
        reached_from = oldest_self

        unresolved: Optional[Tuple[_dt.datetime, _dt.datetime]] = None
        if desired_from is not None:
            if reached_from is None:
                if outcome.coverage not in (COVERAGE_EXHAUSTED,):
                    unresolved = (desired_from, outcome.started_at)
            elif reached_from > desired_from and outcome.coverage != COVERAGE_EXHAUSTED:
                unresolved = (desired_from, reached_from)

        if unresolved is not None:
            new_from, new_to = unresolved
            gap_from = new_from if gap_from is None else min(gap_from, new_from)
            gap_to = new_to if gap_to is None else max(gap_to, new_to)
            outcome.note(
                "记录疑似缺口 %s .. %s（未达到目标回溯时间）" % (to_iso(gap_from), to_iso(gap_to))
            )
        elif gap_from is not None:
            # 只有真的扫回到旧缺口起点之前，才允许清除它。
            # 之后某轮只收到最新一页，不得自动清除旧缺口（方案 §6.5）。
            if reached_from is not None and reached_from <= gap_from:
                outcome.note("本轮已回溯到 %s，覆盖旧缺口，清除缺口记录" % to_iso(reached_from))
                gap_from, gap_to = None, None
            else:
                outcome.note(
                    "保留既有缺口 %s .. %s（本轮未回溯到该区间）"
                    % (to_iso(gap_from), to_iso(gap_to))
                )

        fields["gap_from_at"] = to_iso(gap_from)
        fields["gap_to_at"] = to_iso(gap_to)
        outcome.gap_from, outcome.gap_to = gap_from, gap_to

        with self.storage.transaction():
            self.storage.save_account_state(handle, **fields)

    # --- 运行记录 -----------------------------------------------------------
    def _begin_run(
        self, run_uid: str, handle: str, started_at: _dt.datetime, mode: str
    ) -> int:
        with self.storage.transaction():
            return self.storage.start_run(
                run_uid, handle, started_at, self.provider.name, mode
            )

    def _finish_run(self, run_id: int, outcome: AccountOutcome) -> None:
        with self.storage.transaction():
            self.storage.finish_run(
                run_id,
                finished_at=to_iso(now_utc()),
                requests_attempted=self.client.requests_used,
                pages_fetched=outcome.pages_fetched,
                returned_count=outcome.returned_count,
                valid_count=outcome.valid_count,
                new_count=outcome.new_count,
                updated_count=outcome.updated_count,
                parse_failures=outcome.parse_failures,
                coverage_status=outcome.coverage,
                status=outcome.status,
                error_kind=outcome.error_kind,
                error_detail=outcome.error_detail[:2000] if outcome.error_detail else None,
                notes_json=json.dumps(outcome.notes, ensure_ascii=False),
            )

    def _record_run(
        self, run_uid: str, outcome: AccountOutcome, state: Dict[str, Any]
    ) -> None:
        run_id = self._begin_run(run_uid, outcome.handle, outcome.started_at, outcome.mode)
        self._finish_run(run_id, outcome)

    # --- 原始响应落盘 -------------------------------------------------------
    def _save_raw(self, handle: str, page_index: int, page: FetchPage) -> None:
        """限期保留的接口响应（方案 §3.2 / §12）。失败不影响采集。"""
        if not is_safe_filename_token(handle):
            return
        raw = None
        if page.posts:
            raw = [p.raw for p in page.posts if p.raw is not None]
        if not raw:
            return
        try:
            ensure_dir(self.config.raw_dir)
            stamp = page.fetched_at.strftime("%Y%m%dT%H%M%SZ")
            path = os.path.join(
                self.config.raw_dir, "%s-%s-p%d.json" % (handle, stamp, page_index)
            )
            payload = {
                "source": page.source,
                "fetched_at": to_iso(page.fetched_at),
                "monitored_handle": handle,
                "page_index": page_index,
                "returned_count": page.returned_count,
                "next_cursor_present": bool(page.next_cursor),
                "warnings": page.warnings,
                "items": raw,
            }
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, sort_keys=True)
        except (OSError, TypeError, ValueError) as exc:
            self.log.warning("保存原始响应失败：%s", exc)

    def _save_diagnostic(self, handle: str, page_index: int, page: FetchPage) -> None:
        """失败时保存有限诊断样本（方案 §10）。"""
        if not is_safe_filename_token(handle):
            return
        try:
            ensure_dir(self.config.raw_dir)
            stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
            path = os.path.join(
                self.config.raw_dir, "%s-%s-p%d-error.txt" % (handle, stamp, page_index)
            )
            lines = [
                "monitored_handle: %s" % handle,
                "source: %s" % page.source,
                "page_index: %d" % page_index,
                "http_status: %s" % page.http_status,
                "error_kind: %s" % page.error_kind,
                "retry_after_seconds: %s" % page.retry_after_seconds,
                "error_detail: %s" % page.error_detail[:4000],
                "warnings: %s" % "; ".join(page.warnings),
            ]
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError as exc:
            self.log.warning("保存诊断样本失败：%s", exc)
