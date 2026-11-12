"""真实工具正文的脱敏、按需读取和旧记录回查。"""
import json
import sqlite3
from app import store, config
from app.runtime.payloads import capture, preview, MAX_TEXT


def test_capture_preserves_source_response_but_redacts_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_LLM_API_KEY", "configured-api-key")
    monkeypatch.setattr(config, "ACCESS_TOKEN", "configured-access-token")
    part = {"id": "p", "callID": "call-1", "tool": "read", "state": {"status": "completed",
            "input": {"filePath": str(tmp_path / "repo/main.py"), "nested": {"Authorization": "Bearer secret", "password": "password"}},
            "output": "1: def run():\n2:     return 42\nconfigured-api-key\nconfigured-access-token"}}
    payload = capture(part, tmp_path)
    assert payload["parameters"]["filePath"] == "repo/main.py"
    assert payload["parameters"]["nested"] == {"Authorization": "[已隐藏]", "password": "[已隐藏]"}
    assert "def run()" in payload["response"] and "configured-" not in payload["response"]
    assert payload["callId"] == "call-1"


def test_capture_marks_overlong_output_and_sse_keeps_only_summary(tmp_path):
    payload = capture({"tool": "read", "state": {"input": {}, "output": "x" * (MAX_TEXT + 1)}}, tmp_path)
    assert payload["truncated"] and len(payload["response"]) == MAX_TEXT
    event = {"seq": 1, "action": {"kind": "tool", "id": "p", "payload": payload, "title": "read"}}
    small = preview(event)
    assert "payload" not in small["action"] and "payload" in event["action"]
    assert len(json.dumps(small)) < 200


def test_tool_payload_endpoint_is_job_scoped_and_excluded_from_stream(client):
    first = store.create("one", "zip", "upload.zip", "agent")
    second = store.create("two", "zip", "upload.zip", "agent")
    payload = {"tool": "read", "parameters": {"filePath": "repo/main.py"}, "response": "UNIQUE_RAW_RESPONSE", "error": None}
    store.event(first["id"], "Agent 分析", 40, "read", action={"kind": "tool", "id": "p", "payload": payload})
    seq = store.events(first["id"])[0]["seq"]
    response = client.get(f"/api/jobs/{first['id']}/events/{seq}/tool")
    assert response.status_code == 200 and response.json() == payload
    assert response.headers["cache-control"] == "no-store"
    assert client.get(f"/api/jobs/{second['id']}/events/{seq}/tool").status_code == 404
    store.event(first["id"], "完成", 100, "done", status="completed")
    assert "UNIQUE_RAW_RESPONSE" not in client.get(f"/api/jobs/{first['id']}/events").text


def test_legacy_tool_payload_is_read_from_native_db_without_running_agent(client):
    job = store.create("old", "zip", "upload.zip", "agent")
    store.event(job["id"], "Agent 分析", 40, "read", action={"kind": "tool", "id": "prt_old", "tool": "read"})
    seq = store.events(job["id"])[0]["seq"]
    directory = store.directory(job["id"]) / "opencode/data/opencode"
    directory.mkdir(parents=True)
    with sqlite3.connect(directory / "opencode.db") as db:
        db.execute("CREATE TABLE part(id TEXT PRIMARY KEY,data TEXT)")
        db.execute("INSERT INTO part VALUES(?,?)", ("prt_old", json.dumps({"type": "tool", "tool": "read", "callID": "old-call",
            "state": {"status": "error", "input": {"filePath": "repo/missing.py"}, "error": "File not found"}})))
    response = client.get(f"/api/jobs/{job['id']}/events/{seq}/tool")
    assert response.status_code == 200
    assert response.json()["callId"] == "old-call" and response.json()["error"] == "File not found"
    assert store.get(job["id"])["status"] == "running"


def test_tool_payload_endpoint_requires_existing_access_token(client, monkeypatch):
    job = store.create("auth", "zip", "upload.zip", "agent")
    monkeypatch.setattr(config, "ACCESS_TOKEN", "private-token")
    response = client.get(f"/api/jobs/{job['id']}/events/1/tool")
    assert response.status_code in {401, 403}
