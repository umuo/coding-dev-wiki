import asyncio
import shutil
import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart, ModelMessagesTypeAdapter, ModelRequest, RetryPromptPart
from app import jobs, store
from app.analysis.agent import analyze
from app.analysis import checkpoints
from .test_agent import FILES, OUTPUT, SOURCE
from .test_jobs import upload, wait
from .conftest import zip_bytes
from .model_responses import recorded_response


def failing_model(messages, info):
    read = any(isinstance(part, ToolReturnPart) and part.tool_name == "read_file" for message in messages for part in message.parts)
    if not read:
        return ModelResponse(parts=[ToolCallPart("read_file", {"path": "main.py", "start": 1, "end": 2})])
    raise UnexpectedModelBehavior("temporary upstream response failure")


def successful_resume(messages, info):
    assert any(isinstance(part, ToolReturnPart) and part.tool_name == "read_file" for message in messages for part in message.parts)
    return recorded_response(messages, info, OUTPUT)


def test_framework_resume_preserves_messages_and_read_evidence():
    state = {}
    def save(value):
        state.clear()
        state.update(value)
    with pytest.raises(UnexpectedModelBehavior):
        asyncio.run(analyze(FILES, lambda message: None, FunctionModel(failing_model), checkpoint=save))
    assert state["reads"]["main.py"] == [(1, 2)]
    assert state["messages"]
    # Cross a JSON persistence boundary exactly as a server restart would.
    import json
    restored = json.loads(json.dumps(state))
    logs = []
    output, usage = asyncio.run(analyze(FILES, logs.append, FunctionModel(successful_resume), state=restored, checkpoint=save))
    assert output.overview == OUTPUT["overview"]
    assert not any("阅读" in line for line in logs)
    assert usage.requests == 3


def test_pending_tool_call_is_resolved_as_interrupted_not_success():
    raw = ModelMessagesTypeAdapter.dump_python([ModelResponse(parts=[ToolCallPart("read_file", {"path": "main.py"}, tool_call_id="interrupted")])], mode="json")
    history = checkpoints.recover_messages(raw)
    assert isinstance(history[-1], ModelRequest)
    part = history[-1].parts[0]
    assert isinstance(part, RetryPromptPart) and part.tool_call_id == "interrupted"
    assert "不能视为成功" in part.content


def test_continue_same_job_from_persisted_snapshot(client, monkeypatch):
    monkeypatch.setenv("WIKI_LLM_MODEL", "test")
    monkeypatch.setenv("WIKI_LLM_API_KEY", "test-key")
    resumed = False
    async def framework(files, log, **kwargs):
        return await analyze(files, log, FunctionModel(successful_resume if resumed else failing_model), **kwargs)
    monkeypatch.setattr(jobs, "analyze", framework)
    identifier = upload(client, zip_bytes({"main.py": SOURCE}), "agent")
    failed = wait(client, identifier)
    assert failed["status"] == "failed"
    assert "temporary upstream response failure" in failed["error"]
    indexed = checkpoints.load(identifier, "source-index")
    state = checkpoints.load(identifier, "agent-state")
    assert indexed and state["reads"]
    # Removing original artifacts proves the continuation uses the saved snapshot.
    (store.directory(identifier) / "upload.zip").unlink()
    shutil.rmtree(store.directory(identifier) / "source")
    resumed = True
    response = client.post(f"/api/jobs/{identifier}/continue")
    assert response.status_code == 202 and response.json()["id"] == identifier
    assert wait(client, identifier)["status"] == "completed"
    result = client.get(f"/api/jobs/{identifier}/result").json()
    assert result["snapshot"] == indexed["fingerprint"]
    assert result["files"]["main.py"]["content"] == SOURCE
    assert sum("阅读 main.py" in event["message"] for event in store.events(identifier)) == 1
    assert client.post(f"/api/jobs/{identifier}/continue").status_code == 409


def test_legacy_failed_task_is_honest_about_missing_checkpoint(client, monkeypatch):
    monkeypatch.setenv("WIKI_LLM_MODEL", "test")
    monkeypatch.setenv("WIKI_LLM_API_KEY", "test-key")
    def fresh(messages, info):
        if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            return ModelResponse(parts=[ToolCallPart("read_file", {"path": "main.py", "start": 1, "end": 2})])
        return recorded_response(messages, info, OUTPUT)
    async def framework(files, log, **kwargs):
        return await analyze(files, log, FunctionModel(fresh), **kwargs)
    monkeypatch.setattr(jobs, "analyze", framework)
    job = store.create("legacy", "zip", "uploaded ZIP", "agent")
    (store.directory(job["id"]) / "upload.zip").write_bytes(zip_bytes({"main.py": SOURCE}))
    store.event(job["id"], "处理失败", 50, "old error", status="failed")
    assert client.post(f"/api/jobs/{job['id']}/continue").status_code == 202
    assert wait(client, job["id"])["status"] == "completed"
    assert any("无法恢复之前的 Agent 上下文" in event["message"] for event in store.events(job["id"]))


def test_continue_claim_is_atomic_and_requires_access(client, monkeypatch):
    from app import config
    monkeypatch.setenv("WIKI_LLM_MODEL", "test")
    monkeypatch.setenv("WIKI_LLM_API_KEY", "test-key")
    job = store.create("failed", "zip", "", "agent")
    store.event(job["id"], "处理失败", 50, "error", status="failed")
    submissions = []
    monkeypatch.setattr(jobs, "submit", lambda identifier, resume=False: submissions.append((identifier, resume)))
    monkeypatch.setattr(config, "ACCESS_TOKEN", "secret")
    assert client.post(f"/api/jobs/{job['id']}/continue").status_code == 401
    client.post("/api/login", json={"token": "secret"})
    assert client.post(f"/api/jobs/{job['id']}/continue").status_code == 202
    assert client.post(f"/api/jobs/{job['id']}/continue").status_code == 409
    assert submissions == [(job["id"], True)]


def test_production_continue_cannot_bypass_local_guard(client, monkeypatch):
    monkeypatch.setenv("WIKI_ENV", "production")
    job = store.create("local", "local", "/tmp", "agent")
    store.event(job["id"], "处理失败", 50, "error", status="failed")
    assert client.post(f"/api/jobs/{job['id']}/continue").status_code == 404
    assert store.get(job["id"])["status"] == "failed"


def test_error_details_mask_key(monkeypatch):
    monkeypatch.setenv("WIKI_LLM_API_KEY", "private-test-key")
    message = checkpoints.error_message(UnexpectedModelBehavior("failure private-test-key"))
    assert "private-test-key" not in message and "[REDACTED]" in message


def test_output_validation_exhaustion_can_be_manually_resumed():
    import copy
    invalid = copy.deepcopy(OUTPUT)
    invalid["featureGroups"][0]["features"][0]["steps"][0]["end_line"] = 20
    def invalid_output(messages, info):
        if not any(isinstance(part, ToolReturnPart) and part.tool_name == "read_file" for message in messages for part in message.parts):
            return ModelResponse(parts=[ToolCallPart("read_file", {"path": "main.py", "start": 1, "end": 2})])
        return recorded_response(messages, info, invalid)
    state = {}
    def save(value):
        state.clear()
        state.update(value)
    with pytest.raises(UnexpectedModelBehavior):
        asyncio.run(analyze(FILES, lambda message: None, FunctionModel(invalid_output), checkpoint=save))
    assert "无效引用" in state["validation_error"]
    output, usage = asyncio.run(analyze(FILES, lambda message: None, FunctionModel(successful_resume), state=state))
    assert output.featureGroups[0].features[0].steps[0].end_line == 2
    assert usage.requests == 3
