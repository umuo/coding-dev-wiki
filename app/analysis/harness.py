"""官方 harness 组件与当前任务检查点之间的适配，不实现 Agent 工具循环。"""
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai_harness.planning import Planning, InMemoryPlanStore, PlanItem
from pydantic_ai_harness.memory import Memory, InMemoryStore
from ..config import model_budget


class Notebook(InMemoryStore):
    async def write(self, path, content, *, expected_version, operation=None):
        size = sum(len(value) for key, value in self.files.items() if key != path)
        if (len(self.files) >= 32 and path not in self.files) or size + len(content) > 65536:
            from pydantic_ai import ModelRetry
            raise ModelRetry("当前任务工作记忆已满，请合并或删除旧笔记后继续")
        return await super().write(path, content, expected_version=expected_version, operation=operation)


class PlanStore(InMemoryPlanStore):
    async def set_items(self, items):
        self.validate(items)
        await super().set_items(items)

    async def add_item(self, item):
        self.validate([*await self.get_items(), item])
        return await super().add_item(item)

    async def update_item(self, item_id, **updates):
        if updates.get("content") and len(updates["content"]) > 400:
            from pydantic_ai import ModelRetry
            raise ModelRetry("计划内容最多 400 字符")
        return await super().update_item(item_id, **updates)

    @staticmethod
    def validate(items):
        if len(items) > 40 or any(len(item.content) > 400 or len(item.id) > 100 for item in items):
            from pydantic_ai import ModelRetry
            raise ModelRetry("计划最多 40 项，每项内容最多 400 字符、ID 最多 100 字符")


async def prepare(context, state):
    context.plan_store = PlanStore()
    await context.plan_store.set_items([PlanItem.model_validate(item) for item in state.get("plan", [])])
    context.memory_store = Notebook(state.get("memory", {}))
    await snapshot(context)


async def snapshot(context):
    context.plan = [item.model_dump(mode="json") for item in await context.plan_store.get_items()]
    context.memory = dict(context.memory_store.files)


class PersistHarness(AbstractCapability):
    async def after_tool_execute(self, ctx, *, call, tool_def, args, result):
        await snapshot(ctx.deps)
        if ctx.deps.checkpoint:
            ctx.deps.checkpoint()
        return result


def capabilities():
    budget = model_budget()
    return [Planning(store_resolver=lambda ctx: ctx.deps.plan_store,
                     inject=False, tools=["read_plan", "add_task", "update_task_status"],
                     guidance="分析计划会随请求提供。模块进度由系统管理；可用 add_task 记录待查问题，用 update_task_status 更新自己添加的任务。"),
            Memory(store_resolver=lambda ctx: ctx.deps.memory_store, agent_name="analysis",
                   max_tokens=min(1200, max(128, budget.input_tokens // 12)),
                   max_memory_size=min(8000, budget.tool_tokens * 2), max_search_result_chars=budget.tool_tokens,
                   guidance="使用 write_memory 保存当前任务的发现、待查问题与下一步，MEMORY.md 保持简短；read_memory/search_memory 按需取回细节。笔记不能代替源码读取验证。",
                   injection_errors="raise"), PersistHarness()]
