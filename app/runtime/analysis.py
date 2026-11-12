"""完整代码智能体生成项目知识，应用验证结果与真实读取证据。"""
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from .client import Runtime
from .workspace import materialize, collect_reads
from ..analysis.schemas import Analysis, validate_analysis, all_evidence


@dataclass
class RuntimeUsage:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0


def normalize_paths(result, workspace, files):
    repo = (workspace / "repo").resolve()
    for ref in all_evidence(result):
        if ref.path in files:
            continue
        path = Path(ref.path)
        candidate = None
        if path.is_absolute():
            path = path.resolve()
            if path.is_relative_to(repo):
                candidate = path.relative_to(repo).as_posix()
        elif ref.path.startswith("repo/"):
            candidate = ref.path[5:]
        if candidate in files:
            ref.path = candidate


async def analyze(files, log, *, state=None, checkpoint=None, runtime_factory=Runtime):
    state = state or {}
    temporary = None
    location = getattr(checkpoint, "directory", None)
    if location is None:
        temporary = tempfile.TemporaryDirectory(prefix="wiki-runtime-")
        location = Path(temporary.name)
    directory = Path(location) / "opencode"
    directory.mkdir(parents=True, exist_ok=True)
    workspace = materialize(directory, files)
    current = {"version": 3, "engine": "opencode", "session_id": None,
               "reads": state.get("reads", {}), "seen": state.get("seen", []), "usage": {}}
    if state.get("engine") == "opencode":
        current.update(state)
    elif state:
        current["legacy"] = state
        log("迁移到完整代码智能体：保留旧检查点和阅读记录，不导入旧的分区长对话")
    seen = set(current["seen"])
    usage = RuntimeUsage()
    def persist():
        current["seen"] = sorted(seen)
        if checkpoint:
            checkpoint(current)
    def observe(messages):
        collect_reads(messages, workspace, files, current["reads"], seen, log)
        assistants = [message["info"] for message in messages if message.get("info", {}).get("role") == "assistant"]
        if assistants:
            usage.requests = len(assistants)
            usage.input_tokens = sum(item.get("tokens", {}).get("input", 0) for item in assistants)
            usage.output_tokens = sum(item.get("tokens", {}).get("output", 0) for item in assistants)
            usage.reasoning_tokens = sum(item.get("tokens", {}).get("reasoning", 0) for item in assistants)
            current["usage"] = vars(usage).copy()
        persist()
    schema = Analysis.model_json_schema()
    prompt = ("分析 repo/ 里的项目，先解释项目解决什么问题、入口和最主要的业务链路，再解释架构。"
              "只选择 4-8 个主要功能；小项目按实际数量。最多 6 个分组、12 个架构节点、16 条关系；"
              "每项功能 2-4 条关键源码证据。不要按目录拆成 12 轮，不要审计所有测试。"
              "先交付完整但简洁的结果，源码细节在网页中可以按引用继续阅读。"
              "先读 README、依赖和入口，必要时检索依赖。引用相对 repo/，例如 app/api.py。"
              "最终只输出一个符合以下 JSON Schema 的 JSON 对象，不写 HTML，不写 Markdown 说明："
              + json.dumps(schema, ensure_ascii=False))
    if current.get("session_id"):
        prompt = "继续当前项目的分析，沿用已有探索成果，不重复全仓扫描。" + prompt
    log("OpenCode 完整运行时开始分析：主链路优先，原生会话与自动压缩，不再执行串行目录分区")
    try:
        async with runtime_factory(directory, workspace, log) as runtime:
            current["session_id"] = await runtime.session(current.get("session_id"))
            persist()
            for attempt in range(2):
                text = (await runtime.prompt(prompt, observe)).strip()
                if text.startswith("```") and text.endswith("```"):
                    text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
                try:
                    result = Analysis.model_validate_json(text)
                    normalize_paths(result, workspace, files)
                    validate_analysis(result, files, current["reads"])
                    if len(result.featureGroups) > 6 or len(result.architecture) > 12 or len(result.edges) > 16:
                        raise ValueError("请保留主要架构：最多 6 个功能分组、12 个节点、16 条关系")
                    if sum(len(group.features) for group in result.featureGroups) > 12:
                        raise ValueError("请只保留最多 12 个主要功能，后续细节通过问答展开")
                    unread = len(set(files) - set(current["reads"]))
                    result.limitations.append(f"静态分析；{len(files)} 个索引文件中，{unread} 个未通过工具读取，不代表穷尽全部功能。")
                    current["draft"] = result.model_dump()
                    persist()
                    return result, usage
                except ValueError as exc:
                    if attempt:
                        raise ValueError("智能体结果校验失败，会话已保留：" + str(exc)[:250]) from exc
                    log("结果校验未通过，将在原生会话中修正一次，不重新分析整个项目")
                    prompt = "仅修正最终结果，必要时读取缺失证据，然后输出同一 Schema 的完整 JSON。校验反馈：" + str(exc)[:700]
    finally:
        persist()
        if temporary:
            temporary.cleanup()
