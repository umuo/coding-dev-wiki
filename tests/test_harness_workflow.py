"""真实 harness 与 FunctionModel 的仓库分段、持久化及上下文回归。"""
import asyncio
import copy
import json
import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from app.analysis.agent import analyze
from app.analysis.workflow import partition
from app.analysis.harness import Notebook
from .test_agent import FILES, SOURCE, OUTPUT
from .model_responses import summary


def call(name, args):
    return ModelResponse(parts=[ToolCallPart(name, args)])


def returned(messages, name):
    return [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart) and part.tool_name == name]


def text(messages):
    return str([part.content for message in messages for part in message.parts if isinstance(part, UserPromptPart)])


def test_partition_is_deterministic_bounded_and_covers_all_files_once():
    files = {f"package-{index}/sub/file.py": {} for index in range(100)}
    modules = partition(files)
    paths = [path for module in modules for path in module["paths"]]
    assert sorted(paths) == sorted(files) and len(paths) == len(set(paths))
    assert len(modules) == 12 and modules == partition(dict(reversed(list(files.items()))))


@pytest.mark.parametrize("window", ["8192", "128000"])
def test_modules_have_isolated_history_and_resume_skips_completed_module(monkeypatch, window):
    monkeypatch.setenv("WIKI_LLM_CONTEXT_TOKENS", window)
    files = {f"{name}/{index}.py": {**FILES["main.py"], "path": f"{name}/{index}.py"}
             for name in ("alpha", "beta") for index in range(7)}
    state = {}
    phases = []
    interrupted = False
    def checkpoint(value):
        state.clear()
        state.update(copy.deepcopy(value))
    def respond(messages, info):
        nonlocal interrupted
        prompt = text(messages)
        phase = "synthesis" if "汇总整个项目。" in prompt else "alpha" if "分析当前模块 alpha" in prompt else "beta"
        phases.append(phase)
        if phase == "synthesis":
            # 汇总只接收模块摘要与工作记忆，没有前面模块的原始读取输出。
            assert not returned(messages, "read_file")
            completed = {part.args_as_dict()["module_id"] for message in messages for part in message.parts
                         if isinstance(part, ToolCallPart) and part.tool_name == "read_module"}
            for module in partition(files):
                if module["id"] not in completed:
                    return call("read_module", {"module_id": module["id"]})
            return call(info.output_tools[0].name, summary(OUTPUT))
        reads = returned(messages, "read_file")
        assert all(part.content == "1: def run():\n2:     return 42" for part in reads)
        if not reads:
            assert not returned(messages, "save_feature")
            if phase == "beta":
                assert "alpha 的发现保存在笔记中" in prompt
            return call("read_file", {"path": phase + "/0.py", "start": 1, "end": 2})
        if phase == "beta" and not interrupted:
            interrupted = True
            raise UnexpectedModelBehavior("beta interrupted")
        if phase == "alpha" and not returned(messages, "write_memory"):
            return call("write_memory", {"content": "alpha 的发现保存在笔记中；接下来检查 beta 依赖。"})
        if not returned(messages, "save_feature"):
            feature = copy.deepcopy(OUTPUT["featureGroups"][0]["features"][0])
            feature["title"] = phase
            feature["steps"][0]["path"] = phase + "/0.py"
            return call("save_feature", {"key": phase, "group": "核心", "summary": "功能", "feature": feature})
        if not returned(messages, "save_node"):
            node = copy.deepcopy(OUTPUT["architecture"][0])
            node["evidence"][0]["path"] = phase + "/0.py"
            return call("save_node", {"key": phase, **node})
        return call(info.output_tools[0].name, summary(OUTPUT))
    with pytest.raises(UnexpectedModelBehavior, match="beta interrupted"):
        asyncio.run(analyze(files, lambda value: None, FunctionModel(respond), checkpoint=checkpoint))
    assert [module["status"] for module in state["workflow"]["modules"]] == ["completed", "in_progress"]
    assert state["memory"]["analysis/MEMORY.md"].startswith("alpha 的发现")
    assert len(state["features"]) == 1
    phases.clear()
    logs = []
    result, usage = asyncio.run(analyze(files, logs.append, FunctionModel(respond),
                                       state=json.loads(json.dumps(state)), checkpoint=checkpoint))
    assert "alpha" not in phases and "synthesis" in phases
    assert len(result.featureGroups[0].features) == 2
    assert any("跳过已完成模块：alpha" in message for message in logs)
    assert all(module["status"] == "completed" for module in state["workflow"]["modules"])
    assert usage.requests == len(phases)


def test_memory_survives_small_window_compaction_and_is_scoped_to_task(monkeypatch):
    monkeypatch.setenv("WIKI_LLM_CONTEXT_TOKENS", "8192")
    content = SOURCE + ("# " + "context " * 70 + "\n") * 20
    files = {"main.py": {**FILES["main.py"], "content": content, "lines": len(content.splitlines())}}
    step = 0
    state = {}
    def respond(messages, info):
        nonlocal step
        step += 1
        if step == 1:
            assert "待查入口依赖" not in text(messages)
            return call("write_memory", {"content": "待查入口依赖：run 是主要入口。"})
        assert "待查入口依赖" in text(messages)
        if step <= 11:
            return call("read_file", {"path": "main.py", "start": 1, "end": 20})
        if step == 12:
            return call("save_feature", {"key": "f", "group": "核心", "summary": "功能", "feature": OUTPUT["featureGroups"][0]["features"][0]})
        if step == 13:
            return call("save_node", {"key": "n", **OUTPUT["architecture"][0]})
        return call(info.output_tools[0].name, summary(OUTPUT))
    logs = []
    asyncio.run(analyze(files, logs.append, FunctionModel(respond), checkpoint=lambda value: state.update(copy.deepcopy(value))))
    assert any("官方 harness 压缩" in line for line in logs)
    assert "待查入口依赖" in state["memory"]["analysis/MEMORY.md"]
    step = 0
    asyncio.run(analyze(files, lambda value: None, FunctionModel(respond)))


def test_notebook_is_not_a_source_filesystem_and_bounds_total_storage():
    async def exercise():
        notebook = Notebook()
        with pytest.raises(ValueError):
            await notebook.write("../source.py", "overwrite", expected_version=None)
        await notebook.write("analysis/MEMORY.md", "x" * 65000, expected_version=None)
        from pydantic_ai import ModelRetry
        with pytest.raises(ModelRetry, match="工作记忆已满"):
            await notebook.write("analysis/other.md", "x" * 1000, expected_version=None)
    asyncio.run(exercise())
