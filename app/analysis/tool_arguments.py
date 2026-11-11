"""使用框架校验钩子及 json-repair 处理格式错误，不猜测业务字段。"""
import json
import re
from dataclasses import replace
from json_repair import repair_json
from pydantic import ValidationError
from pydantic_ai import ModelRetry
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart, RetryPromptPart, UserPromptPart
from ..config import model_budget

PROTOCOL = re.compile(r"</?parameter\b|<[^>]*DSML|<｜", re.IGNORECASE)
JSON_RULE = "工具参数必须是严格 JSON 对象：字段名和字符串使用双引号，嵌套对象/数组不能编码成字符串；不要输出 DSML/XML/parameter 协议标记。"


def bad_json(value):
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        json.loads(value)
        return False
    except (ValueError, RecursionError):
        return True


def clean_failed_calls(messages):
    failed = {part.tool_call_id for message in messages for part in message.parts if isinstance(part, RetryPromptPart)}
    removed = {part.tool_call_id for message in messages for part in message.parts if isinstance(part, ToolCallPart)
               and (bad_json(part.args) or (part.tool_call_id in failed and PROTOCOL.search(str(part.args))))}
    if not removed:
        return messages, 0
    cleaned = []
    for message in messages:
        if isinstance(message, ModelResponse) and any(isinstance(part, ToolCallPart) and part.tool_call_id in removed for part in message.parts):
            parts = [part for part in message.parts if not (isinstance(part, ToolCallPart) and part.tool_call_id in removed)]
            if not any(isinstance(part, ToolCallPart) for part in parts):
                parts = []
        else:
            parts = [part for part in message.parts if not (isinstance(part, (ToolReturnPart, RetryPromptPart)) and part.tool_call_id in removed)]
        if parts:
            cleaned.append(replace(message, parts=parts))
    return cleaned, len(removed)


def resolve_schema(schema, root):
    if "$ref" in schema and schema["$ref"].startswith("#/"):
        item = root
        for key in schema["$ref"][2:].split("/"):
            item = item.get(key.replace("~1", "/").replace("~0", "~"), {})
        return item
    return schema


def embedded_objects(value, schema, root, depth=0):
    if depth > 12:
        return value
    schema = resolve_schema(schema, root)
    kind = schema.get("type")
    if isinstance(value, str) and kind in {"object", "array"}:
        try:
            decoded = json.loads(value)
        except (ValueError, RecursionError):
            return value
        if (kind == "object" and isinstance(decoded, dict)) or (kind == "array" and isinstance(decoded, list)):
            value = decoded
    if isinstance(value, dict) and kind == "object":
        properties = schema.get("properties", {})
        return {key: embedded_objects(item, properties.get(key, {}), root, depth + 1) for key, item in value.items()}
    if isinstance(value, list) and kind == "array":
        return [embedded_objects(item, schema.get("items", {}), root, depth + 1) for item in value]
    return value


class ToolArguments(AbstractCapability):
    def __init__(self):
        self.candidates = {}
        self.advice = JSON_RULE

    def retry(self, ctx, tool_def, reason):
        required = ", ".join(tool_def.parameters_json_schema.get("required", []))
        hint = f"{tool_def.name} 参数校验失败：{reason}。{JSON_RULE}顶层必需字段：{required}。"
        if tool_def.name == "save_feature":
            hint += "feature 必须是含 title、desc、steps 的对象，steps 必须为证据对象数组。"
        hint += f"已保存 {len(ctx.deps.features)} 项功能，请只重新提交当前未完成条目，不要重做已保存结果。"
        self.advice = hint
        ctx.deps.validation_error = hint
        ctx.deps.log(hint[:700])
        if ctx.deps.checkpoint:
            ctx.deps.checkpoint()
        raise ModelRetry(hint)

    async def before_tool_validate(self, ctx, *, call, tool_def, args):
        if isinstance(args, str):
            if not args.strip():
                return args
            if len(args) > model_budget().max_output_tokens * 8 + 4096:
                self.retry(ctx, tool_def, "参数过长，请缩小为一项简洁记录")
            try:
                candidate = json.loads(args)
            except (ValueError, RecursionError):
                if PROTOCOL.search(args):
                    self.retry(ctx, tool_def, "检测到 DSML/XML 协议标记污染，拒绝猜测修复，请重新生成纯 JSON")
                try:
                    candidate = repair_json(args, return_objects=True)
                except Exception:
                    self.retry(ctx, tool_def, "JSON 语法损坏，无法修复")
                if not isinstance(candidate, dict):
                    self.retry(ctx, tool_def, "修复后仍不是 JSON 对象")
                candidate = embedded_objects(candidate, tool_def.parameters_json_schema, tool_def.parameters_json_schema)
                self.candidates[call.tool_call_id] = candidate
                return candidate
        elif isinstance(args, dict):
            candidate = args
        else:
            return args
        normalized = embedded_objects(candidate, tool_def.parameters_json_schema, tool_def.parameters_json_schema)
        if normalized != candidate:
            self.candidates[call.tool_call_id] = normalized
            return normalized
        return args

    async def after_tool_validate(self, ctx, *, call, tool_def, args):
        candidate = self.candidates.pop(call.tool_call_id, None)
        if candidate is not None:
            call.args = candidate
            ctx.deps.log(f"已规范化 {tool_def.name} 的 JSON 参数并通过字段校验；源码引用仍需校验")
        return args

    async def on_tool_validate_error(self, ctx, *, call, tool_def, args, error):
        self.candidates.pop(call.tool_call_id, None)
        if isinstance(error, ValidationError):
            reason = "; ".join(str(item.get("loc", "")) + ": " + item.get("msg", "") for item in error.errors(include_input=False)[:3])
        else:
            reason = str(error)
        self.retry(ctx, tool_def, reason)

    async def before_model_request(self, ctx, request_context):
        messages, count = clean_failed_calls(request_context.messages)
        if count:
            ctx.deps.log(f"从模型输入移除 {count} 条非法参数调用，保留已读证据与 {len(ctx.deps.features)} 项已保存功能")
            messages.append(ModelRequest(parts=[UserPromptPart(content=self.advice)]))
            request_context.messages = messages
        return request_context
