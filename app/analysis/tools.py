"""源码只读工具与 Agent 依赖状态。"""
import json
from dataclasses import dataclass, field
from typing import Callable
from pydantic_ai import ModelRetry, RunContext
from .budget import count_tokens


@dataclass
class RepositoryContext:
    files: dict
    log: Callable
    reads: dict = field(default_factory=dict)
    calls: int = 0
    validation_error: str = ""
    checkpoint: Callable | None = None
    features: dict = field(default_factory=dict)
    nodes: dict = field(default_factory=dict)
    edges: dict = field(default_factory=dict)
    workflow: dict = field(default_factory=dict)
    plan: list = field(default_factory=list)
    memory: dict = field(default_factory=dict)
    plan_store: object = None
    memory_store: object = None



def register_read_tools(agent, budget):
    def report(ctx, message):
        ctx.deps.calls += 1
        ctx.deps.log(message)
        if ctx.deps.checkpoint:
            ctx.deps.checkpoint()

    @agent.tool
    def list_files(ctx: RunContext[RepositoryContext], prefix: str = "", offset: int = 0) -> dict:
        """按路径前缀分页列出文件、行数、语言及符号；每页最多 60 个文件。"""
        report(ctx, "查看文件目录 " + (prefix or "/"))
        entries = [f for p, f in ctx.deps.files.items() if p.startswith(prefix)]
        selected = []
        size = 0
        offset = max(0, offset)
        for file in entries[offset:offset + budget.page_size]:
            item = {"path": file["path"], "lines": file["lines"], "language": file["lang"],
                    "symbols": [{key: symbol[key] for key in ("name", "line", "end_line")} for symbol in file["symbols"][:35]]}
            while item["symbols"] and count_tokens(json.dumps(item, ensure_ascii=False)) > budget.tool_tokens:
                item["symbols"].pop()
            cost = count_tokens(json.dumps(item, ensure_ascii=False)) + 2
            if selected and size + cost > budget.tool_tokens:
                break
            selected.append(item)
            size += cost
        next_offset = offset + len(selected)
        return {"total": len(entries), "files": selected,
                "next_offset": next_offset if next_offset < len(entries) else None}


    @agent.tool
    def read_file(ctx: RunContext[RepositoryContext], path: str, start: int = 1, end: int = 120) -> str:
        """读取精确行范围（1-based inclusive），最多 180 行；按模型预算截断，返回实际行号。"""
        file = ctx.deps.files.get(path)
        if not file:
            raise ModelRetry("文件不存在，请先调用 list_files")
        if not (1 <= start <= end) or start > file["lines"]:
            raise ModelRetry("起始行号超出文件，请使用 list_files 查看行数")
        end = min(end, start + 179, file["lines"])
        lines = file["content"].splitlines()
        selected = []
        size = 0
        actual_end = start - 1
        for number in range(start, end + 1):
            text = f"{number}: {lines[number - 1]}"
            cost = count_tokens(text) + 1
            if size + cost > budget.tool_tokens:
                break
            selected.append(text)
            size += cost
            actual_end = number
        if not selected:
            raise ModelRetry("单行太长，无法读取；请选择其他证据")
        ctx.deps.reads.setdefault(path, []).append((start, actual_end))
        report(ctx, f"阅读 {path}:{start}-{actual_end}")
        return "\n".join(selected)

    @agent.tool
    def search_code(ctx: RunContext[RepositoryContext], query: str, prefix: str = "") -> list[dict]:
        """对源码做不区分大小写的字面搜索，最多返回 40 个匹配行，不作为已阅读证据。"""
        if len(query) < 2:
            raise ModelRetry("搜索词至少两个字符")
        report(ctx, "检索代码 " + query[:80])
        found = []
        size = 0
        for path, file in ctx.deps.files.items():
            if not path.startswith(prefix):
                continue
            for number, line in enumerate(file["content"].splitlines(), 1):
                if query.lower() in line.lower():
                    item = {"path": path, "line": number, "text": line[:250]}
                    cost = count_tokens(json.dumps(item, ensure_ascii=False)) + 2
                    if size + cost > budget.tool_tokens:
                        return found
                    found.append(item)
                    size += cost
                    if len(found) >= min(40, max(5, budget.tool_tokens // 80)):
                        return found
        return found
