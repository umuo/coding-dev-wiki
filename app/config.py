"""集中管理服务配置；密钥仅保留在服务端。"""
import os
from pathlib import Path
from dataclasses import dataclass

DATA_DIR = Path(os.getenv("WIKI_DATA_DIR", ".data")).resolve()
WEB_DIR = Path(__file__).resolve().parents[1] / "web"
MAX_UPLOAD = 50 * 1024 * 1024
MAX_EXPANDED = 150 * 1024 * 1024
MAX_FILES = 2000
MAX_FILE_BYTES = 512 * 1024
GIT_HOSTS = set(os.getenv("WIKI_GIT_HOSTS", "github.com,gitlab.com,bitbucket.org").split(","))
ALLOW_FAKE_IP = os.getenv("WIKI_GIT_ALLOW_FAKE_IP", "0") == "1"
ACCESS_TOKEN = os.getenv("WIKI_ACCESS_TOKEN", "")


def model_config():
    return {
        "base_url": os.getenv("WIKI_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        "api_key": os.getenv("WIKI_LLM_API_KEY", ""),
        "model": os.getenv("WIKI_LLM_MODEL", ""),
    }


def model_ready():
    config = model_config()
    return bool(config["model"] and config["api_key"])


def developer_enabled():
    """默认关闭；必须同时明确指定开发环境和开发开关。"""
    return os.getenv("WIKI_ENV", "production") == "development" and os.getenv("WIKI_DEV_MODE", "0") == "1"


@dataclass(frozen=True)
class ModelBudget:
    context_tokens: int
    max_output_tokens: int
    input_tokens: int
    tool_tokens: int
    page_size: int


def model_budget():
    value = os.getenv("WIKI_LLM_CONTEXT_TOKENS", "128000").strip() or "128000"
    try:
        context = int(value)
    except ValueError:
        raise ValueError("WIKI_LLM_CONTEXT_TOKENS 必须是整数 token 数，例如 128000") from None
    if context < 8192:
        raise ValueError("WIKI_LLM_CONTEXT_TOKENS 至少为 8192，以容纳 Agent 工具定义")
    output = min(8192, max(1024, context // 4))
    reserve = max(512, context // 8)
    available = context - output - reserve
    tool = min(6000, max(256, available // 5))
    return ModelBudget(context, output, available, tool, min(60, max(5, tool // 80)))
