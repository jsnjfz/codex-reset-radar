"""原始响应与日志的限期清理（方案 §12）。

只清理本程序自己产生的文件：`raw_dir` 下的 `.json` / `.txt`，`log_dir` 下的
`x-watch-*.log`。数据库和 Markdown 永不自动删除。
"""

from __future__ import annotations

import os
import time
from typing import List, Tuple


def _sweep(directory: str, days: int, suffixes: Tuple[str, ...], prefix: str = "") -> int:
    if days <= 0 or not os.path.isdir(directory):
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for name in os.listdir(directory):
        if prefix and not name.startswith(prefix):
            continue
        if not name.endswith(suffixes):
            continue
        path = os.path.join(directory, name)
        try:
            if not os.path.isfile(path) or os.path.getmtime(path) >= cutoff:
                continue
            os.unlink(path)
            removed += 1
        except OSError:
            continue  # 清理失败不影响采集结果
    return removed


def cleanup(raw_dir: str, raw_days: int, log_dir: str, log_days: int) -> List[str]:
    notes: List[str] = []
    raw_removed = _sweep(raw_dir, raw_days, (".json", ".txt"))
    if raw_removed:
        notes.append("清理原始响应 %d 个（保留 %d 天）" % (raw_removed, raw_days))
    log_removed = _sweep(log_dir, log_days, (".log",), prefix="x-watch-")
    if log_removed:
        notes.append("清理日志 %d 个（保留 %d 天）" % (log_removed, log_days))
    return notes
