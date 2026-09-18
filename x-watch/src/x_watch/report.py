"""机器可读的状态报告（JSON）。

这是采集引擎与外部消费者（菜单栏 app、脚本）之间的**稳定契约**。有了它，调用方
不必直连 SQLite，也就不会被表结构变更打破 —— 只需要跟着 `schema` 版本走。

约定：
- JSON 只写 stdout，日志只写 stderr（见 logsetup）。
- 所有时间都是 UTC ISO-8601 带时区，由调用方决定怎么展示。
- 帖子正文和模型输出原样给出（调用方自己负责转义/限长）；概率是模型判断，不是事实。
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Optional, Sequence

from .config import Config
from .forecast import forecasts_for, recent_self_posts
from .translate import TARGET_LANG
from .judge import JUDGE_VERSION
from .storage import Storage
from .util import now_utc, to_iso

REPORT_SCHEMA = 3  # v3 增加 text_zh（中文译文）


def _account_state(row) -> Dict[str, Any]:
    return {
        "handle": row["monitored_handle"],
        "last_attempt_at": row["last_attempt_at"],
        "last_valid_response_at": row["last_valid_response_at"],
        "last_scan_boundary_at": row["last_scan_boundary_at"],
        "coverage_status": row["coverage_status"],
        "consecutive_failures": int(row["consecutive_failures"] or 0),
        "next_allowed_at": row["next_allowed_at"],
        "deferred": bool(int(row["deferred"] or 0)),
        "gap_from_at": row["gap_from_at"],
        "gap_to_at": row["gap_to_at"],
    }


def _reset_entry(row, translations: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    return {
        "post_id": row["post_id"],
        # 中文译文；没有就是 None，界面回落显示原文
        "text_zh": (translations or {}).get(row["post_id"]),
        "author_handle": row["author_handle"],
        "text": row["text"] or "",
        "url": row["url"] or "",
        "published_at": row["published_at"],
        "probability": float(row["probability"] or 0.0),
        "scope": row["scope"],
        "reasoning": row["reasoning"] or "",
        "model": row["model"] or "",
        "judged_at": row["created_at"],
        "notified_at": row["notified_at"],
    }


def _recent_posts(
    config: Config,
    storage: Storage,
    per_handle: int = 15,
    translations: Optional[Dict[str, str]] = None,
    handles: Optional[Sequence[str]] = None,
):
    """每个监控账号最近的自有帖子。给菜单栏列表用。"""
    out = []
    translations = translations or {}
    for account in config.enabled_accounts():
        if handles is not None and account.handle not in handles:
            continue
        for row in recent_self_posts(storage, account.handle, limit=per_handle):
            out.append(
                {
                    "post_id": row["post_id"],
                    "monitored_handle": account.handle,
                    "text": row["text"] or "",
                    "text_zh": translations.get(row["post_id"]),
                    "url": row["url"] or "",
                    "published_at": row["published_at"],
                    "post_type": row["post_type"],
                    "is_reset": bool(row["is_reset"]) if row["is_reset"] is not None else None,
                    "probability": (
                        float(row["probability"]) if row["probability"] is not None else None
                    ),
                }
            )
    return out


def build_status_report(
    config: Config,
    storage: Storage,
    reset_limit: int = 20,
    min_probability: float = 0.0,
    post_limit: int = 15,
    handles: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """只读状态快照。不请求上游、不调模型。"""
    targets = [a.handle for a in config.enabled_accounts()
               if handles is None or a.handle in handles]
    counts = storage.counts()
    source_states = []
    for row in storage.all_source_states():
        source_states.append(
            {
                "source_key": row["source_key"],
                "next_allowed_at": row["next_allowed_at"],
                "consecutive_throttle": int(row["consecutive_throttle"] or 0),
                "note": row["note"] or "",
            }
        )

    runs = []
    recent_runs = [row for handle in targets
                   for row in storage.recent_runs(10, handle=handle)]
    for row in sorted(recent_runs, key=lambda r: r["id"], reverse=True)[:10]:
        runs.append(
            {
                "handle": row["monitored_handle"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "mode": row["mode"],
                "status": row["status"],
                "coverage_status": row["coverage_status"],
                "pages_fetched": int(row["pages_fetched"] or 0),
                "new_count": int(row["new_count"] or 0),
                "updated_count": int(row["updated_count"] or 0),
                "parse_failures": int(row["parse_failures"] or 0),
                "error_kind": row["error_kind"],
            }
        )

    reset_rows = storage.recent_resets(reset_limit, min_probability, handles=targets)
    post_rows = _recent_posts(config, storage, per_handle=post_limit, handles=targets)
    # 一次性把译文取出来（只取与当前 content_hash 匹配的），不逐条查库
    all_ids = [r["post_id"] for r in reset_rows] + [p["post_id"] for p in post_rows]
    translations = storage.translations_for(all_ids, TARGET_LANG)
    for post in post_rows:
        post["text_zh"] = translations.get(post["post_id"])

    return {
        "schema": REPORT_SCHEMA,
        "generated_at": to_iso(now_utc()),
        "database_path": config.database_path,
        "output_dir": config.output_dir,
        "interval_minutes": int(config.collection["interval_minutes"]),
        "provider": config.source["provider"],
        "judge": {
            "enabled": bool(config.judge["enabled"]),
            "backend": config.judge["backend"],
            "model": config.judge["model"],
            "threshold": float(config.judge["threshold"]),
            "version": JUDGE_VERSION,
            "calls_last_24h": storage.judgment_count_since(
                now_utc() - _dt.timedelta(days=1)
            ),
            "max_calls_per_day": int(config.judge["max_calls_per_day"]),
        },
        "configured_accounts": targets,
        "counts": counts,
        "accounts": [_account_state(r) for r in storage.all_account_states()
                     if r["monitored_handle"] in targets],
        "source_states": source_states,
        "recent_runs": runs,
        "gaps": [
            {
                "handle": r["monitored_handle"],
                "gap_from_at": r["gap_from_at"],
                "gap_to_at": r["gap_to_at"],
                "coverage_status": r["coverage_status"],
            }
            for r in storage.unresolved_gaps()
            if r["monitored_handle"] in targets
        ],
        "resets": [_reset_entry(r, translations) for r in reset_rows],
        # 界面要展示的「最近帖子」，按监控账号分组
        "recent_posts": post_rows,
        # 今日重置概率。只读已有结果，不在这里触发模型调用
        "forecasts": forecasts_for(
            storage,
            targets,
            config.app["display_timezone"],
            threshold=float(config.judge["threshold"]),
        ),
    }


def build_tick_report(
    config: Config,
    storage: Storage,
    collection_summary: Optional[Dict[str, Any]] = None,
    judge_summary: Optional[Dict[str, Any]] = None,
    errors: Optional[Sequence[str]] = None,
    reset_limit: int = 20,
    handles: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """一次监控 tick 的结果：采集概况 + 判定概况 + 当前命中列表。"""
    report = build_status_report(config, storage, reset_limit=reset_limit, handles=handles)
    report["collection"] = collection_summary or {}
    report["judge_run"] = judge_summary or {}
    report["errors"] = list(errors or [])
    report["ok"] = not report["errors"]
    return report


def summarize_run(summary) -> Dict[str, Any]:
    """把 collector 的 RunSummary 压成 JSON 友好的结构。"""
    totals = summary.totals()
    return {
        "run_uid": summary.run_uid,
        "started_at": to_iso(summary.started_at),
        "accounts": totals["accounts"],
        "new_posts": totals["new"],
        "updated_posts": totals["updated"],
        "pages_fetched": totals["pages"],
        "parse_failures": totals["parse_failures"],
        "worst_status": summary.worst_status,
        "aborted_reason": summary.aborted_reason,
        "outcomes": [
            {
                "handle": o.handle,
                "mode": o.mode,
                "status": o.status,
                "coverage": o.coverage,
                "pages": o.pages_fetched,
                "new": o.new_count,
                "updated": o.updated_count,
                "parse_failures": o.parse_failures,
                "error_kind": o.error_kind,
            }
            for o in summary.outcomes
        ],
        "notes": list(summary.notes),
    }


def summarize_judge(result, threshold: float) -> Dict[str, Any]:
    """把 JudgeRun 压成 JSON 友好的结构。"""
    return {
        "considered": result.considered,
        "judged": result.judged,
        "failed": result.failed,
        "triggered": len(result.triggered),
        "threshold": threshold,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "notes": list(result.notes),
        "hits": [
            {
                "post_id": v.post_id,
                "probability": v.probability,
                "scope": v.scope,
                "reasoning": v.reasoning,
            }
            for v in result.triggered
        ],
    }
