"""配置加载与校验。

设计要点：
- 相对路径一律相对于**配置文件所在目录**解析，不受当前工作目录影响（方案 §8）。
- 参数范围、账号格式、重复账号在加载期就报错，不留到采集中途。
- 每个账号生成 `scope_hash`；采集范围变化时旧扫描边界不能直接复用（方案 §7.3）。
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from .toml_compat import TomlError, load_toml

HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
KNOWN_PROVIDERS = ("fxtwitter_json", "fxtwitter_rss")

# (键, 最小值, 最大值)
_INT_RANGES = (
    ("interval_minutes", 5, 1440),
    ("max_pages_per_account", 1, 50),
    ("bootstrap_hours", 1, 24 * 30),
    ("bootstrap_max_pages", 1, 50),
    ("overlap_hours", 0, 24 * 7),
    ("request_gap_seconds", 0, 600),
    ("connect_timeout_seconds", 1, 120),
    ("read_timeout_seconds", 1, 600),
    ("max_attempts_per_request", 1, 5),
    ("max_total_requests_per_run", 1, 2000),
    ("max_run_seconds", 30, 24 * 3600),
)

_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "app": {
        "database_path": "data/x-watch.sqlite3",
        "output_dir": "output",
        "raw_dir": "data/raw",
        "log_dir": "logs",
        "display_timezone": "local",
    },
    "source": {
        "provider": "fxtwitter_json",
        "auto_fallback": False,
        "page_size": 50,
        "include_replies": True,
        "user_agent": "x-watch/0.1 (personal archive tool)",
    },
    "collection": {
        "interval_minutes": 60,
        "max_pages_per_account": 3,
        "bootstrap_hours": 72,
        "bootstrap_max_pages": 3,
        "overlap_hours": 6,
        "request_gap_seconds": 5,
        "connect_timeout_seconds": 10,
        "read_timeout_seconds": 30,
        "max_attempts_per_request": 2,
        "max_total_requests_per_run": 30,
        "max_run_seconds": 600,
    },
    "export": {
        "include_reposts": False,
        "include_unknown": True,
        "include_context": False,
        "include_quoted": False,
        "write_post_files": True,
        "write_daily_index": True,
    },
    "retention": {
        "raw_days": 7,
        "log_days": 30,
    },
    # 调用收费 API 的功能。默认关闭 —— 方案 §9.3：收费流程不能默认开启。
    "judge": {
        "enabled": False,
        # api = Anthropic SDK（需 API key，输出有约束解码）
        # claude_cli = 本机已登录的 Claude Code（用订阅额度，输出靠指令约束）
        "backend": "claude_cli",
        "claude_executable": "claude",
        "cli_timeout_seconds": 180,
        "model": "claude-opus-5",
        "effort": "medium",
        "max_tokens": 2048,
        "threshold": 0.7,
        "max_calls_per_run": 20,
        "max_calls_per_day": 200,
        "max_text_chars": 4000,
        "only_self_posts": True,
        "lookback_hours": 24,
        "notifier": "none",
        "notify_command": "",
    },
}

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
JUDGE_BACKENDS = ("api", "claude_cli")


class ConfigError(Exception):
    """配置无效。调用方应以退出码 2 结束。"""


class Account:
    __slots__ = ("handle", "enabled", "scope_hash")

    def __init__(self, handle: str, enabled: bool, scope_hash: str) -> None:
        self.handle = handle
        self.enabled = enabled
        self.scope_hash = scope_hash

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return "Account(%r, enabled=%r)" % (self.handle, self.enabled)


class Config:
    def __init__(
        self,
        path: str,
        app: Dict[str, Any],
        source: Dict[str, Any],
        collection: Dict[str, Any],
        export: Dict[str, Any],
        retention: Dict[str, Any],
        accounts: List[Account],
        judge: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.path = path
        self.base_dir = os.path.dirname(os.path.abspath(path))
        self.app = app
        self.source = source
        self.collection = collection
        self.export = export
        self.retention = retention
        self.accounts = accounts
        self.judge = judge or dict(_DEFAULTS["judge"])

    # --- 路径 ---------------------------------------------------------------
    def resolve(self, relative: str) -> str:
        if os.path.isabs(relative):
            return os.path.normpath(relative)
        return os.path.normpath(os.path.join(self.base_dir, relative))

    @property
    def database_path(self) -> str:
        return self.resolve(self.app["database_path"])

    @property
    def output_dir(self) -> str:
        return self.resolve(self.app["output_dir"])

    @property
    def raw_dir(self) -> str:
        return self.resolve(self.app["raw_dir"])

    @property
    def log_dir(self) -> str:
        return self.resolve(self.app["log_dir"])

    @property
    def lock_path(self) -> str:
        return self.database_path + ".lock"

    def enabled_accounts(self) -> List[Account]:
        return [a for a in self.accounts if a.enabled]

    def find_account(self, handle: str) -> Optional[Account]:
        key = normalize_handle(handle)
        for account in self.accounts:
            if account.handle == key:
                return account
        return None


def normalize_handle(raw: Any) -> str:
    """去掉 @ 与首尾空白并转小写。X 用户名大小写不敏感，统一用小写做键。"""
    if not isinstance(raw, str):
        raise ConfigError("账号必须是字符串，收到 %r" % (raw,))
    handle = raw.strip()
    if handle.startswith("@"):
        handle = handle[1:]
    # 容忍粘贴整条个人主页链接
    if "/" in handle:
        handle = handle.rstrip("/").rsplit("/", 1)[-1]
    return handle.lower()


def compute_scope_hash(handle: str, source: Dict[str, Any]) -> str:
    """影响采集范围的配置指纹。变了就必须重新做覆盖验证，不能沿用旧边界。"""
    parts = (
        handle,
        str(source["provider"]),
        "replies=%s" % bool(source["include_replies"]),
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def load_config(path: str) -> Config:
    if not os.path.isfile(path):
        raise ConfigError("配置文件不存在：%s（可从 config.example.toml 复制）" % path)
    try:
        data = load_toml(path)
    except TomlError as exc:
        raise ConfigError(str(exc)) from exc
    except OSError as exc:
        raise ConfigError("无法读取配置文件 %s：%s" % (path, exc)) from exc

    unknown_tables = set(data) - set(_DEFAULTS) - {"accounts"}
    if unknown_tables:
        raise ConfigError("配置中存在无法识别的表：%s" % ", ".join(sorted(unknown_tables)))

    sections: Dict[str, Dict[str, Any]] = {}
    for name, defaults in _DEFAULTS.items():
        raw = data.get(name, {})
        if not isinstance(raw, dict):
            raise ConfigError("[%s] 必须是表" % name)
        unknown_keys = set(raw) - set(defaults)
        if unknown_keys:
            raise ConfigError(
                "[%s] 存在无法识别的键：%s" % (name, ", ".join(sorted(unknown_keys)))
            )
        merged = dict(defaults)
        merged.update(raw)
        sections[name] = merged

    app = sections["app"]
    source = sections["source"]
    collection = sections["collection"]
    export = sections["export"]
    retention = sections["retention"]

    # --- [app] ---
    for key in ("database_path", "output_dir", "raw_dir", "log_dir", "display_timezone"):
        if not isinstance(app[key], str) or not app[key].strip():
            raise ConfigError("[app].%s 必须是非空字符串" % key)

    # --- [source] ---
    if source["provider"] not in KNOWN_PROVIDERS:
        raise ConfigError(
            "[source].provider 必须是 %s 之一，收到 %r"
            % (" / ".join(KNOWN_PROVIDERS), source["provider"])
        )
    for key in ("auto_fallback", "include_replies"):
        _require_bool(source, key, "source")
    if not isinstance(source["page_size"], int) or isinstance(source["page_size"], bool):
        raise ConfigError("[source].page_size 必须是整数")
    if not 1 <= source["page_size"] <= 100:
        raise ConfigError("[source].page_size 必须在 1..100 之间")
    if not isinstance(source["user_agent"], str) or not source["user_agent"].strip():
        raise ConfigError("[source].user_agent 必须是非空字符串")
    if source["provider"] == "fxtwitter_rss" and source["auto_fallback"]:
        raise ConfigError("provider 已是 fxtwitter_rss 时不应再开启 auto_fallback")

    # --- [collection] ---
    for key, low, high in _INT_RANGES:
        value = collection[key]
        if not isinstance(value, int) or isinstance(value, bool):
            raise ConfigError("[collection].%s 必须是整数" % key)
        if not low <= value <= high:
            raise ConfigError("[collection].%s 必须在 %d..%d 之间，收到 %d" % (key, low, high, value))

    # 预算必须够跑完一个账号的分页，否则账号会被结构性饿死
    max_pages = max(collection["max_pages_per_account"], collection["bootstrap_max_pages"])
    if collection["max_total_requests_per_run"] < max_pages:
        raise ConfigError(
            "[collection].max_total_requests_per_run(%d) 小于单账号最大页数(%d)，"
            "任何账号都无法跑完一轮分页"
            % (collection["max_total_requests_per_run"], max_pages)
        )
    # 预算含重试，粗略核对一下运行时长是否够串行跑完
    min_seconds = max_pages * collection["request_gap_seconds"]
    if collection["max_run_seconds"] < min_seconds:
        raise ConfigError(
            "[collection].max_run_seconds(%d) 不足以按 request_gap_seconds=%d 串行取 %d 页"
            % (collection["max_run_seconds"], collection["request_gap_seconds"], max_pages)
        )

    # --- [export] ---
    for key in export:
        _require_bool(export, key, "export")

    # --- [retention] ---
    for key in ("raw_days", "log_days"):
        value = retention[key]
        if not isinstance(value, int) or isinstance(value, bool):
            raise ConfigError("[retention].%s 必须是整数" % key)
        if not 0 <= value <= 3650:
            raise ConfigError("[retention].%s 必须在 0..3650 之间" % key)

    # --- [judge] ---
    judge = sections["judge"]
    for key in ("enabled", "only_self_posts"):
        _require_bool(judge, key, "judge")
    for key in ("model", "effort", "notifier", "notify_command", "backend", "claude_executable"):
        if not isinstance(judge[key], str):
            raise ConfigError("[judge].%s 必须是字符串" % key)
    if not judge["model"].strip():
        raise ConfigError("[judge].model 不能为空")
    if judge["backend"] not in JUDGE_BACKENDS:
        raise ConfigError(
            "[judge].backend 必须是 %s 之一，收到 %r"
            % (" / ".join(JUDGE_BACKENDS), judge["backend"])
        )
    if not judge["claude_executable"].strip():
        raise ConfigError("[judge].claude_executable 不能为空")
    if not isinstance(judge["cli_timeout_seconds"], int) or isinstance(
        judge["cli_timeout_seconds"], bool
    ):
        raise ConfigError("[judge].cli_timeout_seconds 必须是整数")
    if not 10 <= judge["cli_timeout_seconds"] <= 1800:
        raise ConfigError("[judge].cli_timeout_seconds 必须在 10..1800 之间")
    if judge["effort"] not in EFFORT_LEVELS:
        raise ConfigError(
            "[judge].effort 必须是 %s 之一，收到 %r"
            % (" / ".join(EFFORT_LEVELS), judge["effort"])
        )
    from .notify import NOTIFIERS  # 局部导入避免循环依赖

    if judge["notifier"] not in NOTIFIERS:
        raise ConfigError(
            "[judge].notifier 必须是 %s 之一，收到 %r"
            % (" / ".join(NOTIFIERS), judge["notifier"])
        )
    if judge["notifier"] == "command" and not judge["notify_command"].strip():
        raise ConfigError("[judge].notifier = \"command\" 时必须设置 notify_command")
    if not isinstance(judge["threshold"], (int, float)) or isinstance(
        judge["threshold"], bool
    ):
        raise ConfigError("[judge].threshold 必须是 0..1 的数值")
    if not 0.0 <= float(judge["threshold"]) <= 1.0:
        raise ConfigError("[judge].threshold 必须在 0..1 之间")
    for key, low, high in (
        ("max_tokens", 256, 64000),
        ("max_calls_per_run", 1, 1000),
        ("max_calls_per_day", 1, 10000),
        ("max_text_chars", 200, 100000),
        ("lookback_hours", 1, 24 * 30),
    ):
        value = judge[key]
        if not isinstance(value, int) or isinstance(value, bool):
            raise ConfigError("[judge].%s 必须是整数" % key)
        if not low <= value <= high:
            raise ConfigError("[judge].%s 必须在 %d..%d 之间" % (key, low, high))
    if judge["max_calls_per_day"] < judge["max_calls_per_run"]:
        raise ConfigError(
            "[judge].max_calls_per_day(%d) 不应小于 max_calls_per_run(%d)"
            % (judge["max_calls_per_day"], judge["max_calls_per_run"])
        )

    # --- accounts ---
    raw_accounts = data.get("accounts", [])
    if not isinstance(raw_accounts, list):
        raise ConfigError("[[accounts]] 必须是账号表数组")
    if not raw_accounts:
        raise ConfigError("配置中没有任何 [[accounts]]，无事可做")

    accounts: List[Account] = []
    seen: Dict[str, int] = {}
    for index, item in enumerate(raw_accounts, start=1):
        if not isinstance(item, dict):
            raise ConfigError("第 %d 个 [[accounts]] 不是表" % index)
        unknown = set(item) - {"handle", "enabled"}
        if unknown:
            raise ConfigError(
                "第 %d 个 [[accounts]] 存在无法识别的键：%s" % (index, ", ".join(sorted(unknown)))
            )
        if "handle" not in item:
            raise ConfigError("第 %d 个 [[accounts]] 缺少 handle" % index)
        handle = normalize_handle(item["handle"])
        if not HANDLE_RE.match(handle):
            raise ConfigError(
                "第 %d 个账号 %r 规范化后为 %r，不符合 X 用户名格式（1-15 位字母数字下划线）"
                % (index, item["handle"], handle)
            )
        if handle in seen:
            raise ConfigError(
                "账号 %r 重复出现（第 %d 与第 %d 项）；同一账号只应配置一次"
                % (handle, seen[handle], index)
            )
        seen[handle] = index
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigError("账号 %r 的 enabled 必须是布尔值" % handle)
        accounts.append(Account(handle, enabled, compute_scope_hash(handle, source)))

    if not any(a.enabled for a in accounts):
        raise ConfigError("所有账号都是 enabled = false，没有可采集的对象")

    return Config(path, app, source, collection, export, retention, accounts, judge)


def _require_bool(section: Dict[str, Any], key: str, name: str) -> None:
    if not isinstance(section[key], bool):
        raise ConfigError("[%s].%s 必须是 true 或 false" % (name, key))


def describe_paths(config: Config) -> List[Tuple[str, str]]:
    return [
        ("database", config.database_path),
        ("output", config.output_dir),
        ("raw", config.raw_dir),
        ("logs", config.log_dir),
    ]
