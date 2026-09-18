#!/bin/sh
# macOS 定时任务（launchd）注册 / 检查 / 删除。
#
# 用法：
#   ./scripts/launchd.sh show       显示将要写入的任务内容（不做任何改动）
#   ./scripts/launchd.sh install    写入并加载任务
#   ./scripts/launchd.sh status     查看任务是否已加载、最近退出码
#   ./scripts/launchd.sh uninstall  卸载并删除任务
#
# 注册前一定先看 show 的输出，确认任务名、命令、运行身份和间隔（方案 §11.2）。
#
# 已知限制（不是本脚本能解决的）：
# - 电脑休眠、关机或断网期间不会采集。launchd 的 StartInterval 任务在唤醒后
#   只补跑一次，不会把错过的每一次都补上。
# - 因此"每小时一次"在笔记本上实际是"醒着的时候大约每小时一次"。

set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
PROJECT_DIR=$(dirname -- "$SCRIPT_DIR")
CONFIG="${X_WATCH_CONFIG:-$PROJECT_DIR/config.toml}"

LABEL="local.x-watch.runonce"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
RUNNER="$PROJECT_DIR/scripts/run_once.sh"

if [ ! -f "$CONFIG" ]; then
    echo "配置文件不存在：$CONFIG" >&2
    exit 2
fi

# 间隔从 config.toml 读取，避免"改了配置却没改系统任务"（方案 §8）
INTERVAL_MINUTES=$(
    PYTHONPATH="$PROJECT_DIR/src" python3 - "$CONFIG" <<'PY'
import sys
from x_watch.toml_compat import load_toml
data = load_toml(sys.argv[1])
print(int(data.get("collection", {}).get("interval_minutes", 60)))
PY
) || exit 2

INTERVAL_SECONDS=$((INTERVAL_MINUTES * 60))
LOG_DIR="$PROJECT_DIR/logs"
STDOUT_LOG="$LOG_DIR/launchd.out.log"
STDERR_LOG="$LOG_DIR/launchd.err.log"

render_plist() {
    cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>$RUNNER</string>
    </array>

    <!-- 显式指定工作目录，不依赖调度器默认值 -->
    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>X_WATCH_CONFIG</key>
        <string>$CONFIG</string>
    </dict>

    <!-- 间隔来自 config.toml 的 interval_minutes = $INTERVAL_MINUTES -->
    <key>StartInterval</key>
    <integer>$INTERVAL_SECONDS</integer>

    <!-- 加载/开机登录后先跑一次，这样重启不会白等一个间隔 -->
    <key>RunAtLoad</key>
    <true/>

    <key>StandardOutPath</key>
    <string>$STDOUT_LOG</string>
    <key>StandardErrorPath</key>
    <string>$STDERR_LOG</string>

    <!-- 后台低优先级，避免和前台工作抢资源 -->
    <key>ProcessType</key>
    <string>Background</string>
    <key>LowPriorityIO</key>
    <true/>
</dict>
</plist>
EOF
}

describe() {
    echo "任务名（Label）   : $LABEL"
    echo "plist 路径        : $PLIST"
    echo "执行命令          : /bin/sh $RUNNER"
    echo "工作目录          : $PROJECT_DIR"
    echo "配置文件          : $CONFIG"
    echo "运行身份          : $(id -un)（用户级 LaunchAgent，登录后才运行）"
    echo "触发间隔          : 每 $INTERVAL_MINUTES 分钟（$INTERVAL_SECONDS 秒）"
    echo "标准输出日志      : $STDOUT_LOG"
    echo "标准错误日志      : $STDERR_LOG"
    echo
    echo "注意：launchd 不会为同一个任务并发启两个实例；x-watch 自身还有单实例文件锁兜底。"
    echo "注意：休眠或关机期间不会采集，唤醒后只补跑一次。"
}

case "${1:-show}" in
    show)
        describe
        echo
        echo "===== 将写入的 plist 内容 ====="
        render_plist
        echo "===== 以上内容尚未写入。执行 install 才会真正注册。 ====="
        ;;

    install)
        describe
        echo
        printf "确认注册以上任务？输入 yes 继续：" >&2
        read -r answer
        if [ "$answer" != "yes" ]; then
            echo "已取消，未做任何改动。" >&2
            exit 1
        fi

        if [ ! -x "$RUNNER" ]; then
            chmod +x "$RUNNER" || exit 2
        fi
        mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents" || exit 2

        # 先做一次运行前自检，避免注册一个必然失败的任务
        echo "注册前自检（doctor --no-network）..."
        if ! PYTHONPATH="$PROJECT_DIR/src" python3 -m x_watch --config "$CONFIG" doctor --no-network; then
            echo "doctor 未通过，已放弃注册。先修好上面的问题。" >&2
            exit 2
        fi

        render_plist > "$PLIST" || exit 2
        launchctl unload "$PLIST" 2>/dev/null
        launchctl load "$PLIST" || exit 2
        echo
        echo "已注册。用 ./scripts/launchd.sh status 查看状态。"
        echo "提示：定时任务的环境变量（含代理）可能和你的终端不同。"
        echo "      请在任务真正跑过一轮后，用 status 命令和 logs/ 核对结果。"
        ;;

    status)
        echo "任务名：$LABEL"
        if [ ! -f "$PLIST" ]; then
            echo "未注册（找不到 ${PLIST}）"
            exit 0
        fi
        echo "plist：已存在"
        # 输出形如 "PID  上次退出码  Label"
        if launchctl list | grep -q "$LABEL"; then
            echo "launchctl 状态：$(launchctl list | grep "$LABEL")"
            echo "（第二列是上次退出码：0 正常 / 1 partial 或导出失败 / 2 配置错误）"
        else
            echo "launchctl 状态：未加载"
        fi
        echo
        echo "最近日志（${STDOUT_LOG}）："
        [ -f "$STDOUT_LOG" ] && tail -n 15 "$STDOUT_LOG" || echo "（还没有日志）"
        ;;

    uninstall)
        if [ -f "$PLIST" ]; then
            launchctl unload "$PLIST" 2>/dev/null
            rm -f "$PLIST"
            echo "已卸载并删除 $PLIST"
        else
            echo "未注册，无需卸载"
        fi
        echo "数据库、Markdown 输出和日志都保留在 ${PROJECT_DIR}，本脚本不会删除它们。"
        ;;

    *)
        echo "用法：$0 {show|install|status|uninstall}" >&2
        exit 2
        ;;
esac
