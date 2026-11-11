import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
import httpx
import pytest
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from app import jobs, store, config, execution
from app.analysis.agent import analyze
from app.analysis import checkpoints
from app.importing import sources
from app.analysis.index import build_index
from .conftest import zip_bytes
from .test_jobs import upload, wait
from .test_agent import SOURCE, OUTPUT
from .test_resume import successful_resume
from .model_responses import recorded_response


def model_ready(monkeypatch):
    monkeypatch.setenv("WIKI_LLM_MODEL", "test")
    monkeypatch.setenv("WIKI_LLM_API_KEY", "test")


def test_stop_queued_future_and_repeated_stop(client, monkeypatch):
    release = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        blocker = pool.submit(release.wait, 5)
        monkeypatch.setattr(jobs, "POOL", pool)
        job = store.create("queued", "zip", "", "preview")
        future = jobs.submit(job["id"])
        try:
            assert client.post(f"/api/jobs/{job['id']}/stop").status_code == 202
            assert store.get(job["id"])["status"] == "stopped"
            assert future.cancelled()
            assert client.post(f"/api/jobs/{job['id']}/stop").status_code == 202
            assert client.get(f"/api/jobs/{job['id']}/result").status_code == 409
        finally:
            release.set()
            blocker.result(timeout=2)


def test_stop_before_submit_never_runs_analysis(client, monkeypatch):
    called = []
    monkeypatch.setattr(jobs, "unpack", lambda *args, **kwargs: called.append(True))
    job = store.create("stopped", "zip", "", "preview")
    assert client.post(f"/api/jobs/{job['id']}/stop").status_code == 202
    jobs.submit(job["id"]).result(timeout=2)
    assert not called and store.get(job["id"])["status"] == "stopped"


def test_running_model_stop_preserves_records_and_can_continue(client, monkeypatch):
    model_ready(monkeypatch)
    waiting = threading.Event()
    cancelled = threading.Event()
    async def blocked(messages, info):
        if any(isinstance(part, ToolReturnPart) and part.tool_name == "save_feature" for message in messages for part in message.parts):
            waiting.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            from pydantic_ai.messages import ModelResponse, ToolCallPart
            return ModelResponse(parts=[ToolCallPart("read_file", {"path": "main.py", "start": 1, "end": 2})])
        return recorded_response(messages, info, OUTPUT)
    async def framework(files, log, **kwargs):
        return await analyze(files, log, FunctionModel(blocked), **kwargs)
    monkeypatch.setattr(jobs, "analyze", framework)
    identifier = upload(client, zip_bytes({"main.py": SOURCE}), "agent")
    assert waiting.wait(5)
    assert client.get(f"/api/jobs/{identifier}").json()["status"] == "running"
    assert client.post(f"/api/jobs/{identifier}/stop").status_code == 202
    assert wait(client, identifier)["status"] == "stopped"
    assert cancelled.wait(2)
    state = checkpoints.load(identifier, "agent-state")
    assert len(state["features"]) == 1 and state["reads"]["main.py"]
    assert not (store.directory(identifier) / "result.json").exists()
    stream = client.get(f"/api/jobs/{identifier}/events").text
    assert "event: state" in stream and '"status": "stopped"' in stream and "event: done" in stream
    async def resume(files, log, **kwargs):
        return await analyze(files, log, FunctionModel(successful_resume), **kwargs)
    monkeypatch.setattr(jobs, "analyze", resume)
    assert client.post(f"/api/jobs/{identifier}/continue").status_code == 202
    assert wait(client, identifier)["status"] == "completed"
    assert len(client.get(f"/api/jobs/{identifier}/result").json()["featureGroups"][0]["features"]) == 1


def test_pending_http_request_is_actually_cancelled(client, monkeypatch):
    model_ready(monkeypatch)
    waiting = threading.Event()
    closed = threading.Event()
    async def handler(request):
        waiting.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()
    async def framework(files, log, **kwargs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            provider = OpenAIProvider(base_url="https://test.invalid/v1", api_key="test", http_client=http)
            return await analyze(files, log, OpenAIChatModel("test-model", provider=provider), **kwargs)
    monkeypatch.setattr(jobs, "analyze", framework)
    identifier = upload(client, zip_bytes({"main.py": SOURCE}), "agent")
    assert waiting.wait(5)
    assert client.post(f"/api/jobs/{identifier}/stop").status_code == 202
    assert wait(client, identifier)["status"] == "stopped"
    assert closed.wait(2)


def test_stopping_waits_for_cleanup_and_does_not_get_cancelled_twice(client, monkeypatch):
    model_ready(monkeypatch)
    waiting = threading.Event()
    cleanup = threading.Event()
    release = threading.Event()
    completed_cleanup = threading.Event()
    async def framework(files, log, **kwargs):
        waiting.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            await asyncio.to_thread(release.wait, 5)
            completed_cleanup.set()
    monkeypatch.setattr(jobs, "analyze", framework)
    identifier = upload(client, zip_bytes({"main.py": SOURCE}), "agent")
    assert waiting.wait(5)
    try:
        assert client.post(f"/api/jobs/{identifier}/stop").status_code == 202
        assert cleanup.wait(2)
        assert store.get(identifier)["status"] == "stopping"
        assert client.post(f"/api/jobs/{identifier}/continue").status_code == 409
        assert client.post(f"/api/jobs/{identifier}/stop").status_code == 202
        assert not store.event(identifier, "late", 100, "must not complete", status="completed")
        assert not store.event(identifier, "late", 85, "must not restart")
        assert store.get(identifier)["status"] == "stopping"
    finally:
        release.set()
    assert wait(client, identifier)["status"] == "stopped"
    assert completed_cleanup.is_set()


def test_finished_tasks_cannot_be_stopped_and_auth_is_required(client, monkeypatch):
    job = store.create("done", "zip", "", "preview")
    store.event(job["id"], "完成", 100, "done", status="completed")
    assert client.post(f"/api/jobs/{job['id']}/stop").status_code == 409
    monkeypatch.setattr(config, "ACCESS_TOKEN", "secret")
    assert client.post(f"/api/jobs/{job['id']}/stop").status_code == 401


def test_stop_local_source_remains_unavailable_in_production(client, monkeypatch):
    monkeypatch.setenv("WIKI_ENV", "production")
    job = store.create("local", "local", "/tmp", "agent")
    assert client.post(f"/api/jobs/{job['id']}/stop").status_code == 404
    assert store.get(job["id"])["status"] == "queued"


def test_checkpoint_writer_cannot_overwrite_after_close(client):
    job = store.create("snapshot", "zip", "", "agent")
    writer = checkpoints.writer(job["id"])
    writer({"current": 1})
    writer.close()
    writer({"current": 0})
    assert checkpoints.load(job["id"], "agent-state") == {"current": 1}


def test_import_and_scan_check_stop_signal(tmp_path):
    counter = 0
    def check():
        nonlocal counter
        counter += 1
        if counter >= 3:
            raise execution.TaskStopped()
    archive = tmp_path / "input.zip"
    archive.write_bytes(zip_bytes({"a.py": SOURCE, "b.py": SOURCE}))
    with pytest.raises(execution.TaskStopped):
        sources.unpack(archive, tmp_path / "source", check=check)
    counter = 0
    repository = tmp_path / "repo"
    repository.mkdir()
    for index in range(10):
        (repository / f"a{index}.py").write_text(SOURCE)
    with pytest.raises(execution.TaskStopped):
        build_index(repository, lambda message: None, check=check)


def test_git_stop_terminates_clone_process(tmp_path, monkeypatch):
    killed = []
    class Process:
        def poll(self):
            return None
        def kill(self):
            killed.append("kill")
        def wait(self):
            killed.append("wait")
    monkeypatch.setattr(sources, "validate_git_url", lambda url: url)
    monkeypatch.setattr(sources.subprocess, "Popen", lambda *args, **kwargs: Process())
    count = 0
    def check():
        nonlocal count
        count += 1
        if count == 2:
            raise execution.TaskStopped()
    with pytest.raises(execution.TaskStopped):
        sources.clone("https://github.com/a/b", tmp_path / "clone", lambda message: None, check=check)
    assert killed == ["kill", "wait"]


def test_restart_finishes_persisted_stop_request(client):
    job = store.create("interrupted-stop", "zip", "", "agent")
    assert store.request_stop(job["id"])
    assert store.get(job["id"])["status"] == "stopping"
    store.initialize()
    assert store.get(job["id"])["status"] == "stopped"


def test_completion_cannot_override_an_accepted_stop(client):
    job = store.create("race", "zip", "", "agent")
    store.event(job["id"], "Agent 分析", 80, "working")
    assert store.request_stop(job["id"])
    assert not store.event(job["id"], "完成", 100, "late completion", status="completed")
    assert not store.event(job["id"], "失败", 80, "late error", status="failed", error="late")
    assert store.get(job["id"])["status"] == "stopping"
    assert store.get(job["id"])["progress"] == 80
