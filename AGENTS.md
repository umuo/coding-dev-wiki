# 项目协作约定

- 中文用户文档。默认 4 空格缩进，JavaScript 控制语句始终加花括号。
- 按领域组织 `app/importing`、`app/analysis`、任务存储和 API；前端按导航、源码、架构、问答拆模块。代码文件目标不超过 500 行，严禁超过 800 行。
- LLM Agent 必须复用 Pydantic AI 或经过评估的成熟框架，不自行实现 ReAct / tool-calling 循环。
- 分析结果绑定同一源码快照。所有引用必须通过路径、行号和已读取区间校验；分析失败不能静默降级为结构预览。
- 所有仓库内容和模型输出都按不可信文本处理，不执行仓库代码，不用 `innerHTML` 渲染动态内容。
- 运行 `pytest -q`；前端流程变更后运行 `python tests/browser_smoke.py`（需要已启动服务和 Chrome）。文档变更运行 `mkdocs build --strict`，图表需浏览器验证。
- `.data`、`.env`、虚拟环境和测试产物不得提交。模型密钥只在服务端配置。
