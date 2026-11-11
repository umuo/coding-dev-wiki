import asyncio
import json
import httpx
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart, RetryPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from app.analysis.agent import analyze, ask
from app.analysis.index import extract_symbols
from .test_jobs import upload, wait
from .model_responses import recorded_response, summary

SOURCE = "def run():\n    return 42\n"
FILES = {"main.py": {"path": "main.py", "name": "main.py", "lang": "python", "lines": 2,
                     "content": SOURCE, "symbols": extract_symbols(SOURCE, "python")}}
REF = {"path": "main.py", "line": 1, "end_line": 2, "symbol": "run", "role": "入口", "note": "返回 42"}
OUTPUT = {"overview": "返回一个常量的示例项目。", "readingGuide": ["从 run 开始阅读"],
          "featureGroups": [{"title": "常量返回", "summary": "核心功能", "features": [
              {"title": "返回 42", "desc": "run 返回 42", "steps": [REF]}]}],
          "architecture": [{"title": "入口模块", "description": "实现 run", "evidence": [REF]}],
          "edges": [], "limitations": ["仅静态分析"]}


def test_framework_tool_loop_output_retry_and_question():
    seen = []
    def respond(messages, info):
        seen.extend(type(part).__name__ for message in messages[-1:] for part in message.parts)
        read = any(isinstance(part, ToolReturnPart) and part.tool_name == "read_file" for message in messages for part in message.parts)
        if not read:
            return ModelResponse(parts=[ToolCallPart("read_file", {"path": "main.py", "start": 1, "end": 2})])
        return recorded_response(messages, info, OUTPUT)
    log = []
    analysis, usage = asyncio.run(analyze(FILES, log.append, FunctionModel(respond)))
    assert analysis.featureGroups[0].features[0].steps[0].end_line == 2
    assert usage.requests == 4 and any("阅读" in item for item in log)
    assert "ToolReturnPart" in seen
    def answer(messages, info):
        read = any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts)
        if not read:
            return ModelResponse(parts=[ToolCallPart("read_file", {"path": "main.py", "start": 1, "end": 2})])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"answer": "run 返回 42", "citations": [REF]})])
    result = asyncio.run(ask(FILES, OUTPUT, "返回什么？", [], FunctionModel(answer)))
    assert result["citations"][0]["path"] == "main.py"
    attempts = 0
    def fabricate_then_fix(messages, info):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return recorded_response(messages, info, OUTPUT)
        if attempts == 2:
            assert any(isinstance(part, RetryPromptPart) for part in messages[-1].parts)
            return ModelResponse(parts=[ToolCallPart("read_file", {"path": "main.py", "start": 1, "end": 2})])
        return recorded_response(messages, info, OUTPUT)
    asyncio.run(analyze(FILES, lambda message: None, FunctionModel(fabricate_then_fix)))
    assert attempts == 5


def test_openai_compatible_http_provider():
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.url.path == "/v1/chat/completions"
        completed = {message["tool_call_id"] for message in body["messages"] if message["role"] == "tool"}
        names = {call["function"]["name"] for message in body["messages"] for call in message.get("tool_calls", []) if call["id"] in completed}
        if "read_file" not in names:
            tool, arguments = "read_file", {"path": "main.py", "start": 1, "end": 2}
        elif "save_feature" not in names:
            tool, arguments = "save_feature", {"key": "f", "group": "核心", "summary": "核心功能", "feature": OUTPUT["featureGroups"][0]["features"][0]}
        elif "save_node" not in names:
            tool, arguments = "save_node", {"key": "n", **OUTPUT["architecture"][0]}
        else:
            tool = "final_result"
            arguments = summary(OUTPUT)
        return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0, "model": "test-model",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": f"call-{len(requests)}", "type": "function", "function": {"name": tool, "arguments": json.dumps(arguments)}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = OpenAIChatModel("test-model", provider=OpenAIProvider(base_url="https://test.invalid/v1", api_key="test", http_client=client))
            return await analyze(FILES, lambda message: None, model)
    result, usage = asyncio.run(run())
    assert result.overview == OUTPUT["overview"]
    assert len(requests) == 4 and usage.input_tokens == 40


def test_agent_job_pipeline(client, monkeypatch):
    from app import jobs
    from .conftest import zip_bytes
    real_analyze = analyze
    def respond(messages, info):
        read = any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts)
        if not read:
            return ModelResponse(parts=[ToolCallPart("read_file", {"path": "main.py", "start": 1, "end": 2})])
        return recorded_response(messages, info, OUTPUT)
    async def with_framework(files, log, **kwargs):
        return await real_analyze(files, log, FunctionModel(respond))
    monkeypatch.setenv("WIKI_LLM_API_KEY", "test")
    monkeypatch.setenv("WIKI_LLM_MODEL", "test")
    monkeypatch.setattr(jobs, "analyze", with_framework)
    identifier = upload(client, zip_bytes({"main.py": SOURCE}), "agent")
    job = wait(client, identifier)
    assert job["status"] == "completed", job
    result = client.get(f"/api/jobs/{identifier}/result").json()
    assert result["mode"] == "agent" and result["usage"]["requests"] == 4
    assert result["featureGroups"][0]["features"][0]["steps"][0]["line"] == 1


def test_chat_route_uses_snapshot_and_framework(client, monkeypatch):
    from app import api as routes
    from .conftest import zip_bytes
    def respond(messages, info):
        read = any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts)
        if not read:
            return ModelResponse(parts=[ToolCallPart("read_file", {"path": "main.py", "start": 1, "end": 2})])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"answer": "run 返回 42", "citations": [REF]})])
    async def framework_ask(files, analysis, question, history):
        assert files["main.py"]["content"] == SOURCE
        return await ask(files, analysis, question, history, FunctionModel(respond))
    monkeypatch.setattr(routes, "ask", framework_ask)
    monkeypatch.setenv("WIKI_LLM_API_KEY", "test")
    monkeypatch.setenv("WIKI_LLM_MODEL", "test")
    identifier = upload(client, zip_bytes({"main.py": SOURCE}))
    assert wait(client, identifier)["status"] == "completed"
    response = client.post(f"/api/jobs/{identifier}/chat", json={"question": "返回什么", "history": [{"role": "user", "content": "入口是什么"}]})
    assert response.status_code == 200
    assert response.json()["citations"][0]["end_line"] == 2


def test_agent_failure_is_not_published_as_success(client, monkeypatch):
    from app import jobs
    from .conftest import zip_bytes
    def no_read(messages, info):
        return recorded_response(messages, info, OUTPUT)
    async def invalid(files, log, **kwargs):
        return await analyze(files, log, FunctionModel(no_read))
    monkeypatch.setenv("WIKI_LLM_API_KEY", "test")
    monkeypatch.setenv("WIKI_LLM_MODEL", "test")
    monkeypatch.setattr(jobs, "analyze", invalid)
    identifier = upload(client, zip_bytes({"main.py": SOURCE}), "agent")
    assert wait(client, identifier)["status"] == "failed"
    assert client.get(f"/api/jobs/{identifier}/result").status_code == 409
