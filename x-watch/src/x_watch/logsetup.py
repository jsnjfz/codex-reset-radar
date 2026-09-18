"""日志。按天一个文件，同时输出到控制台。

告警只落在日志和每日索引里 —— 这不等于已经配置邮件或手机通知（方案 §10）。
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import sys
from typing import Optional

from .util import ensure_dir

LOGGER_NAME = "x_watch"


def setup_logging(log_dir: Optional[str], verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers = []
    logger.propagate = False

    # 日志走 stderr，stdout 只用于机器可读输出（--json）。
    # 这样调用方（比如菜单栏 app）可以直接 parse stdout，不会被日志污染。
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console)

    if log_dir:
        try:
            ensure_dir(log_dir)
            day = _dt.datetime.now().strftime("%Y-%m-%d")
            path = os.path.join(log_dir, "x-watch-%s.log" % day)
            handler = logging.FileHandler(path, encoding="utf-8")
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
            )
            logger.addHandler(handler)
        except OSError as exc:
            logger.warning("无法写入日志目录 %s：%s（继续运行，仅控制台输出）", log_dir, exc)
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)
