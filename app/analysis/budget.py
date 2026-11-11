"""通过 Pydantic AI Capability 管理请求预算，保留完整工具调用配对。"""
import json
from functools import lru_cache
import tiktoken
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import UserPromptPart
from ..config import model_budget, model_config


@lru_cache(maxsize=8)
def encoding(model):
    try:
        try:
            return tiktoken.encoding_for_model(model)
        except KeyError:
            return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def count_tokens(text):
    encoder = encoding(model_config()["model"])
    if encoder is None:
        # 无法获取词表时按 UTF-8 字节数保守估算，不依赖网络成功才能运行。
        return len(text.encode("utf-8"))
    return len(encoder.encode(text, disallowed_special=()))


def message_tokens(messages):
    from pydantic_ai_harness.compaction import estimate_token_count
    return estimate_token_count(messages, count_tokens) + len(messages) * 12


class ContextBudget(AbstractCapability):
    async def before_model_request(self, ctx, request_context):
        from pydantic_ai_harness.compaction import SlidingWindowCompaction
        from .workflow import briefing
        budget = model_budget()
        parameters = request_context.model_request_parameters
        tools = [*parameters.function_tools, *parameters.output_tools]
        definitions = [{"name": tool.name, "description": tool.description,
                        "parameters": tool.parameters_json_schema} for tool in tools]
        fixed = count_tokens(json.dumps(definitions, ensure_ascii=False))
        fixed += count_tokens(str(parameters.instruction_parts or ""))
        limit = budget.input_tokens - fixed
        if limit < 512:
            raise ValueError("配置的上下文窗口不足以容纳工具定义与输出预算，请增大 WIKI_LLM_CONTEXT_TOKENS")
        messages = request_context.messages
        # 工作状态每轮从检查点重新生成，不依赖已裁掉的工具历史。
        marker = "[repository-work-state]"
        from dataclasses import replace
        messages = [replace(message, parts=[part for part in message.parts
                    if not (isinstance(part, UserPromptPart) and isinstance(part.content, str) and part.content.startswith(marker))])
                    for message in messages]
        messages = [message for message in messages if message.parts]
        if ctx.deps.workflow:
            last = messages[-1]
            messages[-1] = replace(last, parts=[*last.parts, UserPromptPart(marker + briefing(ctx.deps))])
        if message_tokens(messages) > limit:
            compactor = SlidingWindowCompaction(max_tokens=limit, keep_tokens=max(256, limit // 2), tokenizer=count_tokens)
            messages = await compactor.compact(messages, ctx)
            ctx.deps.log(f"上下文接近 {budget.context_tokens} token 窗口，使用官方 harness 压缩旧轮次；模块摘要、笔记与已保存结果保留")
        if message_tokens(messages) > limit:
            raise ValueError(f"最近一轮工具输出超过上下文预算（消息 {message_tokens(messages)} / 可用 {limit} token），请缩小读取范围或增大 WIKI_LLM_CONTEXT_TOKENS 后继续")
        request_context.messages = messages
        ctx.deps.validation_error = ""
        return request_context
