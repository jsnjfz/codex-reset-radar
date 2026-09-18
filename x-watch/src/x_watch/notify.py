"""通知。

**安全约束：通知内容里含帖子正文和模型输出，都是不可信文本。**
因此这里绝不构造 shell 字符串：
- 自定义程序用 `subprocess.run([prog, title, body])`，argv 列表传参，`shell=False`。
- macOS 通知走 `osascript -e <脚本>`，脚本里的字符串做 AppleScript 转义，
  且正文里的控制字符全部剥掉。

方案 §10 说「告警体现在日志和每日索引中，不等于已经配置微信、邮件或手机通知」。
这里提供的是本机通知，不需要任何账号或密钥；外部渠道仍需用户自己接。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import List, Tuple

from .logsetup import get_logger

NOTIFIERS = ("none", "log", "macos", "command")

# 只保留可打印字符与常见空白，去掉控制字符 —— 防止终端转义序列之类的注入
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def sanitize_text(value: str, limit: int = 240) -> str:
    """把不可信文本压成单行、无控制字符、限长。"""
    text = _CONTROL_RE.sub(" ", str(value or ""))
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def _applescript_quote(value: str) -> str:
    """AppleScript 字符串字面量转义：反斜杠与双引号。"""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def available(notifier: str) -> Tuple[bool, str]:
    """检查通知方式是否可用。供 doctor 使用，不真的发通知。"""
    if notifier in ("none", "log"):
        return True, "无需外部程序"
    if notifier == "macos":
        if os.uname().sysname != "Darwin":
            return False, "macos 通知只能在 macOS 上使用"
        if shutil.which("osascript") is None:
            return False, "找不到 osascript"
        return True, "osascript 可用"
    if notifier == "command":
        return True, "由 [judge].notify_command 指定的程序决定"
    return False, "未知通知方式 %r" % notifier


def send(notifier: str, command: str, title: str, body: str, timeout: int = 15) -> bool:
    """发送一条通知。返回是否成功；失败只记日志，不抛异常。"""
    log = get_logger()
    safe_title = sanitize_text(title, 120)
    safe_body = sanitize_text(body, 400)

    if notifier == "none":
        return True
    if notifier == "log":
        log.warning("【通知】%s —— %s", safe_title, safe_body)
        return True

    argv: List[str]
    if notifier == "macos":
        script = 'display notification "%s" with title "%s"' % (
            _applescript_quote(safe_body),
            _applescript_quote(safe_title),
        )
        argv = ["osascript", "-e", script]
    elif notifier == "command":
        if not command:
            log.error("notifier=command 但 [judge].notify_command 为空，未发送通知")
            return False
        if not os.path.isfile(command) or not os.access(command, os.X_OK):
            log.error("[judge].notify_command 不是可执行文件：%s", command)
            return False
        # argv 列表传参，绝不拼 shell 字符串
        argv = [command, safe_title, safe_body]
    else:
        log.error("未知通知方式 %r，未发送通知", notifier)
        return False

    try:
        completed = subprocess.run(
            argv,
            shell=False,
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("发送通知失败：%s", exc)
        return False

    if completed.returncode != 0:
        log.error(
            "通知程序返回 %d：%s",
            completed.returncode,
            sanitize_text(completed.stderr.decode("utf-8", "replace"), 200),
        )
        return False
    log.info("已发送通知：%s", safe_title)
    return True
