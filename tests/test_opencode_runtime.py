"""实际 OpenCode 二进制 + 仅本机假模型；不触发真实供应商请求。"""
import asyncio
import copy
import json
import os
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
from app.runtime.analysis import analyze, normalize_paths
from app.runtime.settings import environment, runtime_config
from app.runtime.workspace import materialize, collect_reads
from .test_agent import FILES, OUTPUT


@pytest.fixture
def native(tmp_path, monkeypatch):
    executable = os.getenv("WIKI_RUNTIME_TEST_BINARY")
    if not executable:
        candidate = Path(__file__).resolve().parents[1] / ".data/bin/opencode"
        executable = str(candidate) if candidate.exists() else None
    if not executable:
        pytest.skip("需要安装固定版本 OpenCode；CI 应设置 WIKI_RUNTIME_TEST_BINARY 运行实际运行时验收")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    fake = SimpleNamespace(calls=[], block=False, correction=False, timeouts=0, directory=False, reasoning_only=False, disconnected=threading.Event(), arrived=threading.Event())
    root = tmp_path / "runtime"
    app = FastAPI()
    @app.post("/v1/chat/completions")
    async def generate(request: Request):
        body = await request.json()
        fake.calls.append(body)
        assert body["model"] == "fake-model"
        names = {item["function"]["name"] for item in body.get("tools", [])}
        assert "bash" not in names and "edit" not in names and "task" not in names
        done = any(message["role"] == "tool" for message in body["messages"])
        if done and fake.timeouts:
            fake.timeouts -= 1
            return JSONResponse({"error": {"message": "The operation timed out.", "type": "invalid_request_error"}}, status_code=400)
        async def stream():
            def event(delta, finish=None):
                return "data: " + json.dumps({"id": "fake", "object": "chat.completion.chunk", "created": 0,
                    "model": "fake-model", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"
            if fake.reasoning_only and not done:
                fake.arrived.set()
                try:
                    yield event({"role": "assistant", "reasoning_content": "PRIVATE MODEL REASONING NEVER DISPLAY THIS"})
                    while not await request.is_disconnected():
                        await asyncio.sleep(.05)
                finally:
                    fake.disconnected.set()
                return
            if done and fake.block:
                fake.arrived.set()
                try:
                    yield event({"role": "assistant", "content": ""})
                    while not await request.is_disconnected():
                        await asyncio.sleep(.05)
                finally:
                    fake.disconnected.set()
                return
            if done and not (fake.directory and len(fake.calls) == 2):
                bad = fake.correction and not any("仅修正最终结果" in str(message.get("content", "")) for message in body["messages"])
                yield event({"role": "assistant", "content": "not JSON" if bad else json.dumps(OUTPUT)})
                yield event({}, "stop")
            else:
                yield event({"role": "assistant", "tool_calls": [{"index": 0, "id": "read-" + str(len(fake.calls)), "type": "function",
                    "function": {"name": "read", "arguments": json.dumps({"filePath": str(root / "opencode/workspace/repo" / ("" if fake.directory and len(fake.calls) == 1 else "main.py")), "offset": 1, "limit": 2})}}]})
                yield event({}, "tool_calls")
            yield "data: [DONE]\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream")
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(.02)
    monkeypatch.setenv("WIKI_OPENCODE_BIN", executable)
    monkeypatch.setenv("WIKI_LLM_API_KEY", "fake-key")
    monkeypatch.setenv("WIKI_LLM_BASE_URL", f"http://127.0.0.1:{port}/v1")
    monkeypatch.setenv("WIKI_LLM_MODEL", "fake-model")
    monkeypatch.setenv("WIKI_LLM_CONTEXT_TOKENS", "32768")
    state = {}
    def checkpoint(value):
        state.clear()
        state.update(copy.deepcopy(value))
    checkpoint.directory = root
    yield fake, state, checkpoint
    server.should_exit = True
    thread.join(5)
    listener.close()


def test_real_runtime_two_requests_delivers_verified_report(native):
    fake, state, checkpoint = native
    logs = []
    result, usage = asyncio.run(analyze(FILES, logs.append, checkpoint=checkpoint))
    assert result.overview == OUTPUT["overview"]
    assert state["engine"] == "opencode" and state["session_id"].startswith("ses_")
    assert state["reads"]["main.py"] == [(1, 2)]
    assert len(fake.calls) == usage.requests == 2
    assert any("阅读 main.py:1-2" == message for message in logs)
    assert fake.calls[0].get("max_tokens", fake.calls[0].get("max_completion_tokens")) == 8192


def test_real_runtime_correction_stays_in_same_session(native):
    fake, state, checkpoint = native
    fake.correction = True
    result, _ = asyncio.run(analyze(FILES, lambda message: None, checkpoint=checkpoint))
    assert result.overview == OUTPUT["overview"] and len(fake.calls) == 3
    assert sum(len(message["tool_calls"]) for message in fake.calls[-1]["messages"] if "tool_calls" in message) == 1
    assert "draft" in state


def test_real_runtime_stop_closes_model_and_resumes_native_session(native):
    fake, state, checkpoint = native
    fake.block = True
    async def stop():
        task = asyncio.create_task(analyze(FILES, lambda message: None, checkpoint=checkpoint))
        for _ in range(200):
            if state.get("reads") and fake.arrived.is_set():
                break
            await asyncio.sleep(.05)
        assert fake.arrived.is_set() and state["reads"]["main.py"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(stop())
    assert fake.disconnected.wait(5)
    saved = json.loads(json.dumps(state))
    original = saved["session_id"]
    before = len(fake.calls)
    fake.block = False
    result, _ = asyncio.run(analyze(FILES, lambda message: None, state=saved, checkpoint=checkpoint))
    assert state["session_id"] == original and result.overview == OUTPUT["overview"]
    assert len(fake.calls) == before + 1


def test_config_isolates_personal_credentials_and_denies_write(tmp_path, monkeypatch):
    monkeypatch.setenv("UNRELATED_API_KEY", "personal-secret")
    env = environment(tmp_path, "runtime-password")
    assert "UNRELATED_API_KEY" not in env
    config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert config["permission"]["*"] == "deny"
    assert config["permission"]["external_directory"] == "deny"
    assert config["permission"]["read"]["*"] == "deny"
    assert config["model"] == config["small_model"]
    assert config["enabled_providers"] == ["wiki"]


def test_default_window_reserves_reasoning_output_without_filling_context(monkeypatch):
    monkeypatch.setenv("WIKI_LLM_CONTEXT_TOKENS", "128000")
    limits = runtime_config()["provider"]["wiki"]["models"]["project"]["limit"]
    assert limits == {"context": 128000, "input": 64000, "output": 16384}


def test_snapshot_rejects_changed_source_and_path_escape(tmp_path):
    materialize(tmp_path, FILES)
    changed = copy.deepcopy(FILES)
    changed["main.py"]["content"] = "changed"
    with pytest.raises(ValueError, match="快照不一致"):
        materialize(tmp_path, changed)
    with pytest.raises(ValueError, match="越界"):
        materialize(tmp_path / "other", {"../../escape": FILES["main.py"]})


def test_evidence_uses_returned_lines_not_requested_limit(tmp_path):
    workspace = materialize(tmp_path, FILES)
    messages = [{"parts": [{"id": "r", "type": "tool", "tool": "read", "state": {"status": "completed",
                 "input": {"filePath": str(workspace / "repo/main.py"), "offset": 1, "limit": 100},
                 "output": "<content>\n1: def run():\n2: wrong line\n</content>"}}]}]
    reads = {}
    collect_reads(messages, workspace, FILES, reads, set(), lambda message: None)
    assert reads == {"main.py": [(1, 1)]}


def test_canonical_paths_only_normalize_existing_snapshot_files(tmp_path):
    from app.analysis.schemas import Analysis
    output = copy.deepcopy(OUTPUT)
    for group in output["featureGroups"]:
        for feature in group["features"]:
            feature["steps"][0]["path"] = "repo/main.py"
    output["architecture"][0]["evidence"][0]["path"] = str(tmp_path / "repo/main.py")
    result = Analysis.model_validate(output)
    normalize_paths(result, tmp_path, FILES)
    assert result.featureGroups[0].features[0].steps[0].path == "main.py"
    assert result.architecture[0].evidence[0].path == "main.py"


def test_real_stream_delivers_actions_before_request_finishes_without_polling(native):
    from app.runtime.client import Runtime
    fake, state, checkpoint = native
    fake.block = True
    actions = []
    connections = []
    def log(message):
        pass
    log.action = actions.append
    class CountRuntime(Runtime):
        def __init__(self, *args):
            super().__init__(*args)
            self.message_reads = 0
            connections.append(self)
        async def messages(self):
            self.message_reads += 1
            return await super().messages()
    async def run():
        task = asyncio.create_task(analyze(FILES, log, checkpoint=checkpoint, runtime_factory=CountRuntime))
        for _ in range(200):
            if state.get("reads") and fake.arrived.is_set():
                break
            await asyncio.sleep(.02)
        assert not task.done()
        assert any(item["kind"] == "tool" and item["status"] == "completed" and item["tool"] == "read" for item in actions)
        assert any(item["kind"] == "model" and item["status"] == "running" for item in actions)
        assert connections[0].message_reads == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())


def test_real_timeout_is_automatically_retried_in_same_native_session(native, monkeypatch):
    import app.runtime.client as module
    monkeypatch.setattr(module, "RETRY_DELAYS", (.01, .01))
    fake, state, checkpoint = native
    fake.timeouts = 1
    logs = []
    result, _ = asyncio.run(analyze(FILES, logs.append, checkpoint=checkpoint))
    assert result.overview == OUTPUT["overview"] and len(fake.calls) == 3
    assert any("自动重试 1/2" in message for message in logs)
    assert state["reads"]["main.py"] == [(1, 2)]
    assert "继续当前尚未完成" in str(fake.calls[-1]["messages"])


def test_real_runtime_can_read_snapshot_root_directory(native):
    fake, state, checkpoint = native
    fake.directory = True
    logs = []
    result, _ = asyncio.run(analyze(FILES, logs.append, checkpoint=checkpoint))
    assert result.overview == OUTPUT["overview"] and len(fake.calls) == 3
    assert state["reads"]["main.py"] == [(1, 2)]
    assert any("仓库根目录" in message and "completed" in message for message in logs)


def test_real_reasoning_stream_produces_progress_counts_without_text(native):
    fake, state, checkpoint = native
    fake.reasoning_only = True
    actions = []
    def log(message):
        pass
    log.action = actions.append
    async def run():
        task = asyncio.create_task(analyze(FILES, log, checkpoint=checkpoint))
        for _ in range(300):
            if any(item["kind"] == "generation" for item in actions):
                break
            await asyncio.sleep(.02)
        try:
            assert not task.done()
            assert any(item["kind"] == "generation" and "0 次" in item["detail"] for item in actions)
            assert "PRIVATE MODEL REASONING" not in json.dumps(actions)
            assert not state.get("reads")
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    asyncio.run(run())


def test_real_runtime_budget_handoff_disables_further_tools(native, monkeypatch):
    from app.runtime import overview
    monkeypatch.setattr(overview, "MAX_TOOLS", 1)
    fake, state, checkpoint = native
    result, _ = asyncio.run(analyze(FILES, lambda message: None, checkpoint=checkpoint))
    assert result.overview == OUTPUT["overview"]
    assert state["reads"]["main.py"]
    final = fake.calls[-1]
    assert not any(item["function"]["name"] in {"read", "glob", "grep", "todowrite"} for item in final.get("tools", []))
    assert any("禁止继续调用工具" in str(message.get("content", "")) for message in final["messages"])


def test_real_runtime_resume_resets_tool_deny_without_wildcard_allow(native):
    from app.runtime.client import Runtime
    fake, state, checkpoint = native
    async def run():
        directory = checkpoint.directory / "opencode"
        workspace = materialize(directory, FILES)
        async with Runtime(directory, workspace, lambda message: None) as runtime:
            identity = await runtime.session()
            response = await runtime.client.patch("/session/" + identity, json={"permission": [
                {"permission": "read", "pattern": "*", "action": "deny"}]})
            response.raise_for_status()
            assert await runtime.session(identity) == identity
            restored = await runtime.get("/session/" + identity)
            from app.runtime.settings import session_permissions
            rules = session_permissions(workspace)
            # 原生 PATCH 追加规则，最新规则恢复相同的受限读取范围。
            assert restored["permission"][-len(rules):] == rules
            assert {"permission": "read", "pattern": "*", "action": "allow"} not in restored["permission"]
            text = await runtime.prompt("Read repo/main.py and return the report", lambda messages: None)
            assert json.loads(text)["overview"] == OUTPUT["overview"]
    asyncio.run(run())
    assert len(fake.calls) == 2
