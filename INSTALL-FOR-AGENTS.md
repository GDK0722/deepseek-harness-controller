# INSTALL-FOR-AGENTS — DSH Controller

本文件写给 AI Agent（Claude Code / Kimi / Codex 等）阅读：请在用户机器上自动完成 DSH Controller 的安装。**全程遵守下方边界。**

## 硬性边界（绝不越界）

- 只允许写入：本仓库目录内部、`~/Library/LaunchAgents/` 下 `com.deepseek.harness.web.plist` 与 `com.deepseek.harness.controller.plist` 两个文件
- **禁止**：使用 sudo、修改 shell 配置文件（.zshrc 等）、全局 pip 安装（`pip install` 必须发生在仓库内 .venv）、删除或改动 `~/.dsh/` 里的任何内容、移动仓库目录
- 读取 `~/.dsh/` 内容仅用于验证，不做修改

## 安装步骤

1. **前置检查**（缺则先补，补齐前不要继续）：
   - macOS 13+：`sw_vers`
   - Python 3.9+：`/usr/bin/python3 --version` 或 Homebrew Python
   - DeepSeek Harness：`command -v dsh`；不存在则用 `npm install -g @deepseek-ai/dsh` 安装（需要 Node.js 18+），官方文档见 https://github.com/deepseek-ai/deepseek-harness
2. **克隆并安装**：
   ```bash
   git clone <本仓库 URL>
   cd dsh-controller
   ./install.sh --yes
   ```
   - 若用户的 DSH 是源码版（非 npm 安装），先 `export DSH_SOURCE_DIR=<源码仓库路径>` 再运行
   - 若用户用非默认端口运行 DSH（`dsh web --port N`），创建 `~/.dsh/controller.json` 写入 `{"port": N}`
   - zstd 缺失时脚本会提示；有 Homebrew 则允许 `brew install zstd`
3. **启动**：`open "DSH Controller.app"`

## 安装后验证（全部通过才算成功）

```bash
launchctl print gui/$(id -u)/com.deepseek.harness.web        # 服务已注册
ps aux | grep -c "[d]sh_controller"                          # Controller 在运行（≥1）
```

然后在 Controller 面板中点击「启动服务」，数秒后状态应为绿色"运行正常"，`curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:3080/` 返回 200。

## 失败处理

- install.sh 任何一步报错：读报错信息，按提示补齐前置后重跑（脚本是幂等的）
- 无法解决：停止操作，向用户如实报告失败步骤与报错，不要反复重试

## 卸载

```bash
./uninstall.sh
```

只移除本安装器创建的文件，不动 DSH 本体与 `~/.dsh` 数据。
