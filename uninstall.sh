#!/bin/bash
# DSH Controller 卸载脚本
# 只移除 install.sh 创建的内容：
#   - ~/Library/LaunchAgents/com.deepseek.harness.controller.plist（开机自启）
#   - ~/Library/LaunchAgents/com.deepseek.harness.web.plist（仅当由本安装器创建）
#   - 本仓库内的 .venv 和 "DSH Controller.app"
# 不会：删除 DeepSeek Harness 本体、~/.dsh 数据、修改 shell 配置

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
LA_DIR="$HOME/Library/LaunchAgents"
SERVICE_PLIST="$LA_DIR/com.deepseek.harness.web.plist"
CONTROLLER_PLIST="$LA_DIR/com.deepseek.harness.controller.plist"
SERVICE_LABEL="com.deepseek.harness.web"
CONTROLLER_LABEL="com.deepseek.harness.controller"

echo "==> 卸载 DSH Controller"

# 停止运行中的 Controller
pkill -f "DSH Controller.app" 2>/dev/null && echo "  已停止运行中的 Controller" || true

# Controller 开机自启
if [ -f "$CONTROLLER_PLIST" ]; then
    launchctl bootout "gui/$(id -u)/$CONTROLLER_LABEL" 2>/dev/null || true
    rm -f "$CONTROLLER_PLIST"
    echo "  已移除开机自启: $CONTROLLER_PLIST"
fi

# DSH 服务 plist：仅当它由本安装器创建（含 CreatedBy 标记）才移除
if [ -f "$SERVICE_PLIST" ]; then
    if /usr/libexec/PlistBuddy -c "Print :CreatedBy" "$SERVICE_PLIST" 2>/dev/null | grep -q "dsh-controller-installer"; then
        launchctl bootout "gui/$(id -u)/$SERVICE_LABEL" 2>/dev/null || true
        rm -f "$SERVICE_PLIST"
        echo "  已移除 DSH 后台服务配置: $SERVICE_PLIST"
        echo "  （DeepSeek Harness 本体与 ~/.dsh 数据未动）"
    else
        echo "  保留 $SERVICE_PLIST（非本安装器创建，不碰）"
    fi
fi

# 本仓库内的构建产物
rm -rf "$REPO_DIR/.venv" "$REPO_DIR/DSH Controller.app"
echo "  已删除 .venv 与 DSH Controller.app"

echo ""
echo "✅ 卸载完成。仓库目录本身可手动删除。"
