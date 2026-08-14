#!/bin/bash
# DSH Controller 安装脚本
# 用法: ./install.sh [--dry-run] [--yes] [--no-service] [--no-login-item] [--no-bootstrap]
#
# 做什么（边界清晰，绝不做列表之外的事）：
#   1. 在本仓库目录创建 .venv 并 pip 安装 pyobjc
#   2. 在本仓库目录组装 "DSH Controller.app"
#   3. 探测你的 DeepSeek Harness 安装方式，生成
#      ~/Library/LaunchAgents/com.deepseek.harness.web.plist（DSH 后台服务）
#   4. 生成 ~/Library/LaunchAgents/com.deepseek.harness.controller.plist（开机自启）
# 绝不：修改 shell 配置、使用 sudo、全局 pip 安装、动仓库目录以外的文件（上述两个 plist 除外）

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="DSH Controller.app"
APP_DIR="$REPO_DIR/$APP_NAME"
VENV_DIR="$REPO_DIR/.venv"
SRC_DIR="$REPO_DIR/src"

SERVICE_LABEL="${DSHC_SERVICE_LABEL:-com.deepseek.harness.web}"
CONTROLLER_LABEL="${DSHC_CONTROLLER_LABEL:-com.deepseek.harness.controller}"
LA_DIR="${DSHC_LA_DIR:-$HOME/Library/LaunchAgents}"
SERVICE_PLIST="$LA_DIR/$SERVICE_LABEL.plist"
CONTROLLER_PLIST="$LA_DIR/$CONTROLLER_LABEL.plist"

DRY_RUN=0
ASSUME_YES=0
WITH_SERVICE=1
WITH_LOGIN_ITEM=1
WITH_BOOTSTRAP=1

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --yes|-y) ASSUME_YES=1 ;;
        --no-service) WITH_SERVICE=0 ;;
        --no-login-item) WITH_LOGIN_ITEM=0 ;;
        --no-bootstrap) WITH_BOOTSTRAP=0 ;;
        --help|-h)
            echo "用法: $0 [--dry-run] [--yes] [--no-service] [--no-login-item] [--no-bootstrap]"
            exit 0 ;;
        *) echo "未知参数: $arg（--help 查看用法）" >&2; exit 2 ;;
    esac
done

info()  { echo "  $*"; }
ok()    { echo "✅ $*"; }
warn()  { echo "⚠️  $*"; }
fail()  { echo "❌ $*" >&2; exit 1; }
run()   { if [ "$DRY_RUN" = 1 ]; then echo "  [dry-run] $*"; else eval "$@"; fi; }

confirm() {
    [ "$ASSUME_YES" = 1 ] && return 0
    printf "%s [y/N] " "$1"
    read -r ans
    case "$ans" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

echo "==> DSH Controller 安装程序 $([ "$DRY_RUN" = 1 ] && echo '(dry-run 模式，只打印不执行)')"

# ── 1. 前置检查 ─────────────────────────────────────────────
echo "==> 检查前置环境"
[ "$(uname -s)" = "Darwin" ] || fail "仅支持 macOS"

PYTHON3=""
for c in /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3 || true)"; do
    [ -n "$c" ] && [ -x "$c" ] || continue
    ver=$("$c" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "0")
    major=${ver%%.*}; minor=${ver##*.}
    if [ "${major:-0}" -ge 3 ] && [ "${minor:-0}" -ge 9 ]; then
        PYTHON3="$c"; break
    fi
done
[ -n "$PYTHON3" ] || fail "未找到 Python 3.9+。请安装 Xcode 命令行工具（xcode-select --install）或 Homebrew Python"
ok "Python: $PYTHON3 ($("$PYTHON3" --version 2>&1))"

# zstd：用量统计需要（解压 ~/.dsh/sessions/*.zstd）
ZSTD_BIN="$(command -v zstd || true)"
[ -z "$ZSTD_BIN" ] && [ -x /opt/homebrew/bin/zstd ] && ZSTD_BIN=/opt/homebrew/bin/zstd
[ -z "$ZSTD_BIN" ] && [ -x /usr/local/bin/zstd ] && ZSTD_BIN=/usr/local/bin/zstd
if [ -z "$ZSTD_BIN" ]; then
    if command -v brew >/dev/null 2>&1; then
        warn "未找到 zstd（用量统计功能需要）"
        if confirm "是否通过 Homebrew 安装 zstd（brew install zstd）？"; then
            run "brew install zstd"
            ZSTD_BIN="$(command -v zstd || echo /opt/homebrew/bin/zstd)"
        else
            warn "跳过：用量统计页将没有数据（服务控制不受影响）"
        fi
    else
        warn "未找到 zstd 且未安装 Homebrew：用量统计页将没有数据（可稍后用 brew install zstd 补齐）"
    fi
else
    ok "zstd: $ZSTD_BIN"
fi

# ── 2. 创建 venv 并安装 pyobjc ─────────────────────────────
echo "==> 准备 Python 虚拟环境（.venv，仅在本仓库目录内）"
if [ ! -x "$VENV_DIR/bin/python3" ]; then
    run "\"$PYTHON3\" -m venv \"$VENV_DIR\""
fi
# PyPI 文件服务器（files.pythonhosted.org）在部分网络下不可达，自动回退镜像
PYPI_MIRROR=""
if [ "$DRY_RUN" = 0 ] && ! curl -sI --max-time 8 https://files.pythonhosted.org -o /dev/null 2>&1; then
    warn "PyPI 文件服务器直连失败，改用清华镜像安装依赖"
    PYPI_MIRROR="--index-url https://pypi.tuna.tsinghua.edu.cn/simple"
fi
run "\"$VENV_DIR/bin/pip\" install --quiet $PYPI_MIRROR --upgrade pip" \
    || warn "pip 自升级失败（不影响继续）"
run "\"$VENV_DIR/bin/pip\" install --quiet $PYPI_MIRROR pyobjc"
if [ "$DRY_RUN" = 0 ]; then
    "$VENV_DIR/bin/python3" -c "import objc, AppKit" || fail "pyobjc 安装失败"
    ok "pyobjc 安装完成"
else
    info "[dry-run] 跳过 pyobjc 导入验证"
fi

# ── 3. 组装 .app ────────────────────────────────────────────
echo "==> 组装 $APP_NAME"
run "mkdir -p \"$APP_DIR/Contents/MacOS\" \"$APP_DIR/Contents/Resources\""
run "cp \"$SRC_DIR/Info.plist\" \"$APP_DIR/Contents/\""
run "cp \"$SRC_DIR/AppIcon.icns\" \"$APP_DIR/Contents/Resources/\""
run "cp \"$SRC_DIR/dsh_controller.py\" \"$SRC_DIR/usage_stats.py\" \"$SRC_DIR/pricing.json\" \"$APP_DIR/Contents/Resources/\""

# launcher 指向本仓库的 venv（移动仓库目录会导致 App 无法启动）
LAUNCHER="$APP_DIR/Contents/MacOS/launcher"
if [ "$DRY_RUN" = 1 ]; then
    info "[dry-run] 生成 launcher（python = $VENV_DIR/bin/python3）"
else
    cat > "$LAUNCHER" <<EOF
#!/bin/bash
# 由 install.sh 生成 —— 请勿手动移动本仓库目录（venv 路径写死于此）
DIR="\$(cd "\$(dirname "\$0")" && pwd)"
exec "$VENV_DIR/bin/python3" "\$DIR/../Resources/dsh_controller.py"
EOF
    chmod +x "$LAUNCHER"
    # ad-hoc 签名：本地构建的 App 不触发 Gatekeeper 拦截
    codesign --force --deep --sign - "$APP_DIR" >/dev/null 2>&1 || warn "ad-hoc 签名失败（不影响使用）"
fi
ok "App 组装完成: $APP_DIR"

# ── 4. 探测 DeepSeek Harness 安装方式 ──────────────────────
DSH_ARGS=""   # plist 的 ProgramArguments（XML 片段）
NODE_DIR=""   # node 所在目录（写入 plist 的 PATH）

detect_node_dir() {
    local n
    n="$(command -v node || true)"
    [ -n "$n" ] && { dirname "$n"; return 0; }
    for d in /opt/homebrew/bin /usr/local/bin "$HOME/.nvm/versions/node"/*/bin; do
        [ -x "$d/node" ] && { echo "$d"; return 0; }
    done
    return 1
}

make_dsh_args() { # $1=dsh 可执行文件绝对路径
    echo "        <string>$1</string>
        <string>web</string>"
}

detect_dsh() {
    # 分支 2：PATH 中的 dsh
    local d
    d="$(command -v dsh || true)"
    [ -n "$d" ] && { make_dsh_args "$d"; return 0; }
    # 分支 3：npm 全局
    if command -v npm >/dev/null 2>&1; then
        local np="$(npm prefix -g 2>/dev/null || true)"
        [ -n "$np" ] && [ -x "$np/bin/dsh" ] && { make_dsh_args "$np/bin/dsh"; return 0; }
    fi
    # 分支 4：pnpm 全局
    if command -v pnpm >/dev/null 2>&1; then
        local pb="$(pnpm bin -g 2>/dev/null || true)"
        [ -n "$pb" ] && [ -x "$pb/dsh" ] && { make_dsh_args "$pb/dsh"; return 0; }
    fi
    # 分支 5：常见固定路径 + nvm
    for d in /opt/homebrew/bin/dsh /usr/local/bin/dsh "$HOME/.nvm/versions/node"/*/bin/dsh; do
        [ -x "$d" ] && { make_dsh_args "$d"; return 0; }
    done
    # 分支 6：源码版（显式指定 DSH_SOURCE_DIR）
    if [ -n "${DSH_SOURCE_DIR:-}" ] && [ -f "$DSH_SOURCE_DIR/apps/cli/lib/bin.js" ]; then
        local node_bin="$(command -v node || true)"
        [ -n "$node_bin" ] && {
            echo "        <string>$node_bin</string>
        <string>$DSH_SOURCE_DIR/apps/cli/lib/bin.js</string>
        <string>web</string>"
            return 0
        }
    fi
    return 1
}

if [ "$WITH_SERVICE" = 1 ]; then
    echo "==> 配置 DSH 后台服务（launchd）"
    # 分支 1：服务已存在则跳过（幂等）
    if launchctl print "gui/$(id -u)/$SERVICE_LABEL" >/dev/null 2>&1; then
        ok "launchd 服务 $SERVICE_LABEL 已存在，跳过生成"
    else
        if DSH_ARGS="$(detect_dsh)"; then
            info "探测到 DSH: $(echo "$DSH_ARGS" | head -1 | sed 's/.*<string>//;s,</string>,,')"
        else
            warn "未在本机找到 DeepSeek Harness"
            if command -v npm >/dev/null 2>&1 && confirm "是否现在安装官方版本（npm install -g @deepseek-ai/dsh）？"; then
                run "npm install -g @deepseek-ai/dsh"
                DSH_ARGS="$(detect_dsh)" || fail "安装后仍无法定位 dsh 命令，请手动安装后重试"
            else
                fail "请先安装 DeepSeek Harness（https://github.com/deepseek-ai/deepseek-harness），
     或以源码方式运行时设置 DSH_SOURCE_DIR 环境变量后重试。
     如暂时不需要服务控制按钮，可加 --no-service 跳过本步骤。"
            fi
        fi
        NODE_DIR="$(detect_node_dir || true)"
        [ -n "$NODE_DIR" ] || fail "找不到 node 可执行文件"
        if [ "$DRY_RUN" = 1 ]; then
            info "[dry-run] 生成 $SERVICE_PLIST（PATH 含 $NODE_DIR）"
        else
            mkdir -p "$LA_DIR"
            cat > "$SERVICE_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$SERVICE_LABEL</string>
    <key>CreatedBy</key>
    <string>dsh-controller-installer</string>
    <key>ProgramArguments</key>
    <array>
$DSH_ARGS
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$NODE_DIR:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/dsh-web.out</string>
    <key>StandardErrorPath</key>
    <string>/tmp/dsh-web.err</string>
</dict>
</plist>
EOF
            ok "已生成 $SERVICE_PLIST"
            if [ "$WITH_BOOTSTRAP" = 1 ]; then
                launchctl bootstrap "gui/$(id -u)" "$SERVICE_PLIST" 2>/dev/null \
                    || launchctl kickstart -k "gui/$(id -u)/$SERVICE_LABEL" 2>/dev/null || true
                ok "服务已注册（在 App 中点击「启动服务」开始运行）"
            fi
        fi
    fi
fi

# ── 5. Controller 开机自启 ──────────────────────────────────
if [ "$WITH_LOGIN_ITEM" = 1 ]; then
    echo "==> 配置开机自启（可选）"
    if [ "$DRY_RUN" = 1 ]; then
        info "[dry-run] 生成 $CONTROLLER_PLIST（RunAtLoad=true）"
    elif confirm "是否让 DSH Controller 开机自动启动？"; then
        mkdir -p "$LA_DIR"
        cat > "$CONTROLLER_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$CONTROLLER_LABEL</string>
    <key>CreatedBy</key>
    <string>dsh-controller-installer</string>
    <key>ProgramArguments</key>
    <array>
        <string>$APP_DIR/Contents/MacOS/launcher</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
EOF
        if [ "$WITH_BOOTSTRAP" = 1 ]; then
            launchctl bootout "gui/$(id -u)/$CONTROLLER_LABEL" 2>/dev/null || true
            launchctl bootstrap "gui/$(id -u)" "$CONTROLLER_PLIST" || true
        fi
        ok "开机自启已配置"
    else
        info "跳过开机自启（双击 $APP_NAME 即可手动启动）"
    fi
fi

# ── 完成 ────────────────────────────────────────────────────
echo ""
echo "🎉 安装完成！"
info "1. 双击打开: $APP_DIR"
info "2. 菜单栏出现小鱼图标，点击即可控制服务 / 查看用量"
info "3. 卸载：运行本仓库的 ./uninstall.sh"
[ "$DRY_RUN" = 1 ] && info "（以上为 dry-run 预览，未实际写入任何内容）"
