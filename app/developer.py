"""开发专属入口；生产模式不导入，生产镜像不包含本文件。"""
import ipaddress
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import Literal
from . import config, jobs, store


def is_local_request(request: Request):
    if not config.developer_enabled() or request.client is None:
        return False
    try:
        local_peer = ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False
    if not local_peer or request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return False
    if any(name in request.headers for name in ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip")):
        return False
    if request.headers.get("sec-fetch-site") == "cross-site":
        return False
    origin = request.headers.get("origin")
    if origin:
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return False
        if (parsed.scheme, parsed.netloc) != (request.url.scheme, request.url.netloc):
            return False
    return True


def require_local_request(request: Request):
    if not is_local_request(request):
        raise HTTPException(404, "入口不存在")


def local_directory(value):
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("请输入项目目录的绝对路径")
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_dir() or resolved == Path(resolved.anchor):
            raise ValueError("请选择有效的项目目录，不能选择文件或文件系统根目录")
        # 只检查目录可访问性，源码过滤、大小限制和符号索引仍使用共用管线。
        next(resolved.iterdir(), None)
    except OSError as exc:
        raise ValueError("项目目录不存在或无法读取") from exc
    return resolved


class LocalRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    mode: Literal["agent", "preview"] = "agent"


def install(application, access, require_mode):
    if not config.developer_enabled():
        return

    @application.post("/api/developer/jobs/local", dependencies=[Depends(access), Depends(require_local_request)], status_code=202)
    def create_local(body: LocalRequest):
        require_mode(body.mode)
        try:
            path = local_directory(body.path)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        job = store.create(path.name, "local", str(path), body.mode)
        jobs.submit(job["id"])
        return job

    @application.get("/developer/import.js", dependencies=[Depends(require_local_request)], include_in_schema=False)
    def developer_script():
        return FileResponse(config.WEB_DIR / "developer" / "import.js", media_type="application/javascript", headers={"Cache-Control": "no-store"})
