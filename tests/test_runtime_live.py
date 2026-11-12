"""原生实时事件、断线补齐、脱敏及可取消超时重试。"""
import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import pytest
from app.runtime.actions import Actions
from app.runtime.stream import EventStream
from app.runtime.client import Runtime
from app.runtime.retries import ModelTimeout
from app import store, config


def runtime(tmp_path):
    actions = []
    def log(message):
        pass
    log.action = actions.append
    return SimpleNamespace(workspace=tmp_path, session_id="ses_test", log=log), actions


def tool(tmp_path, status="running"):
    return {"id": "tool-1", "sessionID": "ses_test", "type": "tool", "tool": "read", "state": {
        "status": status, "input": {"filePath": str(tmp_path / "repo/main.py"), "offset": 1, "limit": 20},
        "output": "SECRET SOURCE", "time": {"start": 1000, "end": 1200} if status == "completed" else {"start": 1000}}}


def test_actions_filter_session_private_content_and_duplicate_updates(tmp_path, monkeypatch):
    rt, published = runtime(tmp_path)
    bridge = Actions(rt)
    monkeypatch.setenv("WIKI_LLM_API_KEY", "private-key")
    event = {"type": "message.part.updated", "properties": {"sessionID": "other", "part": tool(tmp_path)}}
    bridge.event(event)
    assert not published
    event["properties"]["sessionID"] = rt.session_id
    bridge.event(event)
    event["properties"]["part"] = tool(tmp_path, "completed")
    bridge.event(event)
    bridge.event(event)
    bridge.part(tool(tmp_path, "running"))
    bridge.event({"type": "message.part.updated", "properties": {"sessionID": rt.session_id,
                 "part": {"id": "secret", "type": "reasoning", "text": "PRIVATE THINKING"}}})
    assert [item["status"] for item in published] == ["running", "completed"]
    assert published[0]["detail"].startswith("main.py")
    assert published[-1]["durationMs"] == 200
    serialized = json.dumps(published)
    assert "SECRET SOURCE" not in serialized and "PRIVATE THINKING" not in serialized and str(tmp_path) not in serialized
    bridge.publish("masked", "retry", "waiting", "private-key")
    assert "private-key" not in json.dumps(published)


def test_model_start_finish_share_identity_without_reverting_on_snapshot(tmp_path):
    rt, published = runtime(tmp_path)
    bridge = Actions(rt)
    start = {"id": "start", "messageID": "message-1", "type": "step-start"}
    finish = {"id": "finish", "messageID": "message-1", "type": "step-finish"}
    bridge.snapshot([{"parts": [start, finish]}])
    bridge.snapshot([{"parts": [start, finish]}])
    assert len(published) == 2
    assert published[0]["id"] == published[1]["id"]


class Bytes(httpx.AsyncByteStream):
    def __init__(self, data, hold=False):
        self.data, self.hold = data, hold
    async def __aiter__(self):
        yield self.data[:17]
        yield self.data[17:]
        if self.hold:
            await asyncio.Event().wait()


def test_stream_reconnects_and_reconciles_missing_tool_completion(tmp_path):
    async def exercise():
        rt, published = runtime(tmp_path)
        attempts = 0
        snapshots = []
        async def handler(request):
            nonlocal attempts
            attempts += 1
            part = tool(tmp_path, "running" if attempts == 1 else "completed")
            event = {"type": "message.part.updated", "properties": {"sessionID": rt.session_id, "part": part}}
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                stream=Bytes(("data: " + json.dumps(event) + "\n\n").encode(), hold=attempts > 1))
        rt.messages = AsyncMock(side_effect=lambda: [{"parts": [tool(tmp_path, "completed")]}] if attempts > 1 else [])
        async with httpx.AsyncClient(base_url="http://test.invalid", transport=httpx.MockTransport(handler)) as client:
            rt.client = client
            stream = EventStream(rt, snapshots.append)
            await stream.start()
            for _ in range(100):
                if any(item["status"] == "completed" for item in published):
                    break
                await asyncio.sleep(.01)
            await stream.close()
        assert attempts == 2
        assert any(item["status"] == "reconnecting" for item in published)
        assert [item["status"] for item in published if item["kind"] == "tool"] == ["running", "completed"]
        assert rt.messages.await_count == 2 and snapshots
    asyncio.run(exercise())


@pytest.fixture
def retry_runtime(tmp_path, monkeypatch):
    import app.runtime.client as module
    rt = Runtime(tmp_path, tmp_path, lambda message: None)
    actions = []
    rt.log.action = actions.append
    class Stream:
        def __init__(self, runtime, observe):
            self.actions = Actions(runtime)
            self.task = True
        async def start(self):
            pass
        async def close(self):
            rt.closed = True
    monkeypatch.setattr(module, "EventStream", Stream)
    monkeypatch.setattr(module, "RETRY_DELAYS", (0, 0))
    return rt, actions


def test_timeout_retries_keep_session_and_do_not_repeat_full_prompt(retry_runtime):
    rt, actions = retry_runtime
    rt.session_id = "ses_same"
    rt.request = AsyncMock(side_effect=[ModelTimeout("The operation timed out."), httpx.ReadTimeout("timeout"), "report"])
    assert asyncio.run(rt.prompt("original large schema", lambda value: None)) == "report"
    assert rt.request.await_count == 3 and rt.session_id == "ses_same" and rt.closed
    assert rt.request.await_args_list[0].args[0] == "original large schema"
    assert all("original large schema" not in call.args[0] for call in rt.request.await_args_list[1:])
    assert len([item for item in actions if item["status"] == "waiting"]) == 2


def test_timeout_exhaustion_is_bounded_but_authentication_is_not_retried(retry_runtime):
    rt, actions = retry_runtime
    rt.request = AsyncMock(side_effect=ModelTimeout("timeout"))
    with pytest.raises(ModelTimeout, match="自动重试 2 次"):
        asyncio.run(rt.prompt("analyze", lambda value: None))
    assert rt.request.await_count == 3 and actions[-1]["status"] == "failed"
    rt.request = AsyncMock(side_effect=RuntimeError("401 invalid key"))
    with pytest.raises(RuntimeError, match="401"):
        asyncio.run(rt.prompt("analyze", lambda value: None))
    assert rt.request.await_count == 1


def test_stopping_cancels_pending_retry_delay(retry_runtime, monkeypatch):
    import app.runtime.client as module
    rt, actions = retry_runtime
    monkeypatch.setattr(module, "RETRY_DELAYS", (30, 30))
    rt.request = AsyncMock(side_effect=ModelTimeout("timeout"))
    async def stop():
        task = asyncio.create_task(rt.prompt("analyze", lambda value: None))
        for _ in range(100):
            if any(item["status"] == "waiting" for item in actions):
                break
            await asyncio.sleep(.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(stop())
    assert rt.request.await_count == 1 and rt.closed


def test_action_history_migrates_old_database_and_replays_with_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    with sqlite3.connect(tmp_path / "wiki.db") as db:
        db.execute("CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT,at TEXT,stage TEXT,progress INTEGER,message TEXT)")
    store.initialize()
    job = store.create("test", "zip", "upload.zip", "agent")
    payload = {"id": "tool-1", "kind": "tool", "status": "running", "title": "读取源码"}
    assert store.event(job["id"], "Agent 分析", 40, "读取源码", action=payload)
    event = store.events(job["id"])[0]
    assert event["action"] == payload and store.events(job["id"], event["seq"]) == []
    store.request_stop(job["id"])
    assert not store.event(job["id"], "Agent 分析", 40, "迟到动作", action=payload)


def test_browser_sse_action_events_share_progress_cursor(client):
    job = store.create("test", "zip", "upload.zip", "agent")
    store.event(job["id"], "Agent 分析", 40, "读取", action={"id": "r", "status": "completed", "title": "读取源码"})
    cursor = store.events(job["id"])[0]["seq"]
    store.event(job["id"], "分析完成", 100, "完成", status="completed")
    response = client.get(f"/api/jobs/{job['id']}/events").text
    assert "event: action" in response and "event: progress" in response and "event: done" in response
    resumed = client.get(f"/api/jobs/{job['id']}/events", headers={"Last-Event-ID": str(cursor)}).text
    assert "event: action" not in resumed and "event: done" in resumed


def test_compaction_start_finish_updates_one_public_action(tmp_path):
    rt, published = runtime(tmp_path)
    bridge = Actions(rt)
    bridge.event({"type": "message.part.updated", "properties": {"sessionID": rt.session_id,
                 "part": {"id": "compress-1", "type": "compaction", "sessionID": rt.session_id}}})
    bridge.event({"id": "finish", "type": "session.compacted", "properties": {"sessionID": rt.session_id}})
    assert [item["status"] for item in published] == ["running", "completed"]
    assert published[0]["id"] == published[1]["id"] == "compress-1"


def test_native_busy_status_is_visible_before_first_model_token(tmp_path):
    rt, published = runtime(tmp_path)
    bridge = Actions(rt)
    for status in ("busy", "idle"):
        bridge.event({"type": "session.status", "properties": {"sessionID": rt.session_id, "status": {"type": status}}})
    assert [item["status"] for item in published] == ["running", "completed"]
    assert published[0]["id"] == published[1]["id"] == "session-work"


def test_model_error_finishes_running_action_without_forwarding_error_body(tmp_path):
    rt, published = runtime(tmp_path)
    bridge = Actions(rt)
    bridge.part({"id": "start", "messageID": "m", "type": "step-start"})
    bridge.event({"type": "session.error", "properties": {"sessionID": rt.session_id, "error": {"message": "SECRET ERROR BODY"}}})
    assert published[0]["id"] == published[1]["id"]
    assert [item["status"] for item in published] == ["running", "failed"]
    assert "SECRET ERROR BODY" not in json.dumps(published)


def test_response_flow_counts_are_visible_without_exposing_reasoning(tmp_path):
    rt, published = runtime(tmp_path)
    bridge = Actions(rt)
    bridge.event({"type": "message.updated", "properties": {"info": {"id": "m", "sessionID": rt.session_id, "role": "assistant"}}})
    private = "PRIVATE REASONING MUST NOT BE SHOWN"
    bridge.event({"type": "message.part.updated", "properties": {"sessionID": rt.session_id,
                 "part": {"id": "thought", "messageID": "m", "type": "reasoning", "text": private}}})
    assert any(item["kind"] == "generation" and "0 次" in item["detail"] for item in published)
    assert str(len(private)) in bridge.generation.summary()
    assert private not in json.dumps(published) and private not in repr(vars(bridge.generation))
    bridge.event({"type": "message.part.delta", "properties": {"sessionID": rt.session_id, "partID": "thought", "field": "text", "delta": "MORE"}})
    assert sum(bridge.generation.lengths.values()) == len(private) + 4
    bridge.event({"type": "message.part.updated", "properties": {"sessionID": rt.session_id,
                 "part": {"id": "thought", "messageID": "m", "type": "reasoning", "text": private + "MORE"}}})
    assert sum(bridge.generation.lengths.values()) == len(private) + 4


def test_prompt_text_is_not_counted_as_model_response(tmp_path):
    rt, published = runtime(tmp_path)
    bridge = Actions(rt)
    bridge.event({"type": "message.updated", "properties": {"info": {"id": "u", "sessionID": rt.session_id, "role": "user"}}})
    bridge.event({"type": "message.part.updated", "properties": {"sessionID": rt.session_id,
                 "part": {"id": "prompt", "messageID": "u", "type": "text", "text": "private prompt"}}})
    assert not bridge.generation.lengths and not published


def test_tool_details_show_real_results_plan_items_and_failure_reason(tmp_path):
    rt, published = runtime(tmp_path)
    bridge = Actions(rt)
    read = tool(tmp_path, "completed")
    read["state"]["metadata"] = {"display": {"lineStart": 10, "lineEnd": 20, "totalLines": 100}}
    bridge.part(read)
    assert "返回第 10–20 行" in published[-1]["detail"]
    bridge.part({"id": "glob", "type": "tool", "tool": "glob", "state": {"status": "completed", "input": {"pattern": "repo/**/*"},
                "output": str(tmp_path / "repo/README.md") + "\n" + str(tmp_path / "repo/app.py"), "metadata": {}}})
    assert "返回 2 个路径" in published[-1]["detail"] and "README.md" in published[-1]["detail"]
    bridge.part({"id": "plan", "type": "tool", "tool": "todowrite", "state": {"status": "completed", "input": {
                "todos": [{"content": "阅读 README", "status": "completed"}, {"content": "追踪导入流程", "status": "in_progress"}]}}})
    assert "阅读 README" in published[-1]["detail"] and "进行中：追踪导入流程" in published[-1]["detail"]
    failed = tool(tmp_path, "error")
    failed["id"] = "failed"
    failed["state"]["error"] = "The user has specified a rule which prevents you from using this specific tool call. private policy data"
    bridge.part(failed)
    assert "权限拒绝" in published[-1]["detail"] and "private policy" not in published[-1]["detail"]


def test_only_public_assistant_narration_is_displayed_not_json_report(tmp_path):
    rt, published = runtime(tmp_path)
    bridge = Actions(rt)
    bridge.event({"type": "message.updated", "properties": {"info": {"id": "m", "sessionID": rt.session_id, "role": "assistant"}}})
    for identity, text in [("narration", "先读取 README 与项目入口，然后追踪导入、任务执行和结果生成的主要链路。"), ("report", '{"overview":"hidden report"}')]:
        bridge.event({"type": "message.part.updated", "properties": {"sessionID": rt.session_id,
                     "part": {"id": identity, "messageID": "m", "type": "text", "text": text, "time": {"end": 1}}}})
    messages = [item for item in published if item["kind"] == "message"]
    assert len(messages) == 1 and "README" in messages[0]["detail"]
