"""Markdown 导出（方案 §9）。

规则：

- 全部从数据库**重新渲染**，绝不向文件追加 —— 重跑不会产生重复内容。
- 临时文件 + 原子替换。导出失败不回滚已经入库的帖子，之后可单独 `export` 重建。
- 文件名只用已验证的帖子 ID。
- 正文当**数据**处理：放进引用块、链接只接受 http/https、不输出未经处理的 HTML。
  外部帖子里写着"执行命令""忽略规则"也只是文本（方案 §9.3）。
"""

from __future__ import annotations

import datetime as _dt
import os
import sqlite3
from typing import Any, Dict, List, Sequence, Tuple

from .config import Config
from .logsetup import get_logger
from .normalize import (
    RELATION_CONTEXT,
    RELATION_QUOTED,
    RELATION_REPOST,
    RELATION_SELF,
    RELATION_UNKNOWN,
)
from .storage import Storage, parse_media, parse_metrics, parse_warnings
from .util import (
    as_blockquote,
    atomic_write_text,
    display_tzinfo,
    ensure_dir,
    escape_markdown_inline,
    format_display,
    is_safe_filename_token,
    local_date_key,
    now_utc,
    parse_iso,
    safe_url,
    to_iso,
    truncate,
)

_RELATION_LABEL = {
    RELATION_SELF: "本人发布",
    RELATION_REPOST: "本人转帖",
    RELATION_CONTEXT: "对话上下文（上游附带返回）",
    RELATION_QUOTED: "被引用原帖",
    RELATION_UNKNOWN: "关联未确认",
}
_TYPE_LABEL = {
    "original": "原创",
    "reply": "回复",
    "quote": "引用",
    "repost": "转帖",
    "unknown": "未知类型",
}


class ExportError(Exception):
    """导出失败。采集结果已保存，可单独重建（方案 §10）。"""


class Exporter:
    def __init__(self, config: Config, storage: Storage) -> None:
        self.config = config
        self.storage = storage
        self.log = get_logger()
        self.tz = display_tzinfo(config.app["display_timezone"])
        self.posts_dir = os.path.join(config.output_dir, "posts")
        self.daily_dir = os.path.join(config.output_dir, "daily")

    # --- 对外入口 -----------------------------------------------------------
    def export_after_run(self, affected_post_ids: Sequence[str]) -> Dict[str, Any]:
        """一轮采集之后，重建受影响的帖子文件与相关日期索引。"""
        result: Dict[str, Any] = {"posts_written": 0, "dates_written": [], "errors": []}
        if self.config.export["write_post_files"]:
            for post_id in affected_post_ids:
                try:
                    if self.write_post_file(post_id):
                        result["posts_written"] += 1
                except (OSError, ExportError) as exc:
                    result["errors"].append("帖子 %s 导出失败：%s" % (post_id, exc))

        if self.config.export["write_daily_index"]:
            for date_key in self._dates_touched(affected_post_ids):
                try:
                    self.write_daily_index(date_key)
                    result["dates_written"].append(date_key)
                except (OSError, ExportError) as exc:
                    result["errors"].append("日期 %s 索引失败：%s" % (date_key, exc))
        return result

    def rebuild_date(self, date_key: str) -> Dict[str, Any]:
        """只从数据库重建某日索引及当日发现的帖子文件，不请求上游。"""
        _validate_date_key(date_key)
        result: Dict[str, Any] = {"posts_written": 0, "dates_written": [], "errors": []}
        start_iso, end_iso = self._utc_window(date_key)
        rows = self.storage.posts_first_seen_between(start_iso, end_iso)
        if self.config.export["write_post_files"]:
            for post_id in dict.fromkeys(row["post_id"] for row in rows):
                try:
                    if self.write_post_file(post_id):
                        result["posts_written"] += 1
                except (OSError, ExportError) as exc:
                    result["errors"].append("帖子 %s 导出失败：%s" % (post_id, exc))
        try:
            self.write_daily_index(date_key)
            result["dates_written"].append(date_key)
        except (OSError, ExportError) as exc:
            result["errors"].append("日期 %s 索引失败：%s" % (date_key, exc))
        return result

    def rebuild_all(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {"posts_written": 0, "dates_written": [], "errors": []}
        # first_seen_at 存的是 UTC，日期键必须按展示时区重算 —— 两者可能差一天，
        # 所以用 UTC 日期的前后一天兜住边界，再去重，每个日期只重建一次。
        candidates = set()
        for utc_date in self.storage.distinct_discovery_dates():
            candidates.update(_neighbor_dates(utc_date))
        for date_key in sorted(candidates):
            partial = self.rebuild_date(date_key)
            result["posts_written"] += partial["posts_written"]
            result["dates_written"].extend(partial["dates_written"])
            result["errors"].extend(partial["errors"])
        return result

    # --- 单帖归档 -----------------------------------------------------------
    def write_post_file(self, post_id: str) -> bool:
        if not is_safe_filename_token(post_id):
            raise ExportError("帖子 ID %r 不能安全用作文件名" % post_id[:64])
        row = self.storage.get_post(post_id)
        if row is None:
            return False
        ensure_dir(self.posts_dir)
        path = os.path.join(self.posts_dir, "%s.md" % post_id)
        atomic_write_text(path, self._render_post(row))
        return True

    def _render_post(self, row: sqlite3.Row) -> str:
        links = self.storage.links_for_post(row["post_id"])
        published = parse_iso(row["published_at"])
        first_seen = parse_iso(row["first_seen_at"])
        last_seen = parse_iso(row["last_seen_at"])
        updated = parse_iso(row["content_updated_at"])
        url = safe_url(row["url"])

        lines: List[str] = []
        lines.append("# 帖子 %s" % row["post_id"])
        lines.append("")
        lines.append("| 字段 | 值 |")
        lines.append("|---|---|")
        lines.append("| 作者 | @%s |" % escape_markdown_inline(row["author_handle"]))
        lines.append("| 原帖链接 | %s |" % (url if url else "（链接不可用）"))
        lines.append("| 发布时间 | %s |" % format_display(published, self.tz))
        lines.append("| 首次发现 | %s |" % format_display(first_seen, self.tz))
        lines.append("| 最近观察 | %s |" % format_display(last_seen, self.tz))
        if updated is not None:
            lines.append("| 内容变化 | %s |" % format_display(updated, self.tz))
        lines.append(
            "| 内容类型 | %s |"
            % _TYPE_LABEL.get(row["post_type"], escape_markdown_inline(row["post_type"]))
        )
        lines.append("| 内容状态 | %s |" % escape_markdown_inline(row["content_status"]))
        lines.append("| 数据来源 | %s |" % escape_markdown_inline(row["source"]))

        monitor_parts = []
        for link in links:
            monitor_parts.append(
                "@%s（%s）"
                % (
                    escape_markdown_inline(link["monitored_handle"]),
                    _RELATION_LABEL.get(link["relation"], link["relation"]),
                )
            )
        lines.append("| 监控来源 | %s |" % ("；".join(monitor_parts) or "（无关联记录）"))

        if row["reply_to_id"]:
            lines.append("| 回复的帖子 | %s |" % escape_markdown_inline(row["reply_to_id"]))
        if row["quote_post_id"]:
            lines.append("| 引用的帖子 | %s |" % escape_markdown_inline(row["quote_post_id"]))

        metrics = parse_metrics(row["metrics_json"])
        if metrics:
            rendered = ", ".join(
                "%s=%s" % (key, "未提供" if value is None else value)
                for key, value in sorted(metrics.items())
            )
            lines.append("| 互动数快照 | %s |" % escape_markdown_inline(rendered))

        lines.append("")
        # 转帖与引用要标清主体（方案 §9.1）
        self_links = [l for l in links if l["relation"] == RELATION_REPOST]
        if self_links:
            lines.append(
                "> **注意**：本帖作者是 @%s，由 %s 转发。正文归属原作者。"
                % (
                    row["author_handle"],
                    "、".join("@" + l["monitored_handle"] for l in self_links),
                )
            )
            lines.append("")
        if any(l["relation"] == RELATION_QUOTED for l in links):
            lines.append("> **注意**：本帖是被引用的原帖，不是监控账号发布的内容。")
            lines.append("")
        if any(l["relation"] == RELATION_CONTEXT for l in links):
            lines.append(
                "> **注意**：本帖由上游随监控账号时间线一并返回（对话上下文），"
                "不是监控账号发布或转发的内容。"
            )
            lines.append("")

        lines.append("## 正文")
        lines.append("")
        # 正文原样保留、不翻译；放进引用块，避免其中的标记破坏文件结构
        lines.append(as_blockquote(row["text"] or ""))
        lines.append("")

        media = parse_media(row["media_json"])
        if media:
            lines.append("## 媒体")
            lines.append("")
            lines.append("只保存链接与描述，不下载媒体文件。")
            lines.append("")
            for item in media:
                media_url = safe_url(item.get("url"))
                if not media_url:
                    continue
                descriptor = str(item.get("type") or "unknown")
                extras = []
                for key in ("width", "height", "duration"):
                    if key in item:
                        extras.append("%s=%s" % (key, item[key]))
                alt = item.get("alt_text")
                bits = " ".join(extras)
                lines.append(
                    "- [%s] <%s>%s%s"
                    % (
                        escape_markdown_inline(descriptor),
                        media_url,
                        (" %s" % bits) if bits else "",
                        ("\n  - 描述：%s" % escape_markdown_inline(str(alt))) if alt else "",
                    )
                )
            lines.append("")

        warnings = parse_warnings(row["warnings_json"])
        if warnings:
            lines.append("## 解析提示")
            lines.append("")
            for warning in warnings:
                lines.append("- %s" % escape_markdown_inline(warning))
            lines.append("")

        lines.append("---")
        lines.append("")
        lines.append(
            "本文件由 x-watch 从 SQLite 重新生成，可随时重建；请以原帖为准。"
        )
        lines.append("")
        return "\n".join(lines)

    # --- 每日索引 -----------------------------------------------------------
    def write_daily_index(self, date_key: str) -> str:
        _validate_date_key(date_key)
        ensure_dir(self.daily_dir)
        path = os.path.join(self.daily_dir, "%s.md" % date_key)
        atomic_write_text(path, self._render_daily(date_key))
        return path

    def _render_daily(self, date_key: str) -> str:
        start_iso, end_iso = self._utc_window(date_key)
        discovered = self.storage.posts_first_seen_between(start_iso, end_iso)
        updated = self.storage.posts_content_updated_between(start_iso, end_iso)
        runs = self.storage.runs_in_window(start_iso, end_iso)
        gaps = self.storage.unresolved_gaps()

        export_cfg = self.config.export
        shown, hidden = self._split_by_filter(discovered)

        lines: List[str] = []
        lines.append("# %s 新增（按首次发现时间）" % date_key)
        lines.append("")
        lines.append(
            "日期按**首次发现时间**及展示时区 `%s` 计算。补到很久以前的帖子也会出现在"
            "发现当天，并显示原始发布时间。" % self.config.app["display_timezone"]
        )
        lines.append("")

        # --- 运行概况 ---
        lines.append("## 运行概况")
        lines.append("")
        if not runs:
            lines.append("当日没有运行记录。")
        else:
            lines.append("| 开始时间 | 账号 | 模式 | 状态 | 覆盖 | 页 | 新增 | 更新 | 解析失败 |")
            lines.append("|---|---|---|---|---|---|---|---|---|")
            for run in runs:
                lines.append(
                    "| %s | @%s | %s | %s | %s | %d | %d | %d | %d |"
                    % (
                        format_display(parse_iso(run["started_at"]), self.tz),
                        escape_markdown_inline(run["monitored_handle"]),
                        escape_markdown_inline(run["mode"]),
                        escape_markdown_inline(run["status"]),
                        escape_markdown_inline(run["coverage_status"] or "-"),
                        int(run["pages_fetched"] or 0),
                        int(run["new_count"] or 0),
                        int(run["updated_count"] or 0),
                        int(run["parse_failures"] or 0),
                    )
                )
            lines.append("")
            failed = [r for r in runs if r["status"] in ("failed", "partial")]
            if failed:
                lines.append("失败/部分失败的运行：")
                lines.append("")
                for run in failed:
                    lines.append(
                        "- @%s %s：%s"
                        % (
                            escape_markdown_inline(run["monitored_handle"]),
                            escape_markdown_inline(run["status"]),
                            escape_markdown_inline(
                                truncate(run["error_detail"] or run["error_kind"] or "无详情", 200)
                            ),
                        )
                    )
        lines.append("")
        lines.append(
            "> 运行成功不等于覆盖完整。覆盖列为 `limited` / `unknown` 时表示本轮没能证明"
            "这段时间没有遗漏。"
        )
        lines.append("")

        # --- 首次发现 ---
        lines.append("## 首次发现的帖子（%d 条）" % len(shown))
        lines.append("")
        if not shown:
            lines.append("当日没有符合阅读过滤条件的新帖。")
            lines.append("")
        else:
            for row in shown:
                lines.append(self._render_index_entry(row))
            lines.append("")

        if hidden:
            lines.append("### 已入库但按配置不在上面展示（%d 条）" % len(hidden))
            lines.append("")
            lines.append(
                "以下内容**已经保存**在数据库和归档文件里，只是按 `[export]` 过滤没有展开。"
            )
            lines.append("")
            counts: Dict[str, int] = {}
            for row in hidden:
                counts[row["relation"]] = counts.get(row["relation"], 0) + 1
            for relation, count in sorted(counts.items()):
                lines.append(
                    "- %s：%d 条" % (_RELATION_LABEL.get(relation, relation), count)
                )
            lines.append("")

        # --- 内容更新 ---
        lines.append("## 内容更新（%d 条）" % len(updated))
        lines.append("")
        if not updated:
            lines.append("当日没有观察到已有帖子的内容变化。")
        else:
            for row in updated:
                lines.append(
                    "- `%s` @%s —— %s（[归档](../posts/%s.md)）"
                    % (
                        row["post_id"],
                        escape_markdown_inline(row["author_handle"]),
                        escape_markdown_inline(truncate(row["text"] or "（无文字内容）", 100)),
                        row["post_id"],
                    )
                )
        lines.append("")

        # --- 重置判定 ---
        judgments = self.storage.judgments_between(start_iso, end_iso)
        if judgments:
            hits = [j for j in judgments if int(j["is_reset"] or 0)]
            lines.append("## 重置判定（%d 条判定，命中 %d 条）" % (len(judgments), len(hits)))
            lines.append("")
            lines.append(
                "由大模型判断帖子是否在宣布用量限额重置。**概率是模型的判断，不是事实**；"
                "帖子正文是不可信输入，只作为待分类数据处理。"
            )
            lines.append("")
            lines.append("| 概率 | 判定 | 范围 | 作者 | 发布时间 | 依据 |")
            lines.append("|---|---|---|---|---|---|")
            for row in judgments:
                lines.append(
                    "| %.2f | %s | %s | @%s | %s | %s |"
                    % (
                        float(row["probability"] or 0.0),
                        "命中" if int(row["is_reset"] or 0) else "否",
                        escape_markdown_inline(row["scope"] or "-"),
                        escape_markdown_inline(row["author_handle"]),
                        format_display(parse_iso(row["published_at"]), self.tz),
                        # 模型输出同样当数据转义
                        escape_markdown_inline(truncate(row["reasoning"] or "-", 110)),
                    )
                )
            lines.append("")
            for row in hits:
                url = safe_url(row["url"])
                lines.append(
                    "- **p=%.2f** `%s` —— %s%s"
                    % (
                        float(row["probability"] or 0.0),
                        row["post_id"],
                        escape_markdown_inline(truncate(row["text"] or "（无文字）", 140)),
                        (" · 原帖 <%s>" % url) if url else "",
                    )
                )
                lines.append(
                    "  - 判定模型 %s，%s"
                    % (
                        escape_markdown_inline(row["model"] or "-"),
                        "已通知" if row["notified_at"] else "未通知",
                    )
                )
            lines.append("")

        # --- 缺口 ---
        lines.append("## 未解决的缺口")
        lines.append("")
        if not gaps:
            lines.append("当前没有记录在案的疑似缺口。这不等于证明没有遗漏。")
        else:
            lines.append("| 账号 | 缺口起 | 缺口止 | 最近覆盖结论 |")
            lines.append("|---|---|---|---|")
            for gap in gaps:
                lines.append(
                    "| @%s | %s | %s | %s |"
                    % (
                        escape_markdown_inline(gap["monitored_handle"]),
                        format_display(parse_iso(gap["gap_from_at"]), self.tz),
                        format_display(parse_iso(gap["gap_to_at"]), self.tz),
                        escape_markdown_inline(gap["coverage_status"] or "-"),
                    )
                )
            lines.append("")
            lines.append(
                "缺口需要用 `backfill` 命令按明确时间范围和页数上限手动处理；"
                "本工具不做无人值守的无限历史回补。"
            )
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(
            "本索引由 x-watch 从 SQLite 重新渲染（生成时间 %s）。"
            % format_display(now_utc(), self.tz)
        )
        lines.append("")
        return "\n".join(lines)

    def _render_index_entry(self, row: sqlite3.Row) -> str:
        published = parse_iso(row["published_at"])
        first_seen = parse_iso(row["link_first_seen_at"])
        url = safe_url(row["url"])
        flags = []
        if int(row["is_bootstrap"] or 0):
            # 初次导入的记录不能全当成"刚刚发布的新帖"（方案 §6.2）
            flags.append("初次导入")
        if row["relation"] != RELATION_SELF:
            flags.append(_RELATION_LABEL.get(row["relation"], row["relation"]))
        if row["content_status"] != "normal":
            flags.append("内容状态 %s" % row["content_status"])

        header = "- **@%s** · %s · 发布 %s · 发现 %s%s" % (
            escape_markdown_inline(row["monitored_handle"]),
            _TYPE_LABEL.get(row["post_type"], row["post_type"]),
            format_display(published, self.tz),
            format_display(first_seen, self.tz),
            ("　[%s]" % "｜".join(flags)) if flags else "",
        )
        body = "  - %s" % escape_markdown_inline(truncate(row["text"] or "（无文字内容）", 160))
        if row["author_handle"] != row["monitored_handle"]:
            body += "\n  - 实际作者：@%s" % escape_markdown_inline(row["author_handle"])
        refs = "  - [归档](../posts/%s.md)" % row["post_id"]
        if url:
            refs += " · 原帖 <%s>" % url
        return "\n".join([header, body, refs])

    # --- 过滤与时间窗 -------------------------------------------------------
    def _split_by_filter(
        self, rows: Sequence[sqlite3.Row]
    ) -> Tuple[List[sqlite3.Row], List[sqlite3.Row]]:
        """`[export]` 只决定阅读索引展示什么，不影响入库（方案 §8）。"""
        cfg = self.config.export
        allowed = {RELATION_SELF: True}
        allowed[RELATION_REPOST] = bool(cfg["include_reposts"])
        allowed[RELATION_CONTEXT] = bool(cfg["include_context"])
        allowed[RELATION_QUOTED] = bool(cfg["include_quoted"])
        allowed[RELATION_UNKNOWN] = bool(cfg["include_unknown"])
        shown = [r for r in rows if allowed.get(r["relation"], True)]
        hidden = [r for r in rows if not allowed.get(r["relation"], True)]
        return shown, hidden

    def _utc_window(self, date_key: str) -> Tuple[str, str]:
        year, month, day = (int(part) for part in date_key.split("-"))
        start_local = _dt.datetime(year, month, day, tzinfo=self.tz)
        end_local = start_local + _dt.timedelta(days=1)
        return (to_iso(start_local) or "", to_iso(end_local) or "")

    def _dates_touched(self, post_ids: Sequence[str]) -> List[str]:
        """受影响帖子涉及的发现日期（按展示时区）。"""
        if not post_ids:
            # 即便没有新增，也刷新今天的索引，让运行概况与缺口保持最新
            return [local_date_key(now_utc(), self.tz)]
        dates = {local_date_key(now_utc(), self.tz)}
        placeholders = list(post_ids)
        for post_id in placeholders:
            for link in self.storage.links_for_post(post_id):
                seen = parse_iso(link["first_seen_at"])
                if seen is not None:
                    dates.add(local_date_key(seen, self.tz))
        return sorted(dates)


def _validate_date_key(date_key: str) -> None:
    try:
        _dt.datetime.strptime(date_key, "%Y-%m-%d")
    except (TypeError, ValueError):
        raise ExportError("日期必须是 YYYY-MM-DD，收到 %r" % date_key)


def _neighbor_dates(date_key: str) -> List[str]:
    base = _dt.datetime.strptime(date_key, "%Y-%m-%d")
    return [
        (base - _dt.timedelta(days=1)).strftime("%Y-%m-%d"),
        date_key,
        (base + _dt.timedelta(days=1)).strftime("%Y-%m-%d"),
    ]
