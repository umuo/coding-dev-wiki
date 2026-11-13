"""首次探索有界、整理禁止检索、证据与恢复保持有效。"""
import asyncio
import json
import pytest
from app.runtime import overview
from app.runtime.analysis import analyze
from app.runtime.settings import runtime_config
from .test_agent import FILES, OUTPUT


@pytest.mark.parametrize("window", [8192, 32768, 128000, 1000000])
def test_compaction_threshold_is_positive(window, monkeypatch):
    monkeypatch.setenv("WIKI_LLM_CONTEXT_TOKENS", str(window))
    config = runtime_config()
    limits = config["provider"]["wiki"]["models"]["project"]["limit"]
    reserved = config["compaction"]["reserved"]
    assert limits["input"] - reserved > 0
    assert limits["input"] + limits["output"] < window


def tool(identity, path="main.py"):
    return {"id": identity, "type": "tool", "tool": "read", "state": {"status": "completed",
        "input": {"filePath": "repo/" + path}, "output": "<content>\n1: def run():\n2:     return 42\n</content>"}}


def test_budget_deduplicates_history_and_limits_tools(monkeypatch):
    monkeypatch.setattr(overview, "MAX_TOOLS", 2)
    budget = overview.ExplorationBudget(["old"])
    budget.observe([{"parts": [tool("old"), tool("one")]}])
    budget.observe([{"parts": [tool("one")]}])
    assert len(budget.tools) == 1
    with pytest.raises(overview.ExplorationComplete):
        budget.observe([{"parts": [tool("two")]}])


def test_budget_limits_distinct_files_and_new_requests(monkeypatch):
    monkeypatch.setattr(overview, "MAX_FILES", 2)
    budget = overview.ExplorationBudget()
    budget.previous_requests = {"history"}
    budget.observe([{"info": {"role": "assistant", "id": "history"}, "parts": [tool("a")]}])
    assert not budget.requests
    with pytest.raises(overview.ExplorationComplete):
        budget.observe([{"parts": [tool("b", "other.py")]}])
    monkeypatch.setattr(overview, "MAX_REQUESTS", 1)
    with pytest.raises(overview.ExplorationComplete):
        overview.ExplorationBudget().observe([{"info": {"role": "assistant", "id": "new"}}])


class FakeRuntime:
    instances = []
    mode = "tools"

    def __init__(self, directory, workspace, log):
        self.log = log
        self.allow_tools = True
        self.calls = []
        self.aborted = 0
        self.history = []
        self.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def session(self, previous=None):
        return "ses_test"

    async def messages(self):
        return self.history

    async def abort(self):
        self.aborted += 1

    async def prompt(self, prompt, observe):
        self.calls.append((self.allow_tools, prompt))
        if self.allow_tools:
            if self.mode != "no-evidence":
                self.history = [{"parts": [tool("one")]}]
                observe(self.history)
            await asyncio.Event().wait()
        assert "禁止继续调用工具" in prompt
        return json.dumps(OUTPUT)


@pytest.mark.parametrize("mode", ["tools", "timeout", "no-evidence"])
def test_budget_handoff_preserves_verified_reads(mode, monkeypatch):
    monkeypatch.setattr(FakeRuntime, "mode", mode)
    monkeypatch.setattr(overview, "EXPLORE_SECONDS", .03)
    monkeypatch.setattr(overview, "MAX_TOOLS", 1 if mode == "tools" else 24)
    logs = []
    run = analyze(FILES, logs.append, runtime_factory=FakeRuntime)
    if mode == "no-evidence":
        with pytest.raises(ValueError, match="未取得有效源码证据"):
            asyncio.run(run)
        assert len(FakeRuntime.instances[-1].calls) == 1
    else:
        result, _ = asyncio.run(run)
        assert result.overview == OUTPUT["overview"]
        assert [allowed for allowed, _ in FakeRuntime.instances[-1].calls] == [True, False]
        assert any("预算已到" in item for item in logs)
    assert FakeRuntime.instances[-1].aborted


def test_finalization_timeout_preserves_manual_recovery(monkeypatch):
    import app.runtime.analysis as module
    class SlowFinal(FakeRuntime):
        async def prompt(self, prompt, observe):
            if not self.allow_tools:
                await asyncio.Event().wait()
            return await super().prompt(prompt, observe)
    monkeypatch.setattr(overview, "MAX_TOOLS", 1)
    monkeypatch.setattr(module, "FINALIZE_SECONDS", .02)
    with pytest.raises(TimeoutError, match="概览整理.*可手动继续"):
        asyncio.run(analyze(FILES, lambda message: None, runtime_factory=SlowFinal))


def test_unread_citation_cannot_reopen_exploration(monkeypatch):
    import copy
    class InvalidFinal(FakeRuntime):
        async def prompt(self, prompt, observe):
            if self.allow_tools:
                return await super().prompt(prompt, observe)
            self.calls.append((self.allow_tools, prompt))
            output = copy.deepcopy(OUTPUT)
            output["featureGroups"][0]["features"][0]["steps"][0]["path"] = "unread.py"
            return json.dumps(output)
    monkeypatch.setattr(overview, "MAX_TOOLS", 1)
    with pytest.raises(ValueError, match="智能体结果校验失败"):
        asyncio.run(analyze(FILES, lambda message: None, runtime_factory=InvalidFinal))
    assert [allowed for allowed, _ in InvalidFinal.instances[-1].calls] == [True, False, False]
