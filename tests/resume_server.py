"""仅供浏览器回归测试的框架模型，不访问外部 LLM。"""
import os
import asyncio
import tempfile
import uvicorn
os.environ["WIKI_DATA_DIR"] = tempfile.mkdtemp(prefix="wiki-resume-ui-")
os.environ["WIKI_LLM_MODEL"] = "test-model"
os.environ["WIKI_LLM_API_KEY"] = "test-key"
os.environ["WIKI_ACCESS_TOKEN"] = ""
if os.getenv("WIKI_TEST_PREVIEW") == "1":
    os.environ["WIKI_LLM_MODEL"] = ""
    os.environ["WIKI_LLM_API_KEY"] = ""
os.environ["WIKI_ENV"] = "production"
os.environ["WIKI_DEV_MODE"] = "0"
from app import jobs
from app.api import app
from app.analysis.agent import analyze
from pydantic_ai.models.function import FunctionModel
from .test_resume import failing_model, successful_resume
from .model_responses import recorded_response
from .test_agent import OUTPUT
from pydantic_ai.messages import ToolReturnPart, ToolCallPart, ModelResponse


async def blocking_model(messages, info):
    if any(isinstance(part, ToolReturnPart) and part.tool_name == "save_feature" for message in messages for part in message.parts):
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.8)
    if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
        return ModelResponse(parts=[ToolCallPart("read_file", {"path": "main.py", "start": 1, "end": 2})])
    return recorded_response(messages, info, OUTPUT)


async def framework(files, log, **kwargs):
    initial = blocking_model if os.getenv("WIKI_TEST_STOP") == "1" else failing_model
    model = successful_resume if kwargs.get("state") else initial
    return await analyze(files, log, FunctionModel(model), **kwargs)


jobs.analyze = framework
if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("WIKI_TEST_PORT", "8083")))
