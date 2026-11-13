# Coding Dev Wiki

导入 Git 仓库或项目 ZIP，使用 **OpenCode 完整代码智能体运行时** 自动分析项目，生成可追溯的交互式源码阅读站。

## 启动

需要 Python 3.11+、Git 和固定版本 OpenCode（安装命令如下）。前端无需 Node 构建，源码由同一个 Web 服务提供。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -c requirements.lock -e '.[dev]'
python -m app.runtime.install
export WIKI_LLM_BASE_URL=https://api.openai.com/v1
export WIKI_LLM_API_KEY=your-key
export WIKI_LLM_MODEL=your-tool-capable-model
uvicorn app.api:app --host 127.0.0.1 --port 8080
```

访问 <http://127.0.0.1:8080>。上述普通 / 生产启动通过 shell 或部署平台注入配置；本地 `python -m app.dev` 自动加载项目根目录 `.env`，可复制 `.env.example` 后填写。已导出的环境变量优先，修改配置后重启。

没有模型配置时可明确选择「结构预览」来体验导入、任务进度及源码导航。它不等同于 Agent 分析。Agent 分析失败时显示失败原因，不会静默降级。

## 本地开发模式

```bash
source .venv/bin/activate
python -m app.dev
```

固定监听 `127.0.0.1:8080`（可用 `--port` 修改端口），首页出现“开发者模式 · 本地项目目录”。输入绝对目录后可直接分析，沿用所选 Agent / 结构预览方式，无需 Git 拉取或 ZIP 上传。本地启动器会自动加载项目根目录 `.env` 中的模型配置；已导出的环境变量优先，修改后重启。

默认 `uvicorn app.api:app` 是生产模式，无此入口。本地启动器显式设置 `WIKI_ENV=development` 和 `WIKI_DEV_MODE=1`；生产不要设置这两个开发值。生产 Docker 镜像会直接排除开发路由、启动器和资源。

## 已实现

- 公开 HTTPS Git URL / ZIP 导入，任务队列、实时 SSE 日志与阶段进度。
- OpenCode 负责原生代码探索、工具循环、会话和压缩；应用只负责只读快照、任务管理和结果/引用校验。
- 项目概述、阅读路线、可点击的架构图、业务功能与实现环节。
- 功能链路 / 函数聚焦 / 完整文件视图、目录树、符号大纲、全仓文本搜索、页内查找、行号跳转、快照分享链接、主题切换。
- 基于同一源码快照的多轮问答和引用跳转。
- SQLite 任务历史、失败后手动继续（源码与 Agent 上下文检查点）、完成后重新分析；重启中断的任务标记失败，可手动重试。
- Python AST 和 Tree-sitter 精确提取 JS/TS/TSX/Java 符号；其他已支持文本类型提供文件级阅读。

## 部署

```bash
docker compose up --build -d
```

通过环境变量配置模型，持久卷保存任务及源码。默认监听本机 8080。MVP 按单个可信工作区设计，**同一实例的访问者共享全部项目**。接入互联网前设置 `WIKI_ACCESS_TOKEN`、通过 HTTPS 反向代理访问，并在代理上限制请求体 52 MiB；本版本没有用户级项目隔离。

## 验证与文档

```bash
pytest -q
mkdocs build --strict
mkdocs serve
```

[使用说明](docs/usage.md) · [需求与验收](docs/spec.md) · [技术决策与调研](docs/architecture.md)

端到端浏览器检查：先启动服务，再运行 `python tests/browser_smoke.py`。脚本默认使用本机 Chrome，也可用 `WIKI_BROWSER_EXECUTABLE` 指定 Chromium 路径。

模型适配只需在 `.env` 配置 `WIKI_LLM_CONTEXT_TOKENS`（默认 `128000`）；输入、输出、工具读取预算自动推导。默认先生成有界的快速概览，细节通过项目问答按需检索；失败后保留原生会话与已读证据，无需手动配置输出 token 上限。详见[上下文窗口配置](docs/usage.md#模型上下文窗口)。

任务运行时可实时查看状态并点击“停止分析”；停止后保留检查点，Agent 任务可继续。停止中的请求会等待执行退出后才标记为已停止。

仓库分析采用完整代码智能体，优先理解主要链路，不再强制执行串行目录分区。失败或停止后恢复原生会话，详见[分析运行时](docs/runtime.md)。

任务页的处理日志已接入 OpenCode 原生 SSE 实时动作，支持刷新恢复。模型请求超时会在同一会话自动重试两次，停止按钮可取消重试。
