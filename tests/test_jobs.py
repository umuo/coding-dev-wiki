import time
from app import config, store
from .conftest import zip_bytes


def wait(client, identifier):
    for _ in range(150):
        job = client.get(f"/api/jobs/{identifier}").json()
        if job["status"] in {"completed", "failed", "stopped"}:
            return job
        time.sleep(0.03)
    raise AssertionError("task did not finish")


def upload(client, content, mode="preview"):
    response = client.post("/api/jobs/zip", files={"file": ("sample.zip", content, "application/zip")}, data={"mode": mode})
    assert response.status_code == 202, response.text
    return response.json()["id"]


def test_zip_progress_result_retry_and_snapshot(client, sample_zip):
    identifier = upload(client, sample_zip)
    assert wait(client, identifier)["status"] == "completed"
    response = client.get(f"/api/jobs/{identifier}/result")
    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "preview"
    assert "api.py" in payload["files"]
    assert ".env" not in payload["files"]
    assert payload["featureGroups"][0]["features"][0]["steps"]
    events = client.get(f"/api/jobs/{identifier}/events").text
    assert "event: done" in events and "100" in events
    cursor = store.events(identifier)[-1]["seq"]
    replay = client.get(f"/api/jobs/{identifier}/events", headers={"Last-Event-ID": str(cursor)}).text
    assert "event: progress" not in replay and "event: done" in replay
    retry = client.post(f"/api/jobs/{identifier}/retry").json()["id"]
    assert retry != identifier
    assert wait(client, retry)["status"] == "completed"
    assert client.get(f"/api/jobs/{retry}/result").json()["snapshot"] == payload["snapshot"]
    assert client.get(f"/projects/{identifier}").status_code == 200
    assert client.get('/assets/viewer.js').status_code == 200


def test_other_repo_and_failure_recovery(client):
    identifier = upload(client, zip_bytes({"src/main.java": "class Main { public static void main(String[] args) {} }"}))
    assert wait(client, identifier)["status"] == "completed"
    assert "src/main.java" in client.get(f"/api/jobs/{identifier}/result").json()["files"]
    bad = upload(client, zip_bytes({"../escape.py": "bad"}))
    assert wait(client, bad)["status"] == "failed"
    assert client.get(f"/api/jobs/{bad}/result").status_code == 409
    assert client.get(f"/api/jobs/{bad}/events").status_code == 200
    job = store.create("interrupted", "git", "", "preview")
    store.initialize()
    assert store.get(job["id"])["status"] == "failed"


def test_no_model_no_silent_fallback(client, sample_zip, monkeypatch):
    monkeypatch.delenv("WIKI_LLM_API_KEY", raising=False)
    monkeypatch.delenv("WIKI_LLM_MODEL", raising=False)
    response = client.post('/api/jobs/zip', files={'file': ('sample.zip', sample_zip)}, data={'mode': 'agent'})
    assert response.status_code == 409
    assert client.post('/api/jobs/git', json={'url': 'file:///tmp/repo', 'mode': 'preview'}).status_code == 400
    assert client.get('/api/jobs/missing').status_code == 404


def test_auth_blocks_all_project_access(client, monkeypatch, sample_zip):
    identifier = upload(client, sample_zip)
    wait(client, identifier)
    monkeypatch.setattr(config, "ACCESS_TOKEN", "test-token")
    assert client.get('/api/jobs').status_code == 401
    assert client.get(f'/api/jobs/{identifier}/result').status_code == 401
    assert client.get(f'/api/jobs/{identifier}/events').status_code == 401
    assert client.post('/api/login', json={'token': 'wrong'}).status_code == 401
    assert client.post('/api/login', json={'token': 'test-token'}).status_code == 200
    assert client.get('/api/jobs').status_code == 200
