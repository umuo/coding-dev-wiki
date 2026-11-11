import asyncio
import copy
import json
import pytest
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelResponse, ModelRequest, ToolCallPart, ToolReturnPart, RetryPromptPart, UserPromptPart, ModelMessagesTypeAdapter
from pydantic_ai.exceptions import UnexpectedModelBehavior
from app.analysis.agent import analyze
from app.analysis.tool_arguments import clean_failed_calls
from .test_agent import FILES, OUTPUT
from .model_responses import recorded_response


def payload(output=OUTPUT):
    return {"key": "feature-0-0", "group": output["featureGroups"][0]["title"],
            "summary": output["featureGroups"][0]["summary"], "feature": copy.deepcopy(output["featureGroups"][0]["features"][0])}


def read_response(messages):
    if not any(isinstance(part, ToolReturnPart) and part.tool_name == "read_file" for message in messages for part in message.parts):
        return ModelResponse(parts=[ToolCallPart("read_file", {"path": "main.py", "start": 1, "end": 2})])
    return None


def was_saved(messages):
    return any(isinstance(part, ToolReturnPart) and part.tool_name == "save_feature" for message in messages for part in message.parts)


@pytest.mark.parametrize("kind", ["missing_colon", "trailing_comma", "encoded_feature", "encoded_steps"])
def test_repair_and_embedded_json_remain_schema_validated(kind):
    values = payload()
    if kind in {"encoded_steps", "combined"}:
        values["feature"]["steps"] = json.dumps(values["feature"]["steps"], ensure_ascii=False)
    if kind in {"encoded_feature", "combined"}:
        values["feature"] = json.dumps(values["feature"], ensure_ascii=False)
    raw = json.dumps(values, ensure_ascii=False)
    if kind in {"missing_colon", "combined"}:
        raw = raw.replace('"group":', '"group" ', 1)
    if kind == "trailing_comma":
        raw = raw[:-1] + ",}"
    def respond(messages, info):
        read = read_response(messages)
        if read:
            return read
        if not was_saved(messages):
            return ModelResponse(parts=[ToolCallPart("save_feature", raw)])
        # Validated repairs must be normalized in the history seen by the next model request.
        calls = [part for message in messages for part in message.parts if isinstance(part, ToolCallPart) and part.tool_name == "save_feature"]
        assert isinstance(calls[-1].args, dict)
        assert isinstance(calls[-1].args["feature"], dict)
        assert isinstance(calls[-1].args["feature"]["steps"], list)
        return recorded_response(messages, info, OUTPUT)
    logs = []
    result, usage = asyncio.run(analyze(FILES, logs.append, FunctionModel(respond)))
    assert len(result.featureGroups[0].features) == 1
    assert any("规范化 save_feature" in line for line in logs)
    assert usage.requests == 4


def test_protocol_pollution_requires_regeneration_and_does_not_contaminate_history():
    emitted = False
    bad_id = "bad-dsml"
    raw = '{"key">viewer-navigation</parameter>\n<｜DSML｜:'
    def respond(messages, info):
        nonlocal emitted
        read = read_response(messages)
        if read:
            return read
        if not emitted:
            emitted = True
            return ModelResponse(parts=[ToolCallPart("save_feature", raw, tool_call_id=bad_id)])
        assert not any(isinstance(part, ToolCallPart) and part.tool_call_id == bad_id for message in messages for part in message.parts)
        assert not any(isinstance(part, (RetryPromptPart, ToolReturnPart)) and part.tool_call_id == bad_id for message in messages for part in message.parts)
        return recorded_response(messages, info, OUTPUT)
    logs = []
    result, usage = asyncio.run(analyze(FILES, logs.append, FunctionModel(respond)))
    assert len(result.featureGroups[0].features) == 1
    assert any("DSML" in line for line in logs)
    assert any("移除" in line for line in logs)
    assert usage.requests == 5


def test_repairs_do_not_bypass_source_evidence_checks():
    values = payload()
    values["feature"]["steps"][0]["end_line"] = 99
    raw = json.dumps(values)[:-1] + ",}"
    state = {}
    def checkpoint(value):
        state.clear()
        state.update(value)
    def respond(messages, info):
        return read_response(messages) or ModelResponse(parts=[ToolCallPart("save_feature", raw)])
    with pytest.raises(UnexpectedModelBehavior):
        asyncio.run(analyze(FILES, lambda message: None, FunctionModel(respond), checkpoint=checkpoint))
    assert not state["features"]
    assert "无效引用" in state["validation_error"]


def test_protocol_failures_are_bounded_and_never_saved():
    calls = 0
    def respond(messages, info):
        nonlocal calls
        read = read_response(messages)
        if read:
            return read
        calls += 1
        return ModelResponse(parts=[ToolCallPart("save_feature", '{"key">bad</parameter>')])
    state = {}
    def checkpoint(value):
        state.clear()
        state.update(value)
    with pytest.raises(UnexpectedModelBehavior):
        asyncio.run(analyze(FILES, lambda message: None, FunctionModel(respond), checkpoint=checkpoint))
    assert calls == 5
    assert not state["features"]


def test_valid_business_text_is_not_rewritten_as_protocol():
    output = copy.deepcopy(OUTPUT)
    text = '<parameter name="example">业务原文</parameter>'
    output["featureGroups"][0]["features"][0]["desc"] = text
    def respond(messages, info):
        return read_response(messages) or recorded_response(messages, info, output)
    result, usage = asyncio.run(analyze(FILES, lambda message: None, FunctionModel(respond)))
    assert result.featureGroups[0].features[0].desc == text


def test_saved_ten_features_survive_format_failure_and_resume():
    output = copy.deepcopy(OUTPUT)
    template = output["featureGroups"][0]["features"][0]
    output["featureGroups"][0]["features"] = [{**copy.deepcopy(template), "title": f"功能 {index}"} for index in range(10)]
    state = {}
    def checkpoint(value):
        state.clear()
        state.update(value)
    def fail(messages, info):
        read = read_response(messages)
        if read:
            return read
        saved = sum(isinstance(part, ToolReturnPart) and part.tool_name == "save_feature" for message in messages for part in message.parts)
        if saved == 10:
            return ModelResponse(parts=[ToolCallPart("save_feature", '{"key">next</parameter>')])
        return recorded_response(messages, info, output)
    with pytest.raises(UnexpectedModelBehavior):
        asyncio.run(analyze(FILES, lambda message: None, FunctionModel(fail), checkpoint=checkpoint))
    assert len(state["features"]) == 10
    saved_before = copy.deepcopy(state["features"])
    def resume(messages, info):
        response = recorded_response(messages, info, output)
        assert all(part.tool_name != "save_feature" for part in response.parts)
        return response
    result, usage = asyncio.run(analyze(FILES, lambda message: None, FunctionModel(resume), state=json.loads(json.dumps(state))))
    assert len(result.featureGroups[0].features) == 10
    assert state["features"] == saved_before and usage.requests == 2


def test_cleaning_preserves_valid_calls_and_matching_results():
    messages = [ModelRequest(parts=[UserPromptPart(content="analyze")]),
        ModelResponse(parts=[ToolCallPart("read_file", {"path": "a.py"}, tool_call_id="read"), ToolCallPart("save_feature", '{"key">bad', tool_call_id="bad")]),
        ModelRequest(parts=[ToolReturnPart("read_file", "code", tool_call_id="read"), RetryPromptPart("bad JSON", tool_name="save_feature", tool_call_id="bad")])]
    cleaned, count = clean_failed_calls(messages)
    assert count == 1
    calls = {part.tool_call_id for message in cleaned for part in message.parts if isinstance(part, ToolCallPart)}
    results = {part.tool_call_id for message in cleaned for part in message.parts if isinstance(part, ToolReturnPart)}
    assert calls == results == {"read"}


def test_ambiguous_compound_damage_regenerates_instead_of_guessing_values():
    values = payload()
    values["feature"]["steps"] = json.dumps(values["feature"]["steps"], ensure_ascii=False)
    values["feature"] = json.dumps(values["feature"], ensure_ascii=False)
    raw = json.dumps(values, ensure_ascii=False).replace('"group":', '"group" ', 1)
    attempted = False
    def respond(messages, info):
        nonlocal attempted
        read = read_response(messages)
        if read:
            return read
        if not attempted:
            attempted = True
            return ModelResponse(parts=[ToolCallPart("save_feature", raw)])
        return recorded_response(messages, info, OUTPUT)
    logs = []
    result, usage = asyncio.run(analyze(FILES, logs.append, FunctionModel(respond)))
    assert len(result.featureGroups[0].features) == 1
    assert any("参数校验失败" in line for line in logs)
    assert usage.requests == 5


def test_openai_wire_protocol_recovers_bad_args_and_normalizes_history():
    import httpx
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider
    from .model_responses import summary
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        number = len(requests)
        if number == 1:
            name, args, identity = "read_file", json.dumps({"path": "main.py", "start": 1, "end": 2}), "read"
        elif number == 2:
            name, args, identity = "save_feature", '{"key">bad</parameter>', "invalid-dsml"
        elif number == 3:
            assert not any(call["id"] == "invalid-dsml" for message in body["messages"] for call in message.get("tool_calls", []))
            assert not any(message.get("tool_call_id") == "invalid-dsml" for message in body["messages"])
            values = payload()
            values["feature"] = json.dumps(values["feature"], ensure_ascii=False)
            name, args, identity = "save_feature", json.dumps(values, ensure_ascii=False), "feature"
        elif number == 4:
            call = next(call for message in body["messages"] for call in message.get("tool_calls", []) if call["id"] == "feature")
            assert isinstance(json.loads(call["function"]["arguments"])["feature"], dict)
            name, args, identity = "save_node", json.dumps({"key": "node-0", **OUTPUT["architecture"][0]}, ensure_ascii=False), "node"
        else:
            name, args, identity = "final_result", json.dumps(summary(OUTPUT), ensure_ascii=False), "final"
        return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0, "model": "test",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": identity, "type": "function", "function": {"name": name, "arguments": args}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = OpenAIChatModel("test-model", provider=OpenAIProvider(base_url="https://test.invalid/v1", api_key="test", http_client=client))
            return await analyze(FILES, lambda message: None, model)
    result, usage = asyncio.run(run())
    assert len(result.featureGroups[0].features) == 1 and len(requests) == 5
