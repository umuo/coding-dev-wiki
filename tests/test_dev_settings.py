import sys
from app import dev


def test_developer_launcher_loads_env_before_server_import(tmp_path, monkeypatch):
    settings = tmp_path / ".env"
    settings.write_text('WIKI_LLM_MODEL="test-local-model"\nWIKI_LLM_API_KEY=test-key\nWIKI_ENV=production\nWIKI_DEV_MODE=0\n')
    monkeypatch.setattr(dev, "ENV_FILE", settings)
    monkeypatch.setattr(sys, "argv", ["dev"])
    for name in ("WIKI_LLM_MODEL", "WIKI_LLM_API_KEY", "WIKI_ENV", "WIKI_DEV_MODE"):
        monkeypatch.delenv(name, raising=False)
    captured = {}
    def launch(application, **kwargs):
        import os
        captured.update(application=application, model=os.getenv("WIKI_LLM_MODEL"),
                        key_ready=bool(os.getenv("WIKI_LLM_API_KEY")),
                        environment=os.getenv("WIKI_ENV"), enabled=os.getenv("WIKI_DEV_MODE"), **kwargs)
    monkeypatch.setattr(dev.uvicorn, "run", launch)
    dev.main()
    assert captured["model"] == "test-local-model"
    assert captured["key_ready"]
    assert captured["environment"] == "development" and captured["enabled"] == "1"
    assert captured["host"] == "127.0.0.1" and captured["proxy_headers"] is False


def test_explicit_environment_overrides_dotenv(tmp_path, monkeypatch):
    settings = tmp_path / ".env"
    settings.write_text("WIKI_LLM_MODEL=file-model\n")
    monkeypatch.setattr(dev, "ENV_FILE", settings)
    monkeypatch.setattr(sys, "argv", ["dev"])
    monkeypatch.setenv("WIKI_LLM_MODEL", "exported-model")
    for name in ("WIKI_ENV", "WIKI_DEV_MODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(dev.uvicorn, "run", lambda *args, **kwargs: None)
    dev.main()
    import os
    assert os.getenv("WIKI_LLM_MODEL") == "exported-model"
