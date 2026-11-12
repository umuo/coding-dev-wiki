"""浏览器验收：真实 OpenCode 连接仅本机假模型，生产任务入口不打补丁。"""
import asyncio
import json
import os
import tempfile
import threading
from pathlib import Path
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from .test_agent import OUTPUT

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

fake = FastAPI()
resumed = False


@fake.post("/v1/chat/completions")
async def completion(request: Request):
    body = await request.json()
    done = any(message["role"] == "tool" for message in body["messages"])
    async def stream():
        global resumed
        def event(delta, reason=None):
            return "data: " + json.dumps({"id": "fake", "object": "chat.completion.chunk", "created": 0,
                "model": "fake-model", "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}) + "\n\n"
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
