import os
import subprocess
import sys
from pathlib import Path
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import config, store
from app.api import app, access, require_mode, lifespan
from app.developer import install
from .test_jobs import wait


@pytest.fixture
def developer(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_ENV", "development")
    monkeypatch.setenv("WIKI_DEV_MODE", "1")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "ACCESS_TOKEN", "")
    application = FastAPI(lifespan=lifespan)
    application.router.routes.extend(app.router.routes)
    install(application, access, require_mode)
    with TestClient(application, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        yield client


def test_development_import_snapshot_retry_and_original_unchanged(developer, tmp_path):
    repository = tmp_path / "project"
    repository.mkdir()
    source = repository / "main.py"
    source.write_text("def run():\n    return 1\n")
    initial = source.read_bytes()
    response = developer.post("/api/developer/jobs/local", json={"path": str(repository), "mode": "preview"})
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]
    assert wait(developer, identifier)["status"] == "completed"
    before = developer.get(f"/api/jobs/{identifier}/result").json()
    assert before["files"]["main.py"]["content"] == initial.decode()
    assert source.read_bytes() == initial
    source.write_text("def run():\n    return 2\n")
    retry = developer.post(f"/api/jobs/{identifier}/retry")
    assert retry.status_code == 202
    assert wait(developer, retry.json()["id"])["status"] == "completed"
    after = developer.get(f"/api/jobs/{retry.json()['id']}/result").json()
    assert before["snapshot"] != after["snapshot"]
    assert developer.get(f"/api/jobs/{identifier}/result").json()["files"]["main.py"]["content"] == initial.decode()
    assert "developerImport" in developer.get("/").text
    assert developer.get("/developer/import.js").status_code == 200


@pytest.mark.parametrize("headers", [
    {"Host": "public.example"}, {"Origin": "https://evil.example"},
    {"X-Forwarded-For": "127.0.0.1"}, {"Forwarded": "for=127.0.0.1"},
    {"Sec-Fetch-Site": "cross-site"}, {"X-Real-IP": "127.0.0.1"},
])
def test_no_proxy_remote_host_or_cross_origin_entry(developer, headers):
    assert "developerImport" not in developer.get("/", headers=headers).text
    assert developer.get("/developer/import.js", headers=headers).status_code == 404
    assert developer.post("/api/developer/jobs/local", headers=headers, json={"path": "/tmp", "mode": "preview"}).status_code == 404


def test_non_loopback_peer_cannot_use_developer(developer):
    with TestClient(developer.app, base_url="http://localhost", client=("203.0.113.1", 50000)) as remote:
        assert "developerImport" not in remote.get("/").text
        assert remote.post("/api/developer/jobs/local", json={"path": "/tmp", "mode": "preview"}).status_code == 404


def test_invalid_directory_and_auth(developer, tmp_path, monkeypatch):
    file = tmp_path / "file.py"
    file.write_text("pass")
    for path in ("relative/path", str(tmp_path / "missing"), str(file), "/"):
        response = developer.post("/api/developer/jobs/local", json={"path": path, "mode": "preview"})
        assert response.status_code == 400, response.text
    monkeypatch.setattr(config, "ACCESS_TOKEN", "secret")
    assert developer.post("/api/developer/jobs/local", json={"path": str(tmp_path), "mode": "preview"}).status_code == 401


def test_production_blocks_old_local_retry(client, monkeypatch, tmp_path):
    monkeypatch.setenv("WIKI_ENV", "production")
    monkeypatch.setenv("WIKI_DEV_MODE", "1")
    job = store.create("old-local", "local", str(tmp_path), "preview")
    store.event(job["id"], "完成", 100, "done", status="completed")
    assert client.post(f"/api/jobs/{job['id']}/retry").status_code == 404


@pytest.mark.parametrize("environment,flag", [("production", "0"), ("production", "1"), ("development", "0")])
def test_production_has_no_registered_route_or_frontend(environment, flag, tmp_path):
    script = """
from fastapi.testclient import TestClient
from app.api import app
with TestClient(app, base_url='http://localhost', client=('127.0.0.1', 50000)) as client:
    assert '/api/developer/jobs/local' not in client.get('/openapi.json').text
    assert client.post('/api/developer/jobs/local', json={'path': '/tmp', 'mode': 'preview'}).status_code == 404
    assert client.get('/developer/import.js').status_code == 404
    assert 'developerImport' not in client.get('/').text
    assert '/developer/import.js' not in client.get('/').text
"""
    env = {**os.environ, "WIKI_ENV": environment, "WIKI_DEV_MODE": flag, "WIKI_DATA_DIR": str(tmp_path / "data")}
    subprocess.run([sys.executable, "-c", script], env=env, check=True, timeout=30)


def test_production_build_excludes_developer_code():
    root = Path(__file__).resolve().parents[1]
    excluded = (root / ".dockerignore").read_text().splitlines()
    assert {"app/dev.py", "app/developer.py", "web/developer"}.issubset(excluded)
    assert "WIKI_ENV=production WIKI_DEV_MODE=0" in (root / "Dockerfile").read_text()


def test_production_worker_cannot_read_local_source(client, tmp_path, monkeypatch):
    from app import jobs
    monkeypatch.setenv("WIKI_ENV", "production")
    monkeypatch.setenv("WIKI_DEV_MODE", "1")
    source = tmp_path / "main.py"
    source.write_text("def run():\n    return 1\n")
    job = store.create("local-blocked", "local", str(tmp_path), "preview")
    jobs.run(job["id"])
    assert store.get(job["id"])["status"] == "failed"
    assert source.read_text() == "def run():\n    return 1\n"
    assert not (store.directory(job["id"]) / "result.json").exists()
