"""命令行入口（方案 §11.1）。

退出码：
  0  本轮按策略正常完成（**不代表没有遗漏** —— 覆盖范围要单独看 `status`）
  1  partial / failed，或导出失败
  2  配置或初始化错误
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional, Sequence

from . import __version__
from .config import Config, ConfigError, load_config, normalize_handle
from .collector import (
    COVERAGE_LIMITED,
    COVERAGE_UNKNOWN,
    RUN_FAILED,
    RUN_PARTIAL,
    Collector,
)
from .export import ExportError, Exporter
from .httpclient import HttpClient
from .lock import LockBusy, SingleInstanceLock
from .logsetup import setup_logging
from .providers import build_provider
from .retention import cleanup
from .storage import Storage
from .toml_compat import BACKEND as TOML_BACKEND
from .util import display_tzinfo, format_display, now_utc, parse_iso, to_iso

EXIT_OK = 0
EXIT_RUN_ISSUE = 1
EXIT_CONFIG_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="x_watch",
        description="定期收集指定 X 博主的公开帖子，存入 SQLite 并导出 Markdown。",
        epilog="退出码 0 只表示本轮按策略完成，不代表没有遗漏；覆盖情况请看 status。",
    )
    parser.add_argument("--config", default="config.toml", help="配置文件路径（默认 config.toml）")
    parser.add_argument("--verbose", action="store_true", help="输出调试级日志")
    parser.add_argument("--version", action="version", version="x-watch %s" % __version__)

    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="检查配置、数据库路径、网络和少量接口返回")
    doctor.add_argument("--no-network", action="store_true", help="只做本地检查，不发任何请求")
    doctor.add_argument("--handle", help="探测指定账号（默认取第一个启用账号）")

    run_once = sub.add_parser("run-once", help="单次采集；定时任务只调用这一条")
    run_once.add_argument("--handle", action="append", help="只采集指定账号，可重复")
    run_once.add_argument("--no-export", action="store_true", help="只采集不导出 Markdown")
    run_once.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="向 stdout 输出机器可读的 JSON 结果（日志走 stderr）",
    )
    judge_flag = run_once.add_mutually_exclusive_group()
    judge_flag.add_argument(
        "--judge",
        action="store_const",
        const=True,
        dest="judge_override",
        help="本轮强制开启重置判定（覆盖 [judge].enabled）",
    )
    judge_flag.add_argument(
        "--no-judge",
        action="store_const",
        const=False,
        dest="judge_override",
        help="本轮强制关闭重置判定",
    )
    run_once.set_defaults(judge_override=None)

    status = sub.add_parser("status", help="查看账号状态、最近运行和未解决缺口")
    status.add_argument("--runs", type=int, default=10, help="展示最近多少条运行记录")
    status.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="向 stdout 输出机器可读的 JSON 状态（日志走 stderr）",
    )

    export = sub.add_parser("export", help="只从数据库重建 Markdown，不请求上游")
    group = export.add_mutually_exclusive_group(required=True)
    group.add_argument("--date", help="重建指定日期（YYYY-MM-DD，按展示时区）")
    group.add_argument("--all", action="store_true", help="重建全部日期与帖子文件")

    judge_cmd = sub.add_parser(
        "judge", help="调用 Claude 判断新帖是否宣布用量限额重置（需在配置里开启）"
    )
    judge_cmd.add_argument("--handle", action="append", help="只判定指定账号，可重复")
    judge_cmd.add_argument("--hours", type=int, help="回看多少小时（默认取配置值）")
    judge_cmd.add_argument(
        "--dry-run", action="store_true", help="只列出候选帖子，不调用模型、不花钱"
    )
    judge_cmd.add_argument(
        "--no-notify", action="store_true", help="判定并入库，但不发通知"
    )

    forecast_cmd = sub.add_parser(
        "forecast", help="算今天还会不会重置的概率（按天缓存，输入没变时零调用）"
    )
    forecast_cmd.add_argument("--handle", action="append", help="只算指定账号，可重复")
    forecast_cmd.add_argument(
        "--force", action="store_true", help="忽略缓存强制重算（会消耗一次调用）"
    )
    forecast_cmd.add_argument(
        "--json", action="store_true", dest="as_json", help="输出 JSON 到 stdout"
    )

    backfill = sub.add_parser("backfill", help="有界补漏；不保证能取回该时间段")
    backfill.add_argument("--handle", required=True)
    backfill.add_argument("--from", dest="since", required=True, help="起始时间，带时区的 ISO 8601")
    backfill.add_argument("--to", dest="until", required=True, help="结束时间，带时区的 ISO 8601")
    backfill.add_argument("--max-pages", type=int, required=True, help="页数上限")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print("配置错误：%s" % exc, file=sys.stderr)
        return EXIT_CONFIG_ERROR

    logger = setup_logging(config.log_dir, verbose=args.verbose)

    try:
        display_tzinfo(config.app["display_timezone"])
    except ValueError as exc:
        print("配置错误：%s" % exc, file=sys.stderr)
        return EXIT_CONFIG_ERROR

    handlers = {
        "doctor": cmd_doctor,
        "run-once": cmd_run_once,
        "status": cmd_status,
        "export": cmd_export,
        "backfill": cmd_backfill,
        "judge": cmd_judge,
        "forecast": cmd_forecast,
    }
    try:
        return handlers[args.command](config, args)
    except ConfigError as exc:
        logger.error("配置错误：%s", exc)
        return EXIT_CONFIG_ERROR
    except LockBusy as exc:
        # 已有进程持有锁 → 本轮不重复执行，明确记录 skipped（方案 §10）。
        # 这是正常行为，不是错误：调用方（菜单栏 app）要能区分「被跳过」和「失败」，
        # 所以 --json 时仍然输出一个合法的信封，而不是什么都不输出。
        logger.warning("跳过本轮：%s", exc)
        if getattr(args, "as_json", False):
            import json as _json

            from .report import REPORT_SCHEMA

            print(
                _json.dumps(
                    {
                        "schema": REPORT_SCHEMA,
                        "ok": True,
                        "skipped": True,
                        "skipped_reason": str(exc),
                        "accounts": [],
                        "resets": [],
                        "errors": [],
                    },
                    ensure_ascii=False,
                )
            )
        return EXIT_OK
    except KeyboardInterrupt:
        logger.warning("被用户中断；已提交的数据不会丢失")
        return EXIT_RUN_ISSUE


# --- doctor ------------------------------------------------------------------


def cmd_doctor(config: Config, args: argparse.Namespace) -> int:
    logger = setup_logging(config.log_dir, verbose=args.verbose)
    problems: List[str] = []

    logger.info("x-watch %s", __version__)
    logger.info("Python %s", sys.version.split()[0])
    logger.info("TOML 解析后端：%s", TOML_BACKEND)
    logger.info("配置文件：%s", os.path.abspath(config.path))
    logger.info("")

    logger.info("== 配置 ==")
    logger.info("provider=%s  auto_fallback=%s  include_replies=%s",
                config.source["provider"], config.source["auto_fallback"],
                config.source["include_replies"])
    logger.info(
        "轮询间隔=%d 分钟（供任务注册脚本读取；改了要重新注册系统任务）",
        config.collection["interval_minutes"],
    )
    logger.info(
        "页数预算：正常 %d 页/账号，首次导入 %d 页；总请求上限 %d；运行时长上限 %d 秒",
        config.collection["max_pages_per_account"],
        config.collection["bootstrap_max_pages"],
        config.collection["max_total_requests_per_run"],
        config.collection["max_run_seconds"],
    )
    enabled = config.enabled_accounts()
    logger.info(
        "账号：共 %d 个，启用 %d 个 —— %s",
        len(config.accounts),
        len(enabled),
        ", ".join("@" + a.handle for a in enabled),
    )
    logger.info("")

    logger.info("== 路径与写权限 ==")
    for label, path in (
        ("数据库目录", os.path.dirname(config.database_path) or "."),
        ("输出目录", config.output_dir),
        ("原始响应目录", config.raw_dir),
        ("日志目录", config.log_dir),
    ):
        ok, detail = _check_writable(path)
        logger.info("%-12s %-50s %s", label, path, detail)
        if not ok:
            problems.append("%s 不可写：%s（%s）" % (label, path, detail))
    logger.info("")

    logger.info("== 数据库 ==")
    try:
        with Storage(config.database_path) as storage:
            counts = storage.counts()
            logger.info(
                "可读写。帖子 %d 条，账号关联 %d 条，运行记录 %d 条",
                counts["posts"],
                counts["links"],
                counts["runs"],
            )
            for row in storage.all_source_states():
                until = parse_iso(row["next_allowed_at"])
                if until and until > now_utc():
                    logger.warning(
                        "上游 %s 仍在退避中，下次允许时间 %s（%s）",
                        row["source_key"],
                        row["next_allowed_at"],
                        row["note"] or "",
                    )
    except Exception as exc:  # sqlite3.Error 等
        logger.error("数据库不可用：%s", exc)
        problems.append("数据库不可用：%s" % exc)
    logger.info("")

    logger.info("== 单实例锁 ==")
    try:
        with SingleInstanceLock(config.lock_path):
            logger.info("可取得：%s", config.lock_path)
    except LockBusy as exc:
        logger.warning("当前被占用：%s（如果定时任务正在跑，这是正常的）", exc)
    logger.info("")

    logger.info("== 重置判定（调用大模型）==")
    from .judge import JUDGE_VERSION, check_backend
    from .notify import available as notifier_available

    if not config.judge["enabled"]:
        logger.info("已关闭（[judge].enabled = false）。不会发出任何模型请求。")
    else:
        logger.info(
            "已开启：后端 %s，模型 %s，effort=%s，阈值 %.2f，判定版本 %s",
            config.judge["backend"],
            config.judge["model"],
            config.judge["effort"],
            float(config.judge["threshold"]),
            JUDGE_VERSION,
        )
        if config.judge["backend"] == "claude_cli":
            logger.info("计费：走本机 Claude Code 的订阅额度，不使用 API key")
            logger.info("effort 参数只对 api 后端生效；claude_cli 由 CLI 自己决定")
        else:
            logger.info("计费：按 Anthropic API 定价单独计费")
        logger.info(
            "预算：每轮最多 %d 次，24 小时内最多 %d 次；回看 %d 小时",
            config.judge["max_calls_per_run"],
            config.judge["max_calls_per_day"],
            config.judge["lookback_hours"],
        )
        ok, detail = check_backend(config.judge)
        logger.info("后端可用性：%s", detail)
        if not ok:
            problems.append("判定已开启但不可用：%s" % detail)
        ok, detail = notifier_available(config.judge["notifier"])
        logger.info("通知方式 %s：%s", config.judge["notifier"], detail)
        if not ok:
            problems.append("通知方式不可用：%s" % detail)
        if config.judge["notifier"] == "none":
            logger.warning("notifier = none：命中只写日志和每日索引，不发通知")
    logger.info("")

    logger.info("== 网络与代理 ==")
    import urllib.request

    proxies = urllib.request.getproxies()
    if proxies:
        # 只显示存在哪些代理键，不打印可能含凭证的完整地址
        logger.info("检测到代理环境变量：%s", ", ".join(sorted(proxies)))
    else:
        logger.info("未检测到代理环境变量（HTTP_PROXY / HTTPS_PROXY）")
    logger.info(
        "提示：浏览器能访问不代表计划任务能访问。请在计划任务实际使用的账号下再跑一次 doctor。"
    )

    if args.no_network:
        logger.info("已指定 --no-network，跳过接口探测。")
    else:
        handle = normalize_handle(args.handle) if args.handle else (
            enabled[0].handle if enabled else None
        )
        if handle is None:
            problems.append("没有可探测的账号")
        else:
            problems.extend(_probe_source(config, handle, logger))

    logger.info("")
    if problems:
        logger.error("== doctor 发现 %d 个问题 ==", len(problems))
        for item in problems:
            logger.error("- %s", item)
        return EXIT_RUN_ISSUE
    logger.info("== doctor 通过 ==")
    logger.info(
        "注意：doctor 通过只说明当前环境能连通并取到样本，不代表覆盖完整或长期可用。"
    )
    return EXIT_OK


def _check_writable(path: str) -> "tuple[bool, str]":
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        return False, "无法创建：%s" % exc
    if not os.access(path, os.W_OK):
        return False, "无写权限"
    return True, "可写"


def _probe_source(config: Config, handle: str, logger) -> List[str]:
    """少量请求探测接口，只取一页，不写库（方案 §11.1）。"""
    problems: List[str] = []
    client = HttpClient(
        user_agent=config.source["user_agent"],
        connect_timeout=config.collection["connect_timeout_seconds"],
        read_timeout=config.collection["read_timeout_seconds"],
        max_attempts=1,
        max_total_requests=2,
        max_run_seconds=min(120, config.collection["max_run_seconds"]),
    )
    provider = build_provider(config.source["provider"], client, config.source)
    logger.info("探测 %s：%s", provider.name, provider.probe_url(handle))
    page = provider.fetch_page(handle)
    if not page.ok:
        logger.error("探测失败：%s / %s", page.error_kind, page.error_detail)
        problems.append("接口探测失败：%s（%s）" % (page.error_kind, page.error_detail[:200]))
        return problems

    logger.info(
        "返回 %d 条原始条目，标准化 %d 条，解析失败 %d 条，分页支持=%s，下一页游标=%s",
        page.returned_count,
        len(page.posts),
        page.parse_failures,
        page.supports_pagination,
        "有" if page.next_cursor else "无",
    )
    if page.warnings:
        for warning in page.warnings[:10]:
            logger.info("提示：%s", warning)
    if page.posts:
        sample = page.posts[0]
        logger.info(
            "样本：%s @%s %s %s",
            sample.post_id,
            sample.author_handle,
            sample.post_type,
            to_iso(sample.published_at),
        )
        relations = {}
        for post in page.posts:
            relations[post.relation] = relations.get(post.relation, 0) + 1
        logger.info("关联类型分布：%s", relations)
    else:
        logger.warning("标准化后没有任何帖子。源返回空不等于账号没有发帖。")
    return problems


# --- run-once ----------------------------------------------------------------


def cmd_run_once(config: Config, args: argparse.Namespace) -> int:
    logger = setup_logging(config.log_dir, verbose=args.verbose)
    exit_code = EXIT_OK
    as_json = getattr(args, "as_json", False)
    errors: List[str] = []

    with SingleInstanceLock(config.lock_path):
        with Storage(config.database_path) as storage:
            collector = Collector(config, storage)
            handles = [normalize_handle(h) for h in (args.handle or [])] or None
            try:
                summary = collector.run_once(handles)
            except ValueError as exc:
                logger.error("%s", exc)
                return EXIT_CONFIG_ERROR

            totals = summary.totals()
            logger.info("")
            logger.info(
                "本轮结束：账号 %d，新增 %d，内容更新 %d，取页 %d，解析失败 %d，请求 %d/%d",
                totals["accounts"],
                totals["new"],
                totals["updated"],
                totals["pages"],
                totals["parse_failures"],
                collector.client.requests_used,
                collector.client.max_total_requests,
            )
            if summary.aborted_reason:
                logger.warning("本轮提前结束：%s", summary.aborted_reason)

            # 数据库先于 Markdown：导出失败不影响已入库数据
            if not args.no_export:
                try:
                    result = Exporter(config, storage).export_after_run(summary.affected_post_ids)
                    logger.info(
                        "导出：帖子文件 %d 个，日期索引 %s",
                        result["posts_written"],
                        ", ".join(result["dates_written"]) or "无",
                    )
                    for error in result["errors"]:
                        logger.error("导出错误：%s", error)
                        errors.append("导出错误：%s" % error)
                        exit_code = EXIT_RUN_ISSUE
                except (ExportError, OSError, ValueError) as exc:
                    logger.error("导出失败（采集结果已保存，可用 export 命令单独重建）：%s", exc)
                    errors.append("导出失败：%s" % exc)
                    exit_code = EXIT_RUN_ISSUE

            # 判定放在采集与导出**之后**：调模型失败绝不能影响已入库的数据。
            judge_result = None
            judge_override = getattr(args, "judge_override", None)
            judge_wanted = (
                config.judge["enabled"] if judge_override is None else judge_override
            )
            if judge_wanted and not args.no_export:
                try:
                    # 必须把 handles 传下去：只采集了 thsottiaux 却去判定
                    # bcherny/simonw 的帖子，是在给没监控的账号白花模型调用，
                    # 还会挤占 max_calls_per_run 预算。
                    sent, judge_result = _run_judge(config, storage, logger, handles)
                    if sent < 0:
                        errors.append("判定后端不可用")
                        exit_code = EXIT_RUN_ISSUE
                    elif config.export["write_daily_index"]:
                        # 判定结论要落进当天索引
                        Exporter(config, storage).export_after_run([])
                except Exception as exc:  # 判定是附加功能，任何异常都不许中断本轮
                    logger.error("判定阶段出错（采集结果不受影响）：%s", exc)
                    errors.append("判定阶段出错：%s" % exc)
                    exit_code = EXIT_RUN_ISSUE

                # 预测「今天还会不会重置」。按天 + 输入指纹缓存，
                # 所以哪怕 1 分钟一轮，模型调用也只在真有新帖时发生。
                try:
                    # 判定关闭时只读缓存，不发起新的模型调用
                    _run_forecast(
                        config, storage, logger, handles,
                        allow_model_calls=bool(judge_wanted),
                    )
                except Exception as exc:
                    logger.warning("预测出错（不影响采集与判定）：%s", exc)

                # 补中文译文。同样只在判定开启时才会调模型。
                try:
                    _run_translate(
                        config, storage, logger, handles,
                        allow_model_calls=bool(judge_wanted),
                    )
                except Exception as exc:
                    logger.warning("翻译出错（界面会显示原文）：%s", exc)

            for note in cleanup(
                config.raw_dir,
                config.retention["raw_days"],
                config.log_dir,
                config.retention["log_days"],
            ):
                logger.info("%s", note)

            for note in summary.notes:
                logger.warning("%s", note)

            limited = [
                o for o in summary.outcomes if o.coverage in (COVERAGE_LIMITED, COVERAGE_UNKNOWN)
            ]
            if limited:
                logger.warning(
                    "以下账号本轮未能证明覆盖完整：%s",
                    ", ".join("@%s(%s)" % (o.handle, o.coverage) for o in limited),
                )
            if summary.worst_status in (RUN_FAILED, RUN_PARTIAL):
                exit_code = EXIT_RUN_ISSUE

            if as_json:
                import json as _json

                from .report import build_tick_report, summarize_judge, summarize_run

                report = build_tick_report(
                    config,
                    storage,
                    collection_summary=summarize_run(summary),
                    judge_summary=(
                        summarize_judge(judge_result, float(config.judge["threshold"]))
                        if judge_result is not None
                        else {"enabled": bool(config.judge["enabled"])}
                    ),
                    errors=errors,
                    handles=handles,
                )
                report["exit_code"] = exit_code
                # 本轮实际是否判定（可能被 --judge/--no-judge 覆盖），
                # 和 config 里的 enabled 区分开，界面才不会显示错
                report["judge"]["enabled_effective"] = bool(judge_wanted)
                # JSON 只写 stdout；上面所有日志都已经走 stderr
                print(_json.dumps(report, ensure_ascii=False))

    if exit_code == EXIT_OK:
        logger.info("退出码 0：本轮按策略完成。这不代表没有遗漏，覆盖情况请看 status。")
    return exit_code


# --- status ------------------------------------------------------------------


def cmd_status(config: Config, args: argparse.Namespace) -> int:
    logger = setup_logging(config.log_dir, verbose=args.verbose)
    tz = display_tzinfo(config.app["display_timezone"])

    if getattr(args, "as_json", False):
        import json as _json

        from .report import build_status_report

        with Storage(config.database_path) as storage:
            print(
                _json.dumps(
                    build_status_report(config, storage), ensure_ascii=False, indent=2
                )
            )
        return EXIT_OK

    with Storage(config.database_path) as storage:
        counts = storage.counts()
        logger.info("数据库：%s", config.database_path)
        logger.info(
            "帖子 %d 条，账号关联 %d 条，运行记录 %d 条",
            counts["posts"], counts["links"], counts["runs"],
        )
        logger.info("")

        logger.info("== 账号状态 ==")
        states = storage.all_account_states()
        if not states:
            logger.info("还没有任何账号状态记录（尚未运行过 run-once）")
        for state in states:
            handle = state["monitored_handle"]
            configured = config.find_account(handle)
            logger.info("@%s%s", handle, "" if configured else "（已不在配置中）")
            logger.info("    最近尝试        %s", _fmt(state["last_attempt_at"], tz))
            logger.info("    最近合法响应    %s", _fmt(state["last_valid_response_at"], tz))
            logger.info("    扫描边界        %s", _fmt(state["last_scan_boundary_at"], tz))
            logger.info(
                "    覆盖结论        %s%s",
                state["coverage_status"] or "-",
                "" if state["coverage_status"] not in ("limited", "unknown")
                else "  ← 未能证明这段时间没有遗漏",
            )
            if state["gap_from_at"]:
                logger.warning(
                    "    疑似缺口        %s .. %s",
                    _fmt(state["gap_from_at"], tz),
                    _fmt(state["gap_to_at"], tz),
                )
            else:
                logger.info("    疑似缺口        无记录（不等于证明没有遗漏）")
            logger.info("    帖子关联数      %d", storage.account_post_count(handle))
            failures = int(state["consecutive_failures"] or 0)
            if failures:
                logger.warning("    连续失败        %d 次", failures)
            if state["next_allowed_at"]:
                until = parse_iso(state["next_allowed_at"])
                if until and until > now_utc():
                    logger.warning("    退避至          %s", _fmt(state["next_allowed_at"], tz))
            if int(state["deferred"] or 0):
                logger.warning("    上轮被延后，下轮优先处理")
        logger.info("")

        logger.info("== 上游退避状态 ==")
        source_states = storage.all_source_states()
        if not source_states:
            logger.info("无记录")
        for row in source_states:
            until = parse_iso(row["next_allowed_at"])
            active = until is not None and until > now_utc()
            logger.info(
                "%s：%s 连续限流 %d 次 %s",
                row["source_key"],
                ("退避至 %s" % _fmt(row["next_allowed_at"], tz)) if active else "正常",
                int(row["consecutive_throttle"] or 0),
                row["note"] or "",
            )
        logger.info("")

        logger.info("== 最近 %d 条运行记录 ==", args.runs)
        runs = storage.recent_runs(args.runs)
        if not runs:
            logger.info("无记录")
        for run in runs:
            logger.info(
                "%s @%-15s %-11s %-16s 页=%d 新增=%d 更新=%d 失败条目=%d %s",
                _fmt(run["started_at"], tz),
                run["monitored_handle"],
                run["mode"],
                "%s/%s" % (run["status"], run["coverage_status"] or "-"),
                int(run["pages_fetched"] or 0),
                int(run["new_count"] or 0),
                int(run["updated_count"] or 0),
                int(run["parse_failures"] or 0),
                (run["error_kind"] or ""),
            )

        gaps = storage.unresolved_gaps()
        logger.info("")
        if gaps:
            logger.warning("== 未解决的缺口 %d 个 ==", len(gaps))
            for gap in gaps:
                logger.warning(
                    "@%s %s .. %s —— 用 backfill 手动处理（需指定时间范围与页数上限）",
                    gap["monitored_handle"],
                    _fmt(gap["gap_from_at"], tz),
                    _fmt(gap["gap_to_at"], tz),
                )
        else:
            logger.info("没有记录在案的缺口。这不是「一条不漏」的证明。")
    return EXIT_OK


def _fmt(iso: Optional[str], tz) -> str:
    moment = parse_iso(iso)
    return format_display(moment, tz) if moment else "-"


# --- export ------------------------------------------------------------------


def cmd_export(config: Config, args: argparse.Namespace) -> int:
    logger = setup_logging(config.log_dir, verbose=args.verbose)
    with Storage(config.database_path) as storage:
        exporter = Exporter(config, storage)
        try:
            result = exporter.rebuild_all() if args.all else exporter.rebuild_date(args.date)
        except ExportError as exc:
            logger.error("导出错误：%s", exc)
            return EXIT_CONFIG_ERROR
        logger.info(
            "重建完成：帖子文件 %d 个，日期索引 %d 个",
            result["posts_written"],
            len(result["dates_written"]),
        )
        for error in result["errors"]:
            logger.error("%s", error)
        return EXIT_RUN_ISSUE if result["errors"] else EXIT_OK


# --- backfill ----------------------------------------------------------------


def cmd_backfill(config: Config, args: argparse.Namespace) -> int:
    logger = setup_logging(config.log_dir, verbose=args.verbose)
    since = parse_iso(args.since)
    until = parse_iso(args.until)
    if since is None or until is None:
        logger.error("--from / --to 必须是带时区的 ISO 8601，例如 2026-09-15T00:00:00Z")
        return EXIT_CONFIG_ERROR
    if since >= until:
        logger.error("--from 必须早于 --to")
        return EXIT_CONFIG_ERROR
    if args.max_pages < 1:
        logger.error("--max-pages 必须 >= 1")
        return EXIT_CONFIG_ERROR

    logger.warning(
        "backfill 不保证能取回 %s .. %s；它同样遵守来源退避、请求间隔与请求预算。",
        to_iso(since),
        to_iso(until),
    )

    with SingleInstanceLock(config.lock_path):
        with Storage(config.database_path) as storage:
            collector = Collector(config, storage)
            try:
                outcome = collector.backfill(
                    normalize_handle(args.handle), since, until, args.max_pages
                )
            except ValueError as exc:
                logger.error("%s", exc)
                return EXIT_CONFIG_ERROR

            logger.info(outcome.summary_line())
            for note in outcome.notes:
                logger.info("  - %s", note)

            try:
                result = Exporter(config, storage).export_after_run(outcome.affected_post_ids)
                logger.info(
                    "导出：帖子文件 %d 个，日期索引 %s",
                    result["posts_written"],
                    ", ".join(result["dates_written"]) or "无",
                )
            except (ExportError, OSError) as exc:
                logger.error("导出失败（采集结果已保存）：%s", exc)
                return EXIT_RUN_ISSUE

            if outcome.status in (RUN_FAILED, RUN_PARTIAL):
                return EXIT_RUN_ISSUE
    return EXIT_OK


# --- judge -------------------------------------------------------------------


def _run_judge(
    config: Config,
    storage: Storage,
    logger,
    handles: Optional[Sequence[str]] = None,
    hours: Optional[int] = None,
    dry_run: bool = False,
    notify_enabled: bool = True,
) -> "tuple":
    """判定 + 通知。返回 `(已发通知数, JudgeRun|None)`；后端不可用时第一项为 -1。"""
    import datetime as _dt

    from .judge import JUDGE_VERSION, JudgeUnavailable, run_judgments
    from .notify import send as send_notification

    judge_cfg = config.judge
    targets = [normalize_handle(h) for h in (handles or [])] or [
        a.handle for a in config.enabled_accounts()
    ]
    window = hours if hours is not None else int(judge_cfg["lookback_hours"])
    since = now_utc() - _dt.timedelta(hours=window)

    logger.info(
        "判定范围：账号 %s，回看 %d 小时，模型 %s，effort=%s，阈值 %.2f",
        ", ".join("@" + h for h in targets),
        window,
        judge_cfg["model"],
        judge_cfg["effort"],
        float(judge_cfg["threshold"]),
    )

    try:
        result = run_judgments(storage, judge_cfg, targets, since, dry_run=dry_run)
    except JudgeUnavailable as exc:
        logger.error("判定功能不可用：%s", exc)
        return -1, None

    logger.info(result.summary_line())
    for note in result.notes:
        logger.warning("%s", note)

    if dry_run:
        return 0, result

    sent = 0
    for verdict in result.triggered:
        if storage.judgment_notified(verdict.post_id, verdict.content_hash, JUDGE_VERSION):
            continue
        post = storage.get_post(verdict.post_id)
        url = (post["url"] if post else "") or ""
        title = "Tibo reset? p=%.2f" % verdict.probability
        body = "%s\n%s\n%s" % (
            (post["text"] if post else "") or "（无文字内容）",
            verdict.reasoning,
            url,
        )
        if not notify_enabled or judge_cfg["notifier"] == "none":
            logger.warning("【命中未通知】%s —— %s", title, verdict.summary())
        elif send_notification(
            judge_cfg["notifier"], judge_cfg["notify_command"], title, body
        ):
            sent += 1
        with storage.transaction():
            # 通知失败也标记，避免每轮重复轰炸；结论仍留在库里和每日索引里
            storage.mark_judgment_notified(
                verdict.post_id, verdict.content_hash, JUDGE_VERSION
            )
    if result.triggered:
        logger.warning(
            "本轮命中 %d 条疑似重置公告，已发送通知 %d 条", len(result.triggered), sent
        )
    return sent, result


def _run_forecast(
    config: Config,
    storage: Storage,
    logger,
    handles: Optional[Sequence[str]] = None,
    force: bool = False,
    allow_model_calls: bool = True,
) -> list:
    """给每个账号算/取今天的重置概率。返回 Forecast 列表。

    `allow_model_calls=False` 时只读缓存 —— 判定关闭时不能偷偷花模型调用。
    """
    from .forecast import compute_forecast

    targets = [normalize_handle(h) for h in (handles or [])] or [
        a.handle for a in config.enabled_accounts()
    ]
    out = []
    for handle in targets:
        try:
            forecast = compute_forecast(
                storage,
                config.judge,
                handle,
                config.app["display_timezone"],
                force=force,
                allow_model_calls=allow_model_calls,
            )
        except Exception as exc:
            logger.warning("@%s 预测失败：%s", handle, exc)
            continue
        if forecast is None:
            continue
        out.append(forecast)
        logger.info("%s", forecast.summary())
    return out


def _run_translate(
    config: Config,
    storage: Storage,
    logger,
    handles: Optional[Sequence[str]] = None,
    allow_model_calls: bool = True,
    per_handle: int = 8,
) -> int:
    """给界面会显示的那几条帖子补中文译文。返回新翻的条数。

    只翻**界面要显示**的（每账号前 N 条 + 命中），不翻库里全部 —— 翻译按条收费。
    每轮最多翻一批，所以积压会在几轮内自然消化，而不是一次巨额调用。
    """
    from .forecast import recent_self_posts
    from .translate import translate_posts

    targets = [normalize_handle(h) for h in (handles or [])] or [
        a.handle for a in config.enabled_accounts()
    ]
    candidates = []
    seen = set()
    for handle in targets:
        for row in recent_self_posts(storage, handle, limit=per_handle):
            if row["post_id"] in seen:
                continue
            seen.add(row["post_id"])
            candidates.append(row)
    # 命中的重置公告优先，它们最可能被细读
    for row in storage.recent_resets(10, 0.0, handles=targets):
        if row["post_id"] in seen:
            continue
        seen.add(row["post_id"])
        candidates.append(dict(row))

    if not candidates:
        return 0
    before = len(
        storage.translations_for([r["post_id"] for r in candidates], "zh")
    )
    translate_posts(
        storage, config.judge, candidates, allow_model_calls=allow_model_calls
    )
    after = len(storage.translations_for([r["post_id"] for r in candidates], "zh"))
    added = max(0, after - before)
    if added:
        logger.info("新增中文译文 %d 条（共 %d/%d 条已译）", added, after, len(candidates))
    return added


def cmd_forecast(config: Config, args: argparse.Namespace) -> int:
    logger = setup_logging(config.log_dir, verbose=args.verbose)
    # 读缓存零成本，所以不在这里硬拦；只有「需要新算」时才要求判定已开启或 --force。
    allow_calls = bool(config.judge["enabled"]) or bool(getattr(args, "force", False))
    if not allow_calls:
        logger.info(
            "[judge].enabled = false：只读已有预测，不会调用模型。"
            "要强制重算请加 --force。"
        )

    with SingleInstanceLock(config.lock_path):
        with Storage(config.database_path) as storage:
            forecasts = _run_forecast(
                config, storage, logger, args.handle,
                force=args.force, allow_model_calls=allow_calls,
            )
            if getattr(args, "as_json", False):
                import json as _json

                print(
                    _json.dumps(
                        [f.to_json() for f in forecasts], ensure_ascii=False, indent=2
                    )
                )
    return EXIT_OK


def cmd_judge(config: Config, args: argparse.Namespace) -> int:
    logger = setup_logging(config.log_dir, verbose=args.verbose)
    if not config.judge["enabled"] and not args.dry_run:
        logger.error(
            "[judge].enabled = false。这个功能会调用收费 API，必须显式开启后才会运行。"
        )
        logger.error("只想看候选帖子可以加 --dry-run（不调用模型、不花钱）。")
        return EXIT_CONFIG_ERROR

    with SingleInstanceLock(config.lock_path):
        with Storage(config.database_path) as storage:
            sent, _judge_result = _run_judge(
                config,
                storage,
                logger,
                handles=args.handle,
                hours=args.hours,
                dry_run=args.dry_run,
                notify_enabled=not args.no_notify,
            )
            if sent < 0:
                return EXIT_RUN_ISSUE
            # 判定结果要进当天索引
            if not args.dry_run and config.export["write_daily_index"]:
                try:
                    Exporter(config, storage).export_after_run([])
                except (ExportError, OSError) as exc:
                    logger.error("刷新每日索引失败：%s", exc)
                    return EXIT_RUN_ISSUE
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
