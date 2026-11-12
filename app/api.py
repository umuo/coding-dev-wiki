"""HTTP 路由、上传与可恢复的 SSE 事件接口。"""
import asyncio
import hmac
import json
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Literal
from . import config, jobs, store
from .analysis.agent import ask
from .importing.sources import validate_git_url


@asynccontextmanager
async def lifespan(application):
    config.model_budget()
    store.initialize()
    yield


app = FastAPI(title="Coding Dev Wiki", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=config.WEB_DIR / "assets"), name="assets")


def authorize(authorization: str):
    # EventSource cannot set Authorization. Cookie is set by the explicit login endpoint.
    if config.ACCESS_TOKEN and not hmac.compare_digest(authorization.removeprefix("Bearer "), config.ACCESS_TOKEN):
        raise HTTPException(401, "请输入访问令牌")


async def access(request: Request):
    token = request.headers.get("authorization", "").removeprefix("Bearer ") or request.cookies.get("wiki_token", "")
    if config.ACCESS_TOKEN and not hmac.compare_digest(token, config.ACCESS_TOKEN):
        raise HTTPException(401, "请输入访问令牌")


class Login(BaseModel):
    token: str


@app.post("/api/login")
def login(body: Login, request: Request):
    authorize("Bearer " + body.token)
    from fastapi.responses import JSONResponse
    response = JSONResponse({"ok": True})
    response.set_cookie("wiki_token", body.token, httponly=True, samesite="strict", secure=request.url.scheme == "https", max_age=86400)
    return response


@app.get("/api/health")
def health():
    from .runtime.settings import limits, binary
    try:
        binary()
        ready = True
    except RuntimeError:
        ready = False
    return {"ok": True, "modelReady": config.model_ready(), "authRequired": bool(config.ACCESS_TOKEN),
            "model": config.model_config()["model"] if config.model_ready() else "",
            "maxUploadMiB": config.MAX_UPLOAD // 1024 // 1024,
            "contextTokens": config.model_budget().context_tokens,
            "maxOutputTokens": limits()["output"], "analysisEngine": "opencode", "analysisRuntimeReady": ready}


def require_mode(mode):
    if mode == "agent" and not config.model_ready():
        raise HTTPException(409, "尚未配置 LLM。请管理员设置模型环境变量，或选择结构预览。")


def get_job(identifier):
    try:
        return store.get(identifier)
    except KeyError:
        raise HTTPException(404, "任务不存在") from None


def get_result(identifier):
    job = get_job(identifier)
    if job["status"] != "completed":
        raise HTTPException(409, "任务尚未完成")
    return store.result(identifier)


class GitRequest(BaseModel):
    url: str = Field(max_length=1000)
    mode: Literal["agent", "preview"] = "agent"


@app.post("/api/jobs/git", dependencies=[Depends(access)], status_code=202)
def create_git(body: GitRequest):
    require_mode(body.mode)
    try:
        validate_git_url(body.url)
    except (ValueError, OSError):
        raise HTTPException(400, "Git 地址无效、主机不在允许列表或无法解析；仅支持公开 HTTPS 仓库") from None
    name = body.url.rstrip("/").split("/")[-1].removesuffix(".git")
    job = store.create(name, "git", body.url, body.mode)
    jobs.submit(job["id"])
    return job


@app.post("/api/jobs/zip", dependencies=[Depends(access)], status_code=202)
async def create_zip(file: UploadFile = File(...), mode: Literal["agent", "preview"] = Form("agent")):
    require_mode(mode)
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(400, "请上传 ZIP 文件")
    job = store.create(Path(file.filename).stem[:120], "zip", "uploaded ZIP", mode)
    target = store.directory(job["id"]) / "upload.zip"
    size = 0
    try:
        with target.open("wb") as output:
            while block := await file.read(1024 * 1024):
                size += len(block)
                if size > config.MAX_UPLOAD:
                    raise HTTPException(413, "ZIP 上传大小不能超过 50 MiB")
                output.write(block)
    except Exception:
        target.unlink(missing_ok=True)
        store.event(job["id"], "上传失败", 0, "文件上传失败或超过限制", status="failed", error="请重新上传 ZIP")
        raise
    finally:
        await file.close()
    jobs.submit(job["id"])
    return job


@app.get("/api/jobs", dependencies=[Depends(access)])
def list_jobs():
    return store.listing()


@app.get("/api/jobs/{identifier}", dependencies=[Depends(access)])
def read_job(identifier: str):
    return get_job(identifier)


@app.get("/api/jobs/{identifier}/events", dependencies=[Depends(access)])
async def stream(identifier: str, request: Request, after: int = 0):
    get_job(identifier)
    async def generate():
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            cursor = after
        last_status = None
        while not await request.is_disconnected():
            live = get_job(identifier)
            if live["status"] != last_status:
                yield f"event: state\ndata: {json.dumps(live, ensure_ascii=False)}\n\n"
                last_status = live["status"]
            batch = store.events(identifier, cursor)
            for event in batch:
                cursor = event["seq"]
                kind = "action" if event.get("action") else "progress"
                yield f"id: {cursor}\nevent: {kind}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            job = get_job(identifier)
            if job["status"] in {"completed", "failed", "stopped"} and not store.events(identifier, cursor):
                yield f"event: done\ndata: {json.dumps(job, ensure_ascii=False)}\n\n"
                break
            if not batch:
                yield ": heartbeat\n\n"
            await asyncio.sleep(0.6)
    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/jobs/{identifier}/result", dependencies=[Depends(access)])
def result(identifier: str):
    return get_result(identifier)


@app.post("/api/jobs/{identifier}/retry", dependencies=[Depends(access)], status_code=202)
def retry(identifier: str, request: Request):
    previous = get_job(identifier)
    if previous["status"] not in {"failed", "completed", "stopped"}:
        raise HTTPException(409, "任务仍在处理中")
    if previous["kind"] == "local":
        if not config.developer_enabled():
            raise HTTPException(404, "任务来源在当前环境不可用")
        from .developer import require_local_request
        require_local_request(request)
    require_mode(previous["mode"])
    if previous["kind"] == "zip" and not (store.directory(identifier) / "upload.zip").exists():
        raise HTTPException(409, "上传未完成，请重新选择 ZIP 文件")
    job = store.create(previous["name"], previous["kind"], previous["source"], previous["mode"])
    if job["kind"] == "zip":
        shutil.copyfile(store.directory(identifier) / "upload.zip", store.directory(job["id"]) / "upload.zip")
    jobs.submit(job["id"])
    return job


@app.post("/api/jobs/{identifier}/continue", dependencies=[Depends(access)], status_code=202)
def continue_analysis(identifier: str, request: Request):
    previous = get_job(identifier)
    if previous["kind"] == "local":
        if not config.developer_enabled():
            raise HTTPException(404, "任务来源在当前环境不可用")
        from .developer import require_local_request
        require_local_request(request)
    require_mode("agent")
    if not store.claim_continuation(identifier):
        raise HTTPException(409, "只有失败或已停止的 Agent 分析任务可以手动继续")
    jobs.submit(identifier, resume=True)
    return store.get(identifier)


@app.post("/api/jobs/{identifier}/stop", dependencies=[Depends(access)], status_code=202)
def stop_analysis(identifier: str, request: Request):
    previous = get_job(identifier)
    if previous["kind"] == "local":
        if not config.developer_enabled():
            raise HTTPException(404, "任务来源在当前环境不可用")
        from .developer import require_local_request
        require_local_request(request)
    if not jobs.stop(identifier):
        raise HTTPException(409, "任务已结束，无需停止")
    return store.get(identifier)


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    history: list[dict[str, str]] = Field(default_factory=list, max_length=8)


CHAT_SEMAPHORE = asyncio.Semaphore(2)


@app.post("/api/jobs/{identifier}/chat", dependencies=[Depends(access)])
async def chat(identifier: str, body: ChatRequest):
    require_mode("agent")
    payload = get_result(identifier)
    try:
        async with CHAT_SEMAPHORE:
            return await asyncio.wait_for(ask(payload["files"], payload, body.question, body.history), timeout=300)
    except Exception:
        raise HTTPException(502, "问答失败，请检查模型的工具调用能力及网络后重试") from None


@app.get("/")
def home(request: Request):
    html = (config.WEB_DIR / "index.html").read_text(encoding="utf-8")
    extension = ""
    if config.developer_enabled():
        from .developer import is_local_request
        if is_local_request(request):
            extension = (config.WEB_DIR / "developer" / "import.html").read_text(encoding="utf-8")
    return HTMLResponse(html.replace("<!-- IMPORT_EXTENSION -->", extension), headers={"Cache-Control": "no-store"})


@app.get("/jobs/{identifier}")
def job_page(identifier: str, request: Request):
    return home(request)


@app.get("/projects/{identifier}")
def project_page(identifier: str):
    return FileResponse(config.WEB_DIR / "viewer.html")


if config.developer_enabled():
    from .developer import install
    install(app, access, require_mode)
