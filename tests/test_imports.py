import socket
import stat
import zipfile
import pytest
from app.importing.sources import unpack, validate_git_url
from app.analysis.index import extract_symbols, build_index
from app.analysis.schemas import Evidence, validate_evidence
from .conftest import zip_bytes


def test_zip_safe_root_and_ignored_content(tmp_path):
    path = tmp_path / "project.zip"
    path.write_bytes(zip_bytes({"repo/app.py": "def run():\n    return 42\n", "repo/.env": "SECRET=1", "repo/node_modules/a.js": "bad"}))
    root = unpack(path, tmp_path / "source")
    files, digest, skipped = build_index(root, lambda message: None)
    assert list(files) == ["app.py"]
    assert len(digest) == 64
    assert skipped == 1


@pytest.mark.parametrize("name", ["../outside.py", "/tmp/outside.py", "repo/../../outside.py", "repo\\outside.py", "C:/outside.py"])
def test_zip_rejects_paths(tmp_path, name):
    archive = tmp_path / "bad.zip"
    archive.write_bytes(zip_bytes({name: "no"}))
    with pytest.raises(ValueError):
        unpack(archive, tmp_path / "source")
    assert not (tmp_path / "outside.py").exists()


def test_zip_rejects_symlinks_and_bombs(tmp_path, monkeypatch):
    archive = tmp_path / "link.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        info = zipfile.ZipInfo("link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        bundle.writestr(info, "../secret")
    with pytest.raises(ValueError, match="符号链接"):
        unpack(archive, tmp_path / "source")
    from app import config
    monkeypatch.setattr(config, "MAX_EXPANDED", 8)
    archive.write_bytes(zip_bytes({"large.py": "x" * 9}))
    with pytest.raises(ValueError, match="150 MiB"):
        unpack(archive, tmp_path / "source")


@pytest.mark.parametrize("url", ["file:///tmp/repo", "ssh://github.com/a/b", "https://user:pass@github.com/a/b", "https://localhost/a/b", "https://github.com/a/b?token=x"])
def test_git_url_restrictions(url):
    with pytest.raises(ValueError):
        validate_git_url(url)


def test_git_blocks_private_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("127.0.0.1", 443))])
    with pytest.raises(ValueError, match="私有"):
        validate_git_url("https://github.com/a/b")


@pytest.mark.parametrize("language,code,name,end", [
    ("python", "class A:\n    def run(self):\n        return 1\n", "A.run", 3),
    ("javascript", "export const run = () => {\n return 1;\n};", "run", 3),
    ("typescript", "export function run(x: string) {\n return x;\n}", "run", 3),
    ("tsx", "export function Run() {\n return <div>ok</div>;\n}", "Run", 3),
    ("java", "class Demo {\n public int run() {\n return 1;\n }\n}", "run", 4),
])
def test_symbol_ranges(language, code, name, end):
    symbol = next(item for item in extract_symbols(code, language) if item["name"] == name)
    assert symbol["end_line"] == end


def test_evidence_rejects_fabrication():
    files = {"a.py": {"lines": 5}}
    evidence = Evidence(path="a.py", line=1, end_line=4, role="entry", note="", symbol="")
    with pytest.raises(ValueError, match="未通过"):
        validate_evidence([evidence], files, {"a.py": [(1, 2)]})
    validate_evidence([evidence], files, {"a.py": [(1, 5)]})
    with pytest.raises(ValueError, match="无效引用"):
        validate_evidence([evidence.model_copy(update={"end_line": 6})], files)
