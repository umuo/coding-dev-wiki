"""浏览器验收：真实 OpenCode 连接仅本机假模型，生产任务入口不打补丁。"""
import asyncio
import json
import os
import tempfile
import threading
from pathlib import Path
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse

os.environ["WIKI_OPENCODE_BIN"] = str(Path(__file__).resolve().parents[1] / ".data/bin/opencode")
os.environ["WIKI_DATA_DIR"] = tempfile.mkdtemp(prefix="wiki-native-browser-")
os.environ["WIKI_LLM_BASE_URL"] = "http://127.0.0.1:8086/v1"
os.environ["WIKI_LLM_API_KEY"] = "fake-key"
os.environ["WIKI_LLM_MODEL"] = "fake-model"
os.environ["WIKI_LLM_CONTEXT_TOKENS"] = "32768"
os.environ["WIKI_ACCESS_TOKEN"] = ""
os.environ["WIKI_ENV"] = "production"
os.environ["WIKI_DEV_MODE"] = "0"
from app.api import app
from app import config
from .test_agent import OUTPUT

assert config.DATA_DIR == Path(os.environ["WIKI_DATA_DIR"]).resolve(), "浏览器服务必须使用隔离数据目录"

fake = FastAPI()
resumed = False
timeout_next = False
generation_only = False


@app.post("/test/timeout-next")
def fail_next():
    global timeout_next
    timeout_next = True
    return {"ok": True}


@app.post("/test/generation-only")
def generate_without_tools():
    global generation_only
    generation_only = True
    return {"ok": True}


@fake.post("/v1/chat/completions")
async def completion(request: Request):
    global timeout_next
    body = await request.json()
    done = any(message["role"] == "tool" for message in body["messages"])
    if done and timeout_next:
        timeout_next = False
        return JSONResponse({"error": {"message": "The operation timed out.", "type": "invalid_request_error"}}, status_code=400)
    async def stream():
        global resumed, generation_only
        def event(delta, reason=None):
            return "data: " + json.dumps({"id": "fake", "object": "chat.completion.chunk", "created": 0,
                "model": "fake-model", "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}) + "\n\n"
        if generation_only and not done:
            try:
                yield event({"role": "assistant", "reasoning_content": "PRIVATE REASONING NEVER DISPLAY THIS"})
                while not await request.is_disconnected():
                    await asyncio.sleep(.05)
            finally:
                generation_only = False
            return
        if done and not resumed:
            try:
                yield event({"role": "assistant", "content": ""})
                while not await request.is_disconnected():
                    await asyncio.sleep(.05)
            finally:
                resumed = True
            return
        if done:
            yield event({"role": "assistant", "content": json.dumps(OUTPUT)})
            yield event({}, "stop")
        else:
            yield event({"role": "assistant", "tool_calls": [{"index": 0, "id": "read", "type": "function",
                "function": {"name": "read", "arguments": json.dumps({"filePath": "repo/main.py", "offset": 1, "limit": 2})}}]})
            yield event({}, "tool_calls")
        yield "data: [DONE]\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream")


if __name__ == "__main__":
    server = uvicorn.Server(uvicorn.Config(fake, host="127.0.0.1", port=8086, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=8085)
