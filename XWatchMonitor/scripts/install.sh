#!/bin/bash
# 把构建好的 .app 安装到 /Applications 并（可选）开启开机启动。
#
# 为什么必须装到 /Applications：build-app.sh 每次都会 rm -rf 掉 dist/，
# 而登录项记录的是 .app 的绝对路径 —— 指向 dist/ 的登录项会在下次构建后失效。
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="X 重置监控.app"
SRC="$PROJECT_DIR/dist/$APP_NAME"
DEST="/Applications/$APP_NAME"

if [ ! -d "$SRC" ]; then
    echo "找不到 $SRC —— 先跑 ./scripts/build-app.sh" >&2
    exit 1
fi

# 正在运行的话先退出，否则替换会失败
if pgrep -f "$APP_NAME/Contents/MacOS/XWatchMonitor" >/dev/null 2>&1; then
    echo "检测到正在运行，先退出旧实例…"
    osascript -e 'quit app "X 重置监控"' 2>/dev/null || true
    sleep 2
fi

rm -rf "$DEST"
cp -R "$SRC" "$DEST"
# 换位置后重新签名，避免签名里的路径信息不一致
codesign --force --deep --sign - "$DEST"
echo "已安装：$DEST"

if [ "${1:-}" = "--login" ]; then
    open -a "$DEST" --args --enable-login-item
    echo "已请求开启开机启动；用 ./scripts/install.sh --status 查看结果"
else
    open -a "$DEST"
fi

STATUS_FILE="$HOME/Library/Application Support/XWatchMonitor/login-status.txt"
sleep 3
echo ""
echo "开机启动状态："
if [ -f "$STATUS_FILE" ]; then
    sed 's/^/  /' "$STATUS_FILE"
else
    echo "  （状态文件还没生成，稍等几秒再看：$STATUS_FILE）"
fi
