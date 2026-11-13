"""快速概览的产品预算；原生运行时仍负责全部工具执行。"""
import asyncio
import json
from collections import Counter

EXPLORE_SECONDS = 90
FINALIZE_SECONDS = 90
MAX_TOOLS = 24
MAX_FILES = 12
MAX_REQUESTS = 8


class ExplorationComplete(Exception):
    """探索预算已用完，转入只整理阶段。"""


class ExplorationBudget:
    def __init__(self, seen=()):
        self.previous = set(seen)
        self.tools = set()
        self.requests = set()
        self.previous_requests = set()
        self.files = set()

    def observe(self, messages):
        for message in messages:
            info = message.get("info", {})
            if info.get("role") == "assistant" and info.get("id") and info["id"] not in self.previous_requests:
                self.requests.add(info["id"])
            for part in message.get("parts", []):
                identity = part.get("id")
                if part.get("type") == "tool" and identity and identity not in self.previous:
                    self.tools.add(identity)
                    if part.get("tool") == "read":
                        self.files.add(part.get("state", {}).get("input", {}).get("filePath", ""))
        # 一次并行工具批次可能略超限；在下一轮检索前中断并保存真实已读区间。
        if len(self.tools) >= MAX_TOOLS or len(self.files) >= MAX_FILES or len(self.requests) >= MAX_REQUESTS:
            raise ExplorationComplete("已达到首次探索预算")


def repository_map(files):
    directories = Counter(path.split("/")[0] for path in files if "/" in path)
    preferred = []
    for path in files:
        name = path.rsplit("/", 1)[-1].lower()
        if name.startswith("readme") or name in {"package.json", "pyproject.toml", "pom.xml", "go.mod", "cargo.toml", "main.py", "app.py", "index.ts", "main.ts"}:
            preferred.append(path)
    preferred.sort(key=lambda path: (path.count("/"), len(path), path))
    return json.dumps({"文件总数": len(files), "顶层目录及文件数": directories.most_common(30),
                       "优先阅读候选（仅路径，不是已读证据）": preferred[:24]}, ensure_ascii=False)


async def explore(runtime, prompt, observe):
    try:
        return await asyncio.wait_for(runtime.prompt(prompt, observe), EXPLORE_SECONDS)
    except (ExplorationComplete, asyncio.TimeoutError):
        await runtime.abort()
        runtime.log("首次探索预算已到，停止继续检索，使用已读证据生成概览")
        return None


def synthesis_prompt(schema, reads, feedback=""):
    return ("仅修正最终结果并整理快速概览。现在禁止继续调用工具。只基于会话中已读取的证据，"
            "保留最重要的 1-4 个功能、最多 5 个架构节点和 6 条关系；每项只用 1-2 条关键引用。"
            "未知部分写入 limitations，不编造完整覆盖。引用必须落在以下已核验区间内："
            + json.dumps(reads, ensure_ascii=False) + "。校验反馈：" + feedback[:700]
            + "。仅输出符合此 Schema 的 JSON：" + json.dumps(schema, ensure_ascii=False))
