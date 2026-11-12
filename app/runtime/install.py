"""显式安装固定版本官方二进制；不在分析期间自动下载或更新。"""
import argparse
import base64
import hashlib
import io
import json
import os
import platform
import tarfile
import urllib.request
from pathlib import Path
from .settings import VERSION
from .. import config


def install(destination):
    system = {"Darwin": "darwin", "Linux": "linux"}.get(platform.system())
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64"}.get(platform.machine())
    if not system or not arch:
        raise RuntimeError("当前平台请手动安装 OpenCode " + VERSION)
    suffix = "-baseline" if arch == "x64" else ""
    package = f"opencode-{system}-{arch}{suffix}"
    with urllib.request.urlopen(f"https://registry.npmjs.org/{package}/{VERSION}", timeout=30) as response:
        metadata = json.load(response)
    with urllib.request.urlopen(metadata["dist"]["tarball"], timeout=60) as response:
        data = response.read(200 * 1024 * 1024)
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
    if integrity != metadata["dist"]["integrity"]:
        raise RuntimeError("官方包完整性校验失败")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        member = archive.getmember("package/bin/opencode")
        if not member.isfile() or member.size > 250 * 1024 * 1024:
            raise RuntimeError("官方二进制包格式异常")
        executable = archive.extractfile(member).read()
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".download")
    temporary.write_bytes(executable)
    temporary.chmod(0o755)
    os.replace(temporary, destination)
    print(f"已安装 OpenCode {VERSION}: {destination}")


def main():
    parser = argparse.ArgumentParser(description="安装经过集成验证的 OpenCode 运行时")
    parser.add_argument("--destination", default=str(config.DATA_DIR / "bin" / "opencode"))
    install(parser.parse_args().destination)


if __name__ == "__main__":
    main()
