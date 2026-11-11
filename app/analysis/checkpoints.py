"""原子保存源码索引、框架消息和已读区间，供人工恢复使用。"""
import json
import threading
from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelRequest, RetryPromptPart, ToolCallPart, ToolReturnPart, ModelResponse
from .. import store
from ..config import model_config


def save(identifier, name, value):
    target = store.directory(identifier) / (name + ".json")
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(target)


def load(identifier, name):
    path = store.directory(identifier) / (name + ".json")
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def writer(identifier):
    lock = threading.Lock()
    closed = False
    def write(state):
        with lock:
            if not closed:
                save(identifier, "agent-state", state)
    def close():
        nonlocal closed
        with lock:
            closed = True
    write.close = close
    return write


def recover_messages(serialized, migrate=False, log=None):
    messages = ModelMessagesTypeAdapter.validate_python(serialized)
    if migrate:
        allowed = {"list_files", "read_file", "search_code"}
        removed = {part.tool_call_id for message in messages for part in message.parts
                   if isinstance(part, ToolCallPart) and part.tool_name not in allowed}
        cleaned = []
        for message in messages:
            if isinstance(message, ModelResponse) and any(isinstance(part, ToolCallPart) and part.tool_call_id in removed for part in message.parts):
                parts = [part for part in message.parts if isinstance(part, ToolCallPart) and part.tool_call_id not in removed]
            else:
                parts = [part for part in message.parts if not (isinstance(part, (ToolReturnPart, RetryPromptPart)) and part.tool_call_id in removed)]
            if parts:
                from dataclasses import replace
                cleaned.append(replace(message, parts=parts))
        messages = cleaned
    from .tool_arguments import clean_failed_calls
    messages, removed = clean_failed_calls(messages)
    if removed and log:
        log(f"恢复时清理 {removed} 条非法 JSON/协议调用，已读区间和已保存结果保持不变")
    pending = {}
    for message in messages:
        for part in message.parts:
            if isinstance(part, ToolCallPart):
                pending[part.tool_call_id] = part
            elif isinstance(part, (ToolReturnPart, RetryPromptPart)):
                pending.pop(part.tool_call_id, None)
    if pending:
        messages.append(ModelRequest(parts=[RetryPromptPart(
            content="上次运行在调用完成前中断，不能视为成功；请重新核验后继续。",
            tool_name=part.tool_name, tool_call_id=part.tool_call_id,
        ) for part in pending.values()]))
    return messages


def serialize(messages, context):
    return {"version": 2, "workflow": context.workflow, "plan": context.plan, "memory": context.memory, "features": dict(context.features), "nodes": dict(context.nodes), "edges": dict(context.edges), "messages": ModelMessagesTypeAdapter.dump_python(list(messages), mode="json"),
            "reads": {path: list(ranges) for path, ranges in context.reads.items()},
            "calls": context.calls, "validation_error": context.validation_error}


def error_message(exc, state=None):
    detail = str(exc) or "未返回错误详情"
    validation = (state or {}).get("validation_error", "")
    from pydantic_ai.exceptions import IncompleteToolCall, UnexpectedModelBehavior
    relevant = isinstance(exc, UnexpectedModelBehavior) and not isinstance(exc, IncompleteToolCall)
    if relevant and not validation:
        for message in reversed((state or {}).get("messages", [])):
            for part in reversed(message.get("parts", [])):
                if part.get("part_kind") == "retry-prompt":
                    content = part.get("content", "")
                    if isinstance(content, list):
                        validation = "; ".join(str(item.get("loc", "")) + ": " + item.get("msg", "") for item in content if isinstance(item, dict))
                    else:
                        validation = str(content)
                    break
            if validation:
                break
    if relevant and validation:
        detail += "；最近校验反馈：" + validation
    key = model_config()["api_key"]
    if key:
        detail = detail.replace(key, "[REDACTED]")
    return f"{type(exc).__name__}：{detail[:700]}"
