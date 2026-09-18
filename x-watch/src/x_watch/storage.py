"""SQLite 存储层（方案 §7）。

SQLite 是唯一事实来源；Markdown 是可以随时重建的阅读输出。

写入一律用参数化 SQL + 短事务 + 显式提交。方案 §7 说“四张核心表即可”，这里多了
两张：`source_state`（§10 要求的同源退避状态）与 `schema_meta`（迁移用）。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .normalize import (
    RELATION_CONTEXT,
    RELATION_QUOTED,
    RELATION_REPOST,
    RELATION_SELF,
    RELATION_UNKNOWN,
    TYPE_ORIGINAL,
    TYPE_QUOTE,
    TYPE_REPLY,
    TYPE_UNKNOWN,
    NormalizedPost,
    content_hash_from_parts,
)
from .util import ensure_dir, now_utc, to_iso

SCHEMA_VERSION = 4  # v4 增加 post_translations（帖子正文中文译文）

# 关联类型的证据强度。只允许向上覆盖：先看到 context 后拿到 repost 证据要升级，
# 反过来不行（方案 §7.2：没有明确证据不得断言）。
_RELATION_RANK = {
    RELATION_UNKNOWN: 0,
    RELATION_QUOTED: 1,
    RELATION_CONTEXT: 2,
    RELATION_REPOST: 3,
    RELATION_SELF: 3,
}

# 数据源可信度。RSS 字段少，不得把 JSON 存下的完整内容降级覆盖（方案 §7.1）。
_SOURCE_RANK = {"fxtwitter_rss": 1, "fxtwitter_json": 2}

# 帖子类型的"具体程度"。reply/quote 由结构存在推导出来，original 由结构缺失推导，
# 所以 original 不能覆盖 reply/quote。详见 _merge_fields 的说明。
_TYPE_SPECIFICITY = {
    TYPE_UNKNOWN: 0,
    TYPE_ORIGINAL: 1,
    TYPE_QUOTE: 2,
    TYPE_REPLY: 2,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
    post_id            TEXT PRIMARY KEY,
    author_id          TEXT,
    author_handle      TEXT NOT NULL,
    url                TEXT NOT NULL,
    text               TEXT NOT NULL,
    published_at       TEXT,
    post_type          TEXT NOT NULL,
    reply_to_id        TEXT,
    quote_post_id      TEXT,
    media_json         TEXT,
    metrics_json       TEXT,
    content_hash       TEXT NOT NULL,
    first_seen_at      TEXT NOT NULL,
    last_seen_at       TEXT NOT NULL,
    content_updated_at TEXT,
    source             TEXT NOT NULL,
    raw_json           TEXT,
    content_status     TEXT NOT NULL,
    warnings_json      TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_first_seen  ON posts(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_posts_published   ON posts(published_at);
CREATE INDEX IF NOT EXISTS idx_posts_author      ON posts(author_handle);

CREATE TABLE IF NOT EXISTS account_posts (
    monitored_handle TEXT NOT NULL,
    post_id          TEXT NOT NULL,
    relation         TEXT NOT NULL,
    is_bootstrap     INTEGER NOT NULL DEFAULT 0,
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    first_run_uid    TEXT,
    PRIMARY KEY (monitored_handle, post_id),
    FOREIGN KEY (post_id) REFERENCES posts(post_id)
);
CREATE INDEX IF NOT EXISTS idx_account_posts_post ON account_posts(post_id);
CREATE INDEX IF NOT EXISTS idx_account_posts_seen ON account_posts(first_seen_at);

CREATE TABLE IF NOT EXISTS account_state (
    monitored_handle       TEXT PRIMARY KEY,
    scope_hash             TEXT NOT NULL,
    last_attempt_at        TEXT,
    last_valid_response_at TEXT,
    last_scan_boundary_at  TEXT,
    gap_from_at            TEXT,
    gap_to_at              TEXT,
    consecutive_failures   INTEGER NOT NULL DEFAULT 0,
    next_allowed_at        TEXT,
    coverage_status        TEXT,
    bootstrap_done         INTEGER NOT NULL DEFAULT 0,
    deferred               INTEGER NOT NULL DEFAULT 0,
    updated_at             TEXT
);

CREATE TABLE IF NOT EXISTS source_state (
    source_key          TEXT PRIMARY KEY,
    next_allowed_at     TEXT,
    last_throttled_at   TEXT,
    consecutive_throttle INTEGER NOT NULL DEFAULT 0,
    note                TEXT,
    updated_at          TEXT
);

CREATE TABLE IF NOT EXISTS post_judgments (
    post_id       TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    judge_version TEXT NOT NULL,
    model         TEXT NOT NULL,
    is_reset      INTEGER NOT NULL,
    probability   REAL NOT NULL,
    scope         TEXT NOT NULL,
    reasoning     TEXT,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    created_at    TEXT NOT NULL,
    notified_at   TEXT,
    PRIMARY KEY (post_id, content_hash, judge_version),
    FOREIGN KEY (post_id) REFERENCES posts(post_id)
);
CREATE INDEX IF NOT EXISTS idx_judgments_created ON post_judgments(created_at);
CREATE INDEX IF NOT EXISTS idx_judgments_reset   ON post_judgments(is_reset, probability);

CREATE TABLE IF NOT EXISTS reset_forecasts (
    date_key     TEXT NOT NULL,
    handle       TEXT NOT NULL,
    version      TEXT NOT NULL,
    probability  REAL NOT NULL,
    confidence   TEXT NOT NULL,
    signals_json TEXT,
    reasoning    TEXT,
    model        TEXT,
    basis_hash   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (date_key, handle, version)
);

CREATE TABLE IF NOT EXISTS post_translations (
    post_id      TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    lang         TEXT NOT NULL,
    text         TEXT NOT NULL,
    model        TEXT,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (post_id, content_hash, lang),
    FOREIGN KEY (post_id) REFERENCES posts(post_id)
);

CREATE TABLE IF NOT EXISTS fetch_runs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    run_uid            TEXT NOT NULL,
    monitored_handle   TEXT NOT NULL,
    started_at         TEXT NOT NULL,
    finished_at        TEXT,
    source             TEXT NOT NULL,
    mode               TEXT NOT NULL,
    requests_attempted INTEGER NOT NULL DEFAULT 0,
    pages_fetched      INTEGER NOT NULL DEFAULT 0,
    returned_count     INTEGER NOT NULL DEFAULT 0,
    valid_count        INTEGER NOT NULL DEFAULT 0,
    new_count          INTEGER NOT NULL DEFAULT 0,
    updated_count      INTEGER NOT NULL DEFAULT 0,
    parse_failures     INTEGER NOT NULL DEFAULT 0,
    coverage_status    TEXT,
    status             TEXT NOT NULL,
    error_kind         TEXT,
    error_detail       TEXT,
    notes_json         TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_handle ON fetch_runs(monitored_handle, started_at);
CREATE INDEX IF NOT EXISTS idx_runs_uid    ON fetch_runs(run_uid);
"""


class Storage:
    def __init__(self, path: str) -> None:
        self.path = path
        ensure_dir(os.path.dirname(os.path.abspath(path)) or ".")
        self.conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        # 单进程写入 + 每小时一次，不需要 WAL；保持单文件让备份和恢复更简单。
        self.conn.execute("PRAGMA synchronous = FULL")
        self._migrate()

    # --- 生命周期 -----------------------------------------------------------
    def _migrate(self) -> None:
        # executescript 会隐式提交当前事务，所以它不能放在 self.transaction() 里。
        self.conn.executescript(_SCHEMA)
        with self.transaction():
            row = self.conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'version'"
            ).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif int(row["value"]) > SCHEMA_VERSION:
                raise RuntimeError(
                    "数据库 schema 版本 %s 高于本程序支持的 %d，拒绝写入"
                    % (row["value"], SCHEMA_VERSION)
                )
            elif int(row["value"]) < SCHEMA_VERSION:
                # 新表由上面的 CREATE TABLE IF NOT EXISTS 建好，这里只记版本。
                # 本项目的迁移都是「加表/加索引」，没有破坏性变更。
                self.conn.execute(
                    "UPDATE schema_meta SET value = ? WHERE key = 'version'",
                    (str(SCHEMA_VERSION),),
                )

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    class _Tx:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self.conn = conn

        def __enter__(self) -> sqlite3.Connection:
            self.conn.execute("BEGIN IMMEDIATE")
            return self.conn

        def __exit__(self, exc_type, exc, tb) -> None:
            if exc_type is None:
                self.conn.execute("COMMIT")
            else:
                self.conn.execute("ROLLBACK")

    def transaction(self) -> "Storage._Tx":
        return Storage._Tx(self.conn)

    # --- posts --------------------------------------------------------------
    def upsert_post(self, post: NormalizedPost, seen_at: _dt.datetime) -> str:
        """写入或更新一条帖子。

        返回 `inserted` / `content_updated` / `seen`。同一帖子再次出现只更新
        `last_seen_at` 与有证据的变化，不重复插入（方案 §7.1）。
        """
        seen_iso = to_iso(seen_at)
        existing = self.conn.execute(
            "SELECT * FROM posts WHERE post_id = ?", (post.post_id,)
        ).fetchone()

        if existing is None:
            self.conn.execute(
                """
                INSERT INTO posts (
                    post_id, author_id, author_handle, url, text, published_at,
                    post_type, reply_to_id, quote_post_id, media_json, metrics_json,
                    content_hash, first_seen_at, last_seen_at, content_updated_at,
                    source, raw_json, content_status, warnings_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    post.post_id,
                    post.author_id,
                    post.author_handle,
                    post.url,
                    post.text or "",
                    to_iso(post.published_at),
                    post.post_type,
                    post.reply_to_id,
                    post.quote_post_id,
                    post.media_json,
                    post.metrics_json,
                    post.content_hash,
                    seen_iso,
                    seen_iso,
                    None,
                    post.source,
                    post.raw_json,
                    post.content_status,
                    json.dumps(post.warnings, ensure_ascii=False) if post.warnings else None,
                ),
            )
            return "inserted"

        updates, content_changed = self._merge_fields(existing, post)
        updates["last_seen_at"] = seen_iso
        if content_changed:
            updates["content_updated_at"] = seen_iso
        columns = ", ".join("%s = ?" % key for key in updates)
        self.conn.execute(
            "UPDATE posts SET %s WHERE post_id = ?" % columns,
            tuple(updates.values()) + (post.post_id,),
        )
        return "content_updated" if content_changed else "seen"

    def _merge_fields(
        self, existing: sqlite3.Row, post: NormalizedPost
    ) -> Tuple[Dict[str, Any], bool]:
        """决定哪些字段可以被新观察覆盖。

        核心约束：**"缺少某个结构"永远不能覆盖"已经观察到该结构"。**

        这条不是纸面推演。实测发现同一条帖子在不同监控账号的时间线里结构不一致：
        帖子 2098217573276131577 在 @bcherny 的时间线里带 `replying_to`（判为 reply），
        在 @simonw 的时间线里 `replying_to` 是 null（判为 original）。若按"新观察覆盖
        旧值"处理，它会在两个账号之间来回翻转，每轮都算一次"内容更新"并重写归档文件
        —— 正是方案 §7.1 要避免的反复更新。

        其余约束（方案 §7.1 / §14）：降级来源（RSS）缺字段时不得把已有完整正文或媒体
        覆盖成空；互动数变化不算内容变化。
        """
        updates: Dict[str, Any] = {}

        new_rank = _SOURCE_RANK.get(post.source, 0)
        old_rank = _SOURCE_RANK.get(existing["source"], 0)
        downgrade = new_rank < old_rank

        # --- 逐字段决定合并结果 ---
        merged_text = existing["text"] or ""
        new_text = post.text or ""
        if new_text and new_text != merged_text:
            merged_text = new_text
            updates["text"] = merged_text

        merged_media_json = existing["media_json"]
        if post.media_json and post.media_json != merged_media_json:
            merged_media_json = post.media_json
            updates["media_json"] = merged_media_json

        # 类型只允许向"更具体"的方向变：
        # unknown(0) < original(1) < reply/quote(2)。original 来自"结构不存在"，
        # 因此不能覆盖已经确认的 reply/quote。
        merged_type = existing["post_type"]
        if _TYPE_SPECIFICITY.get(post.post_type, 0) > _TYPE_SPECIFICITY.get(merged_type, 0):
            merged_type = post.post_type
            updates["post_type"] = merged_type

        merged_reply_to = existing["reply_to_id"]
        merged_quote = existing["quote_post_id"]
        for field, value, current in (
            ("reply_to_id", post.reply_to_id, merged_reply_to),
            ("quote_post_id", post.quote_post_id, merged_quote),
            ("author_id", post.author_id, existing["author_id"]),
        ):
            # 只在新值非空且不同时写入 —— 永不把已知 ID 清空
            if value and value != current:
                updates[field] = value
                if field == "reply_to_id":
                    merged_reply_to = value
                elif field == "quote_post_id":
                    merged_quote = value

        if post.published_at is not None:
            new_published = to_iso(post.published_at)
            if new_published != existing["published_at"]:
                updates["published_at"] = new_published

        # 互动数快照：更新但**不**计入内容变化，避免归档文件反复重写
        if post.metrics_json and post.metrics_json != existing["metrics_json"]:
            updates["metrics_json"] = post.metrics_json

        # 用**合并后**的值重算哈希。若用来料的哈希，被拒绝的字段会让哈希与实际内容
        # 长期不一致，导致每轮都误报一次"内容更新"。
        merged_hash = content_hash_from_parts(
            post.post_id,
            merged_type,
            existing["author_handle"],
            merged_text,
            merged_reply_to,
            merged_quote,
            [m.get("url", "") for m in parse_media(merged_media_json)],
        )
        content_changed = merged_hash != existing["content_hash"]
        if content_changed:
            updates["content_hash"] = merged_hash

        if not downgrade:
            if post.raw_json:
                updates["raw_json"] = post.raw_json
            updates["source"] = post.source
            if post.content_status != existing["content_status"]:
                updates["content_status"] = post.content_status
            if post.warnings:
                updates["warnings_json"] = json.dumps(post.warnings, ensure_ascii=False)

        return updates, content_changed

    def get_post(self, post_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM posts WHERE post_id = ?", (post_id,)).fetchone()

    # --- account_posts ------------------------------------------------------
    def upsert_account_post(
        self,
        handle: str,
        post_id: str,
        relation: str,
        seen_at: _dt.datetime,
        run_uid: str,
        is_bootstrap: bool = False,
    ) -> str:
        """建立监控账号与帖子的关联。返回 `linked` / `relation_upgraded` / `existing`。"""
        seen_iso = to_iso(seen_at)
        existing = self.conn.execute(
            "SELECT relation FROM account_posts WHERE monitored_handle = ? AND post_id = ?",
            (handle, post_id),
        ).fetchone()
        if existing is None:
            self.conn.execute(
                """
                INSERT INTO account_posts (
                    monitored_handle, post_id, relation, is_bootstrap,
                    first_seen_at, last_seen_at, first_run_uid
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (handle, post_id, relation, 1 if is_bootstrap else 0, seen_iso, seen_iso, run_uid),
            )
            return "linked"

        old = existing["relation"]
        if _RELATION_RANK.get(relation, 0) > _RELATION_RANK.get(old, 0):
            self.conn.execute(
                "UPDATE account_posts SET relation = ?, last_seen_at = ? "
                "WHERE monitored_handle = ? AND post_id = ?",
                (relation, seen_iso, handle, post_id),
            )
            return "relation_upgraded"
        self.conn.execute(
            "UPDATE account_posts SET last_seen_at = ? WHERE monitored_handle = ? AND post_id = ?",
            (seen_iso, handle, post_id),
        )
        return "existing"

    def known_post_ids(self, handle: str) -> Set[str]:
        """本轮开始前该账号已知的帖子集合。

        方案 §6.4.1：重叠判断必须用**本轮之前**的记录，不能把刚入库的当历史锚点。
        """
        rows = self.conn.execute(
            "SELECT post_id FROM account_posts WHERE monitored_handle = ?", (handle,)
        ).fetchall()
        return {row["post_id"] for row in rows}

    def account_post_count(self, handle: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM account_posts WHERE monitored_handle = ?", (handle,)
        ).fetchone()
        return int(row["n"])

    # --- account_state ------------------------------------------------------
    def get_account_state(self, handle: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM account_state WHERE monitored_handle = ?", (handle,)
        ).fetchone()

    def ensure_account_state(self, handle: str, scope_hash: str) -> Dict[str, Any]:
        """取账号状态；`scope_hash` 变化时清掉旧边界。

        方案 §7.3：改变采集范围或换数据源后，旧扫描边界不能直接当成新范围已覆盖。
        """
        row = self.get_account_state(handle)
        now_iso = to_iso(now_utc())
        if row is None:
            self.conn.execute(
                "INSERT INTO account_state (monitored_handle, scope_hash, updated_at) VALUES (?,?,?)",
                (handle, scope_hash, now_iso),
            )
            return {
                "monitored_handle": handle,
                "scope_hash": scope_hash,
                "last_attempt_at": None,
                "last_valid_response_at": None,
                "last_scan_boundary_at": None,
                "gap_from_at": None,
                "gap_to_at": None,
                "consecutive_failures": 0,
                "next_allowed_at": None,
                "coverage_status": None,
                "bootstrap_done": 0,
                "deferred": 0,
                "scope_changed": False,
            }

        state = dict(row)
        state["scope_changed"] = False
        if state["scope_hash"] != scope_hash:
            state["scope_changed"] = True
            state["last_scan_boundary_at"] = None
            state["coverage_status"] = None
            state["scope_hash"] = scope_hash
            self.conn.execute(
                "UPDATE account_state SET scope_hash = ?, last_scan_boundary_at = NULL, "
                "coverage_status = NULL, updated_at = ? WHERE monitored_handle = ?",
                (scope_hash, now_iso, handle),
            )
        return state

    def save_account_state(self, handle: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "scope_hash",
            "last_attempt_at",
            "last_valid_response_at",
            "last_scan_boundary_at",
            "gap_from_at",
            "gap_to_at",
            "consecutive_failures",
            "next_allowed_at",
            "coverage_status",
            "bootstrap_done",
            "deferred",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError("account_state 不存在字段：%s" % ", ".join(sorted(unknown)))
        fields["updated_at"] = to_iso(now_utc())
        columns = ", ".join("%s = ?" % key for key in fields)
        self.conn.execute(
            "UPDATE account_state SET %s WHERE monitored_handle = ?" % columns,
            tuple(fields.values()) + (handle,),
        )

    def all_account_states(self) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM account_state ORDER BY monitored_handle"
        ).fetchall()

    # --- source_state -------------------------------------------------------
    def get_source_state(self, source_key: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM source_state WHERE source_key = ?", (source_key,)
        ).fetchone()

    def set_source_backoff(
        self, source_key: str, next_allowed_at: Optional[_dt.datetime], note: str
    ) -> None:
        """记录同源退避。一次 429 影响该上游的所有账号（方案 §10）。"""
        now_iso = to_iso(now_utc())
        row = self.get_source_state(source_key)
        streak = (int(row["consecutive_throttle"]) + 1) if row else 1
        if row is None:
            self.conn.execute(
                """
                INSERT INTO source_state (
                    source_key, next_allowed_at, last_throttled_at,
                    consecutive_throttle, note, updated_at
                ) VALUES (?,?,?,?,?,?)
                """,
                (source_key, to_iso(next_allowed_at), now_iso, streak, note, now_iso),
            )
        else:
            self.conn.execute(
                "UPDATE source_state SET next_allowed_at = ?, last_throttled_at = ?, "
                "consecutive_throttle = ?, note = ?, updated_at = ? WHERE source_key = ?",
                (to_iso(next_allowed_at), now_iso, streak, note, now_iso, source_key),
            )

    def clear_source_backoff(self, source_key: str) -> None:
        row = self.get_source_state(source_key)
        if row is None or row["next_allowed_at"] is None:
            return
        self.conn.execute(
            "UPDATE source_state SET next_allowed_at = NULL, consecutive_throttle = 0, "
            "note = ?, updated_at = ? WHERE source_key = ?",
            ("退避已解除", to_iso(now_utc()), source_key),
        )

    def all_source_states(self) -> List[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM source_state ORDER BY source_key").fetchall()

    # --- fetch_runs ---------------------------------------------------------
    def start_run(
        self, run_uid: str, handle: str, started_at: _dt.datetime, source: str, mode: str
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO fetch_runs (run_uid, monitored_handle, started_at, source, mode, status)
            VALUES (?,?,?,?,?,'running')
            """,
            (run_uid, handle, to_iso(started_at), source, mode),
        )
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, **fields: Any) -> None:
        allowed = {
            "finished_at",
            "requests_attempted",
            "pages_fetched",
            "returned_count",
            "valid_count",
            "new_count",
            "updated_count",
            "parse_failures",
            "coverage_status",
            "status",
            "error_kind",
            "error_detail",
            "notes_json",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError("fetch_runs 不存在字段：%s" % ", ".join(sorted(unknown)))
        columns = ", ".join("%s = ?" % key for key in fields)
        self.conn.execute(
            "UPDATE fetch_runs SET %s WHERE id = ?" % columns,
            tuple(fields.values()) + (run_id,),
        )

    def recent_runs(self, limit: int = 20, handle: Optional[str] = None) -> List[sqlite3.Row]:
        if handle:
            return self.conn.execute(
                "SELECT * FROM fetch_runs WHERE monitored_handle = ? "
                "ORDER BY id DESC LIMIT ?",
                (handle, limit),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM fetch_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def runs_in_window(self, start_iso: str, end_iso: str) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM fetch_runs WHERE started_at >= ? AND started_at < ? ORDER BY id",
            (start_iso, end_iso),
        ).fetchall()

    # --- post_judgments -----------------------------------------------------
    def save_judgment(self, verdict: Any) -> None:
        """写入一条判定结果。键含 content_hash，帖子编辑后会重新判定。"""
        self.conn.execute(
            """
            INSERT OR REPLACE INTO post_judgments (
                post_id, content_hash, judge_version, model, is_reset,
                probability, scope, reasoning, input_tokens, output_tokens, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                verdict.post_id,
                verdict.content_hash,
                verdict.judge_version,
                verdict.model,
                1 if verdict.is_reset else 0,
                float(verdict.probability),
                verdict.scope,
                verdict.reasoning,
                verdict.input_tokens,
                verdict.output_tokens,
                to_iso(verdict.created_at),
            ),
        )

    def mark_judgment_notified(
        self, post_id: str, content_hash: str, judge_version: str
    ) -> None:
        self.conn.execute(
            "UPDATE post_judgments SET notified_at = ? "
            "WHERE post_id = ? AND content_hash = ? AND judge_version = ?",
            (to_iso(now_utc()), post_id, content_hash, judge_version),
        )

    def judgment_notified(
        self, post_id: str, content_hash: str, judge_version: str
    ) -> bool:
        """是否已经通知过。防止重跑导致重复通知。"""
        row = self.conn.execute(
            "SELECT notified_at FROM post_judgments "
            "WHERE post_id = ? AND content_hash = ? AND judge_version = ?",
            (post_id, content_hash, judge_version),
        ).fetchone()
        return bool(row and row["notified_at"])

    def judgment_count_since(self, moment: _dt.datetime) -> int:
        """统计时间窗内的模型调用次数，用于日配额控制。"""
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM post_judgments WHERE created_at >= ?",
            (to_iso(moment),),
        ).fetchone()
        return int(row["n"])

    def judgments_between(self, start_iso: str, end_iso: str) -> List[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT j.*, p.author_handle, p.url, p.text, p.published_at
            FROM post_judgments j
            JOIN posts p ON p.post_id = j.post_id
            WHERE j.created_at >= ? AND j.created_at < ?
            ORDER BY j.probability DESC, j.created_at
            """,
            (start_iso, end_iso),
        ).fetchall()

    def recent_resets(
        self, limit: int = 20, min_probability: float = 0.0,
        handles: Optional[Sequence[str]] = None,
    ) -> List[sqlite3.Row]:
        # 延迟导入以避免 judge -> storage 的循环依赖。
        from .judge import JUDGE_VERSION

        scope_sql = ""
        params: List[Any] = [JUDGE_VERSION, min_probability]
        if handles is not None:
            if not handles:
                return []
            scope_sql = """
              AND EXISTS (
                  SELECT 1 FROM account_posts ap
                  WHERE ap.post_id = p.post_id AND ap.relation = 'self'
                    AND ap.monitored_handle IN (%s)
              )
            """ % ",".join("?" for _ in handles)
            params.extend(handles)
        params.append(limit)
        return self.conn.execute(
            """
            SELECT j.*, p.author_handle, p.url, p.text, p.published_at
            FROM post_judgments j
            JOIN posts p ON p.post_id = j.post_id AND p.content_hash = j.content_hash
            WHERE j.judge_version = ? AND j.is_reset = 1 AND j.probability >= ?
            """ + scope_sql + " ORDER BY p.published_at DESC LIMIT ?",
            params,
        ).fetchall()

    # --- reset_forecasts ----------------------------------------------------
    def get_forecast(self, date_key: str, handle: str, version: str):
        return self.conn.execute(
            "SELECT * FROM reset_forecasts "
            "WHERE date_key = ? AND handle = ? AND version = ?",
            (date_key, handle, version),
        ).fetchone()

    def save_forecast(self, forecast: Any) -> None:
        """同一天同一账号只留一条（是否重算看 basis_hash，见 forecast.py）。"""
        self.conn.execute(
            """
            INSERT OR REPLACE INTO reset_forecasts (
                date_key, handle, version, probability, confidence,
                signals_json, reasoning, model, basis_hash, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                forecast.date_key,
                forecast.handle,
                forecast.version,
                float(forecast.probability),
                forecast.confidence,
                json.dumps(forecast.signals, ensure_ascii=False),
                forecast.reasoning,
                forecast.model,
                forecast.basis_hash,
                to_iso(forecast.created_at),
            ),
        )

    # --- post_translations --------------------------------------------------
    def get_translation(self, post_id: str, content_hash: str, lang: str):
        """键含 content_hash：正文被编辑后旧译文自动失效。"""
        return self.conn.execute(
            "SELECT * FROM post_translations "
            "WHERE post_id = ? AND content_hash = ? AND lang = ?",
            (post_id, content_hash, lang),
        ).fetchone()

    def save_translation(
        self, post_id: str, content_hash: str, lang: str, text: str, model: str
    ) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO post_translations
                (post_id, content_hash, lang, text, model, created_at)
            VALUES (?,?,?,?,?,?)
            """,
            (post_id, content_hash, lang, text, model, to_iso(now_utc())),
        )

    def translations_for(self, post_ids: Sequence[str], lang: str) -> Dict[str, str]:
        """批量取译文，只返回与当前 content_hash 匹配的（避免显示过期译文）。"""
        if not post_ids:
            return {}
        out: Dict[str, str] = {}
        chunk = 400
        for i in range(0, len(post_ids), chunk):
            part = list(post_ids[i : i + chunk])
            marks = ",".join("?" * len(part))
            rows = self.conn.execute(
                """
                SELECT t.post_id, t.text
                FROM post_translations t
                JOIN posts p ON p.post_id = t.post_id
                                AND p.content_hash = t.content_hash
                WHERE t.lang = ? AND t.post_id IN (%s)
                """
                % marks,
                [lang] + part,
            ).fetchall()
            for row in rows:
                out[row["post_id"]] = row["text"]
        return out

    # --- 导出查询 -----------------------------------------------------------
    def posts_first_seen_between(self, start_iso: str, end_iso: str) -> List[sqlite3.Row]:
        """按**首次发现时间**取帖子（方案 §9.2）。

        补到一周前的帖子也必须出现在发现当天的索引里，所以这里过滤的是
        `account_posts.first_seen_at`，不是 `published_at`。
        """
        return self.conn.execute(
            """
            SELECT p.*, ap.monitored_handle, ap.relation, ap.is_bootstrap,
                   ap.first_seen_at AS link_first_seen_at
            FROM account_posts ap
            JOIN posts p ON p.post_id = ap.post_id
            WHERE ap.first_seen_at >= ? AND ap.first_seen_at < ?
            ORDER BY ap.first_seen_at, ap.monitored_handle, p.post_id
            """,
            (start_iso, end_iso),
        ).fetchall()

    def posts_content_updated_between(self, start_iso: str, end_iso: str) -> List[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT p.*, ap.monitored_handle, ap.relation
            FROM posts p
            JOIN account_posts ap ON ap.post_id = p.post_id
            WHERE p.content_updated_at >= ? AND p.content_updated_at < ?
              AND ap.first_seen_at < ?
            ORDER BY p.content_updated_at, p.post_id
            """,
            (start_iso, end_iso, start_iso),
        ).fetchall()

    def links_for_post(self, post_id: str) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM account_posts WHERE post_id = ? ORDER BY monitored_handle",
            (post_id,),
        ).fetchall()

    def post_rows(self, post_ids: Sequence[str]) -> List[sqlite3.Row]:
        if not post_ids:
            return []
        out: List[sqlite3.Row] = []
        chunk = 400  # 避开 SQLite 变量数上限
        for i in range(0, len(post_ids), chunk):
            part = list(post_ids[i : i + chunk])
            marks = ",".join("?" * len(part))
            out.extend(
                self.conn.execute(
                    "SELECT * FROM posts WHERE post_id IN (%s)" % marks, part
                ).fetchall()
            )
        return out

    def distinct_discovery_dates(self, limit: int = 3650) -> List[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT substr(first_seen_at, 1, 10) AS day FROM account_posts "
            "ORDER BY day DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [row["day"] for row in rows]

    def counts(self) -> Dict[str, int]:
        def one(sql: str) -> int:
            return int(self.conn.execute(sql).fetchone()[0])

        return {
            "posts": one("SELECT COUNT(*) FROM posts"),
            "links": one("SELECT COUNT(*) FROM account_posts"),
            "runs": one("SELECT COUNT(*) FROM fetch_runs"),
        }

    def unresolved_gaps(self) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM account_state WHERE gap_from_at IS NOT NULL "
            "ORDER BY monitored_handle"
        ).fetchall()


def parse_media(row_value: Optional[str]) -> List[Dict[str, Any]]:
    if not row_value:
        return []
    try:
        data = json.loads(row_value)
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def parse_metrics(row_value: Optional[str]) -> Dict[str, Any]:
    if not row_value:
        return {}
    try:
        data = json.loads(row_value)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def parse_warnings(row_value: Optional[str]) -> List[str]:
    if not row_value:
        return []
    try:
        data = json.loads(row_value)
    except ValueError:
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


def iter_rows(rows: Iterable[sqlite3.Row]) -> Iterable[Dict[str, Any]]:
    for row in rows:
        yield dict(row)
