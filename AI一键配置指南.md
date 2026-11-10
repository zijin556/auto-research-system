# AI 一键自动配置指南

如果你使用 **Claude Code**、**Codex**、**Cursor**、**Windsurf** 或 **Antigravity**，可以直接把下面的提示词发给你的 AI 助手，让它自动检测你电脑上已有的模型工具并完成全部配置与启动。

---

## 复制以下提示词发给你的 AI 助手

````markdown
请帮我自动配置并启动当前目录下的“自动科研系统（科研文件管理系统）”。请依次执行以下步骤：

1. **检查 Python 环境**：
   - 确认本机已安装 Python 3.11 或 3.12（无需安装任何第三方 pip 包，系统仅依赖 Python 标准库）。

2. **自动检测本机可用的 AI 命令行工具并生成配置**：
   - 运行内置的自动检测与配置脚本：
     `python -X utf8 tools/auto_configure.py`
   - 该脚本会自动检测我电脑上已安装并登录的 CLI 工具：
     - `codex`（OpenAI Codex CLI，ChatGPT Plus/Pro 订阅）
     - `agy`（Google Antigravity CLI，使用 Gemini 模型）
     - `claude`（Claude Code CLI，官方订阅或自定义 API 中转站）
   - 如果我只安装了其中 1 家或 2 家工具，请确认生成的 `00_模型与额度.json` 中已将各个角色（`author`、`reviewer`、`writer`、`drift`、`referee`）的首选模型设为我本机已安装且已登录的工具；如果我只有 1 家模型工具，请确保 `00_模型与额度.json` 顶层包含 `"strict_family": false`（允许同一模型家族兼任作者与审查）。
   - 如果我的 `claude` 或 `codex` 使用了自定义模型名称（可通过查看 `~/.claude/settings.json` 或 `~/.codex/ config.toml` 确认），请同步更新到 `00_模型与额度.json` 的对应角色中。

3. **保护个人研究隐私**：
   - 检查 `00_模型与额度.json` 中的 `"git"` 配置：如果我还没有绑定自己的私有 GitHub 仓库，请保持 `"auto_push": false`（只做本地 Git 自动提交，不向公开模板仓库推送）。

4. **运行自检与启动本地服务**：
   - 运行单元测试验证环境正常：`python -X utf8 -m unittest tests/test_agents.py -q`
   - 启动本地工作台服务（端口 8787）：
     - Windows：`powershell -NoProfile -ExecutionPolicy Bypass -File .\启动系统.ps1 -NoBrowser`
     - macOS / Linux：`python3 -X utf8 nb.py serve --root . --port 8787 --no-browser`
   - 请求 `http://127.0.0.1:8787/api/health` 确认服务启动成功，并告诉我可以打开 `http://127.0.0.1:8787` 开始使用。
````

---

## 各家命令行工具的手动登录方式（供参考）

本系统**不读取、不存储任何 API Key 明文**，而是直接复用你本机命令行工具自带的登录态：

| 工具 | 命令行名称 | 如何检查或登录 |
|---|---|---|
| **OpenAI Codex** | `codex` | 终端运行 `codex login` 使用 ChatGPT 账号登录 |
| **Google Antigravity** | `agy` | 打开 Antigravity IDE 或运行 `agy` 登录 Google 账号（仅调用 Gemini 模型） |
| **Claude Code** | `claude` | 终端运行 `claude` 登录，或在 `~/.claude/settings.json` 中配置中转站 `ANTHROPIC_BASE_URL` 与 `ANTHROPIC_AUTH_TOKEN` |

配置完成后，你随时可以在网页右上角点击 **“设置”**：
- 为每个角色（作者、审查、工程、写作、漂移检查、审稿）切换**首选模型**、**推理强度（low / medium / high / xhigh / max / ultra）**与**备选模型**；
- 点击 **“测试连接”** 验证各家模型是否连通。
