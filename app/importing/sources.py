"""只读导入，禁止执行仓库脚本、检出子模块或跟随符号链接。"""
import ipaddress
import os
import shutil
import socket
import stat
import subprocess
import time
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from .. import config

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build", "target", ".idea", ".data"}


def validate_git_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.port not in (None, 443)):
        raise ValueError("请输入不含凭据和查询参数的 HTTPS Git 地址")
    if parsed.hostname not in config.GIT_HOSTS:
        raise ValueError("Git 主机不在管理员允许列表中")
    if len(parsed.path.strip("/").split("/")) < 2:
        raise ValueError("Git 地址应包含组织和仓库名")
    for info in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM):
        address = ipaddress.ip_address(info[4][0])
        proxy_address = config.ALLOW_FAKE_IP and address in ipaddress.ip_network("198.18.0.0/15")
        if not address.is_global and not proxy_address:
            raise ValueError("不允许连接本机或私有网络地址")
    return url


def clone(url, destination, log, check=lambda: None):
    check()
    validate_git_url(url)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_ALLOW_PROTOCOL": "https"}
    logfile = destination.parent / "git.log"
    command = ["git", "-c", "http.followRedirects=false", "-c", "core.hooksPath=/dev/null",
               "-c", "credential.helper=", "clone", "--depth", "1", "--single-branch", "--", url, str(destination)]
    with logfile.open("wb") as output:
        process = subprocess.Popen(command, stdout=output, stderr=output, env=env)
        started = time.monotonic()
        try:
            while process.poll() is None:
                check()
                if time.monotonic() - started > 180:
                    raise ValueError("Git 获取超过 180 秒，请使用 ZIP 或缩小仓库")
                size = sum(p.stat().st_size for p in destination.rglob("*") if p.is_file() and not p.is_symlink()) if destination.exists() else 0
                if size > config.MAX_EXPANDED:
                    raise ValueError("仓库超过 150 MiB 导入限制")
                time.sleep(0.3)
        except BaseException:
            process.kill()
            process.wait()
            raise
    check()
    if process.returncode:
        raise ValueError("Git 获取失败，请检查公开仓库地址和网络；私有仓库请上传 ZIP")
    revision = subprocess.check_output(["git", "-C", str(destination), "rev-parse", "HEAD"], env=env).decode().strip()
    shutil.rmtree(destination / ".git")
    log("源码获取完成，已固定 Git 提交 " + revision[:12])
    return revision


def unpack(archive, destination, check=lambda: None):
    check()
    total = 0
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if len(entries) > 20000:
            raise ValueError("ZIP 条目过多（最多 20000）")
        for entry in entries:
            check()
            path = PurePosixPath(entry.filename)
            if (path.is_absolute() or ".." in path.parts or "\\" in entry.filename
                    or any(":" in part for part in path.parts) or "\x00" in entry.filename):
                raise ValueError("ZIP 包含不安全路径")
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError("ZIP 不允许符号链接")
            total += entry.file_size
            if total > config.MAX_EXPANDED:
                raise ValueError("ZIP 解压内容超过 150 MiB")
            if entry.is_dir() or any(part in SKIP_DIRS for part in path.parts):
                continue
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(entry) as source, target.open("wb") as output:
                while block := source.read(1024 * 1024):
                    check()
                    output.write(block)
    children = list(destination.iterdir()) if destination.exists() else []
    if (len(children) == 1 and children[0].is_dir()
            and children[0].name not in {"src", "app", "lib", "packages", "services", "apps", "modules", "tests", "docs"}):
        return children[0]
    return destination
