import asyncio
import copy
import json
import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, UserPromptPart, ToolCallPart, ToolReturnPart, ModelMessagesTypeAdapter
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.exceptions import UnexpectedModelBehavior, IncompleteToolCall
from app.config import model_budget
from app.analysis.agent import analyze
from app.analysis.budget import message_tokens
from pydantic_ai_harness.compaction import SlidingWindowCompaction
from app.analysis.checkpoints import recover_messages, error_message
from app.analysis.schemas import Evidence, validate_evidence
from .test_agent import FILES, OUTPUT, SOURCE, REF
from .model_responses import recorded_response, summary


def test_only_context_setting_has_defaults_and_derives_other_limits(monkeypatch):
    monkeypatch.delenv("WIKI_LLM_CONTEXT_TOKENS", raising=False)
    default = model_budget()
    assert default.context_tokens == 128000 and default.max_output_tokens == 8192
    monkeypatch.setenv("WIKI_LLM_CONTEXT_TOKENS", "8192")
    small = model_budget()
    assert small.max_output_tokens == 2048
    assert small.input_tokens + small.max_output_tokens < small.context_tokens
    assert small.tool_tokens < default.tool_tokens and small.page_size < default.page_size
    monkeypatch.setenv("WIKI_LLM_CONTEXT_TOKENS", "")
    assert model_budget() == default


@pytest.mark.parametrize("value", ["128k", "0", "-1", "4096"])
def test_invalid_context_is_explained(value, monkeypatch):
    monkeypatch.setenv("WIKI_LLM_CONTEXT_TOKENS", value)
    with pytest.raises(ValueError, match="WIKI_LLM_CONTEXT_TOKENS"):
        model_budget()


def test_contiguous_read_pages_cover_reference_but_gaps_do_not():
    files = {"a.py": {"lines": 200}}
    ref = Evidence(path="a.py", line=117, end_line=136, role="入口", note="")
    validate_evidence([ref], files, {"a.py": [(1, 120), (121, 180)]})
    with pytest.raises(ValueError, match="未通过"):
        validate_evidence([ref], files, {"a.py": [(1, 120), (122, 180)]})


def test_large_result_is_recorded_in_individual_calls():
    output = copy.deepcopy(OUTPUT)
    output["featureGroups"] = [{"title": f"分组 {group}", "summary": "功能", "features": [
        {**copy.deepcopy(OUTPUT["featureGroups"][0]["features"][0]), "title": f"功能 {group}-{index}"} for index in range(25)]} for group in range(2)]
    recorded = []
    def respond(messages, info):
        if not any(isinstance(part, ToolReturnPart) and part.tool_name == "read_file" for message in messages for part in message.parts):
            return ModelResponse(parts=[ToolCallPart("read_file", {"path": "main.py", "start": 1, "end": 2})])
        response = recorded_response(messages, info, output)
        recorded.extend(response.parts)
        return response
    result, usage = asyncio.run(analyze(FILES, lambda message: None, FunctionModel(respond)))
    assert sum(len(group.features) for group in result.featureGroups) == 50
    assert sum(part.tool_name == "save_feature" for part in recorded) == 50
    assert set(recorded[-1].args_as_dict()) == {"overview", "readingGuide", "limitations"}
    assert usage.requests == 53


def test_saved_records_survive_failure_and_are_not_generated_again():
    state = {}
    def checkpoint(value):
        state.clear()
        state.update(value)
    def fail_after_feature(messages, info):
        if any(isinstance(part, ToolReturnPart) and part.tool_name == "save_feature" for message in messages for part in message.parts):
            raise UnexpectedModelBehavior("temporary interruption")
        if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            return ModelResponse(parts=[ToolCallPart("read_file", {"path": "main.py", "start": 1, "end": 2})])
        return recorded_response(messages, info, OUTPUT)
    with pytest.raises(UnexpectedModelBehavior):
        asyncio.run(analyze(FILES, lambda message: None, FunctionModel(fail_after_feature), checkpoint=checkpoint))
    assert state["version"] == 2 and len(state["features"]) == 1
    calls = []
    def resume(messages, info):
        response = recorded_response(messages, info, OUTPUT)
        calls.extend(part.tool_name for part in response.parts)
        return response
    result, usage = asyncio.run(analyze(FILES, lambda message: None, FunctionModel(resume), state=json.loads(json.dumps(state))))
    assert len(result.featureGroups[0].features) == 1
    assert calls == ["save_node", "final_result"] and usage.requests == 2


def test_legacy_truncated_output_is_removed_but_read_results_remain():
    history = [ModelRequest(parts=[UserPromptPart(content="analyze")]),
               ModelResponse(parts=[ToolCallPart("read_file", {"path": "main.py"}, tool_call_id="read")]),
               ModelRequest(parts=[ToolReturnPart("read_file", "1: def run():", tool_call_id="read")]),
               ModelResponse(parts=[ToolCallPart("final_result", '{"featureGroups":[', tool_call_id="truncated")])]
    recovered = recover_messages(ModelMessagesTypeAdapter.dump_python(history, mode="json"), migrate=True)
    assert any(isinstance(part, ToolReturnPart) and part.tool_call_id == "read" for message in recovered for part in message.parts)
    assert not any(isinstance(part, ToolCallPart) and part.tool_call_id == "truncated" for message in recovered for part in message.parts)


def test_truncation_error_does_not_claim_old_validation_error_is_current():
    state = {"validation_error": "old reference error"}
    assert "old reference error" not in error_message(IncompleteToolCall("token limit"), state)


def test_history_trimming_preserves_tool_pairs():
    history = [ModelRequest(parts=[UserPromptPart(content="original")]),
        ModelResponse(parts=[ToolCallPart("read_file", {"path": "old"}, tool_call_id="a")]),
        ModelRequest(parts=[ToolReturnPart("read_file", "word " * 3000, tool_call_id="a")]),
        ModelResponse(parts=[ToolCallPart("read_file", {"path": "recent"}, tool_call_id="b")]),
        ModelRequest(parts=[ToolReturnPart("read_file", "recent evidence", tool_call_id="b")])]
    trimmed = asyncio.run(SlidingWindowCompaction(max_tokens=600, keep_tokens=200).compact(history, None))
    assert len(trimmed) < len(history) and message_tokens(trimmed) <= 600
    calls = {part.tool_call_id for message in trimmed for part in message.parts if isinstance(part, ToolCallPart)}
    returns = {part.tool_call_id for message in trimmed for part in message.parts if isinstance(part, ToolReturnPart)}
    assert calls == returns == {"b"}


def test_small_window_uses_real_capability_and_keeps_records(monkeypatch):
    monkeypatch.setenv("WIKI_LLM_CONTEXT_TOKENS", "8192")
    content = SOURCE + ("# " + "context " * 70 + "\n") * 20
    files = {"main.py": {**FILES["main.py"], "content": content, "lines": len(content.splitlines())}}
    step = 0
    settings = []
    def respond(messages, info):
        nonlocal step
        step += 1
        settings.append(info.model_settings)
        if step <= 10:
            return ModelResponse(parts=[ToolCallPart("read_file", {"path": "main.py", "start": 1, "end": 20})])
        if step == 11:
            return ModelResponse(parts=[ToolCallPart("save_feature", {"key": "f", "group": "核心", "summary": "功能", "feature": OUTPUT["featureGroups"][0]["features"][0]})])
        if step == 12:
            return ModelResponse(parts=[ToolCallPart("save_node", {"key": "n", **OUTPUT["architecture"][0]})])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, summary(OUTPUT))])
    logs = []
    result, usage = asyncio.run(analyze(files, logs.append, FunctionModel(respond)))
    assert result.featureGroups[0].features[0].steps[0].line == 1
    assert all(item["max_tokens"] == 2048 for item in settings)
    assert any("上下文" in line and "压缩" in line for line in logs)
