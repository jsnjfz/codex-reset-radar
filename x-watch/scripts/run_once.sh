#!/bin/sh
# x-watch 单次采集入口（macOS / Linux）。定时任务只调用这个脚本。
#
# 要点（方案 §11.2）：
# - 用绝对路径的解释器，不依赖"某个终端里激活过 venv"或 PATH。
# - 显式切到项目目录，不依赖调度器给的工作目录。
# - 原样保留程序退出码：0 正常 / 1 partial-failed-导出失败 / 2 配置错误。

set -u

# 脚本所在目录的上一级 = 项目根目录
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
PROJECT_DIR=$(dirname -- "$SCRIPT_DIR")
cd "$PROJECT_DIR" || exit 2

CONFIG="${X_WATCH_CONFIG:-$PROJECT_DIR/config.toml}"

# 优先用项目自带的虚拟环境；没有就用系统 python3。
# 本项目运行时零第三方依赖，所以没有 venv 也能跑。
if [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
    PYTHON="$PROJECT_DIR/.venv/bin/python"
elif [ -n "${X_WATCH_PYTHON:-}" ] && [ -x "${X_WATCH_PYTHON}" ]; then
    PYTHON="$X_WATCH_PYTHON"
else
    PYTHON=$(command -v python3) || {
        echo "找不到 python3；请安装 Python 3.9+ 或设置 X_WATCH_PYTHON" >&2
        exit 2
    }
fi

if [ ! -f "$CONFIG" ]; then
    echo "配置文件不存在：${CONFIG}（可从 config.example.toml 复制）" >&2
    exit 2
fi

PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON" -m x_watch --config "$CONFIG" run-once
STATUS=$?

# 退出码 0 只代表本轮按策略完成，不代表没有遗漏。覆盖情况用 status 命令看。
exit $STATUS
