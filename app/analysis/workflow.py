"""按模块隔离模型历史；持久化完成摘要，恢复未完成阶段。"""
import hashlib
import json
from pydantic_ai_harness.planning import PlanItem, TaskStatus
from .harness import snapshot
from .budget import count_tokens


def partition(files):
    paths = sorted(files)
    if len(paths) <= 12:
        buckets = {"项目": paths}
    else:
        buckets = {}
        for path in paths:
            parts = path.split("/")
            prefix = "/".join(parts[:min(2, len(parts) - 1)]) if len(parts) > 1 else "根目录"
            buckets.setdefault(prefix, []).append(path)
        chunks = {}
        for prefix, entries in buckets.items():
            for offset in range(0, len(entries), 60):
                name = prefix if len(entries) <= 60 else f"{prefix}（分段 {offset // 60 + 1}）"
                chunks[name] = entries[offset:offset + 60]
        buckets = chunks
        # 保留全部路径，最多 12 个阶段，避免超多小目录造成请求膨胀。
        if len(buckets) > 12:
            merged = [[] for _ in range(12)]
            for _, entries in sorted(buckets.items(), key=lambda item: (-len(item[1]), item[0])):
                min(merged, key=len).extend(entries)
            buckets = {f"分区 {index + 1}": sorted(entries) for index, entries in enumerate(merged) if entries}
    return [{"id": "module-" + hashlib.sha256("\n".join(entries).encode()).hexdigest()[:12],
             "name": name[:160], "paths": entries, "status": "pending"} for name, entries in buckets.items()]


async def initialize(context):
    if not context.workflow:
        modules = partition(context.files)
        context.workflow = {"modules": modules, "active": modules[0]["id"] if modules else "synthesis"}
    known = {item.id for item in await context.plan_store.get_items()}
    for module in context.workflow["modules"]:
        if module["id"] not in known:
            await context.plan_store.add_item(PlanItem(id=module["id"], content="分析 " + module["name"]))
    await snapshot(context)


def save(context):
    if context.checkpoint:
        context.checkpoint()


async def finish_module(context, module, output):
    module["summary"] = output.model_dump()
    module["status"] = "completed"
    await context.plan_store.update_item(module["id"], status=TaskStatus.completed)
    # 摘要独立于可编辑笔记，不因模型覆盖 MEMORY.md 而丢失。
    context.log("模块分析完成：" + module["name"])
    await snapshot(context)
    save(context)


def briefing(context):
    entries = [{"id": item["id"], "name": item["name"], "status": item["status"]}
               for item in context.workflow.get("modules", [])]
    module_ids = {item["id"] for item in context.workflow.get("modules", [])}
    questions = [{"id": item["id"], "status": item["status"], "content": item["content"][:120]}
                 for item in context.plan if item["id"] not in module_ids and item["status"] not in {"completed", "cancelled"}]
    return json.dumps({"模块进度": entries, "待查任务（完整计划用 read_plan）": questions[:6], "已保存功能": len(context.features), "已保存架构": len(context.nodes),
                       "提示": "用 read_module 查看已完成模块摘要，list_saved 查看结果，read_memory 查看工作笔记。"}, ensure_ascii=False)


def register_workflow_tools(agent, budget):
    from pydantic_ai import RunContext, ModelRetry
    from .tools import RepositoryContext

    @agent.tool
    def read_module(ctx: RunContext[RepositoryContext], module_id: str) -> dict:
        """按计划中的模块 ID 取回已经完成的模块摘要，不读取历史原始工具输出。"""
        module = next((item for item in ctx.deps.workflow.get("modules", []) if item["id"] == module_id), None)
        if module is None:
            raise ModelRetry("模块不存在，请查看当前模块计划")
        return {"id": module["id"], "name": module["name"], "status": module["status"], "summary": module.get("summary")}

    @agent.tool
    def module_files(ctx: RunContext[RepositoryContext], offset: int = 0) -> dict:
        """分页列出当前模块的准确文件路径；路径分区仅用于调度，源码工具仍可跨模块追踪依赖。"""
        module = next((item for item in ctx.deps.workflow.get("modules", []) if item["id"] == ctx.deps.workflow.get("active")), None)
        paths = module["paths"] if module else sorted(ctx.deps.files)
        selected = []
        for path in paths[max(0, offset):max(0, offset) + budget.page_size]:
            if selected and count_tokens(json.dumps([*selected, path])) > budget.tool_tokens:
                break
            selected.append(path)
        next_offset = max(0, offset) + len(selected)
        return {"paths": selected, "total": len(paths), "next_offset": next_offset if next_offset < len(paths) else None}
