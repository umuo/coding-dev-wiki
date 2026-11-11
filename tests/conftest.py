import io
import zipfile
import pytest
from fastapi.testclient import TestClient
from app import config, store
from app.api import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "ACCESS_TOKEN", "")
    with TestClient(app) as value:
        yield value


def zip_bytes(files):
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as bundle:
        for name, text in files.items():
            bundle.writestr(name, text)
    return content.getvalue()


@pytest.fixture
def sample_zip():
    return zip_bytes({"sample/README.md": "# Todo service\nCreate and list tasks.\n",
                      "sample/api.py": "from service import create_task\n\ndef submit(title):\n    return create_task(title)\n",
                      "sample/service.py": "tasks = []\n\ndef create_task(title):\n    tasks.append(title)\n    return tasks\n",
                      "sample/web/main.ts": "export function submit(title: string) {\n    return fetch('/tasks', {method: 'POST', body: title});\n}\n",
                      "sample/.env": "SECRET=excluded", "sample/node_modules/a.js": "excluded"})
