"""测试模型逐条提交真实记录工具，再返回简短概述。"""
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart


def summary(output):
    return {key: output[key] for key in ("overview", "readingGuide", "limitations")}


def recorded_response(messages, info, output):
    saved = {(part.tool_name, part.content.get("saved")) for message in messages for part in message.parts
             if isinstance(part, ToolReturnPart) and isinstance(part.content, dict)}
    for group_index, group in enumerate(output["featureGroups"]):
        for feature_index, feature in enumerate(group["features"]):
            key = f"feature-{group_index}-{feature_index}"
            if ("save_feature", key) not in saved:
                return ModelResponse(parts=[ToolCallPart("save_feature", {"key": key, "group": group["title"], "summary": group["summary"], "feature": feature})])
    for index, node in enumerate(output["architecture"]):
        key = f"node-{index}"
        if ("save_node", key) not in saved:
            return ModelResponse(parts=[ToolCallPart("save_node", {"key": key, **node})])
    for index, edge in enumerate(output.get("edges", [])):
        key = f"edge-{index}"
        if ("save_edge", key) not in saved:
            return ModelResponse(parts=[ToolCallPart("save_edge", {"key": key, "source": f"node-{edge['source']}", "target": f"node-{edge['target']}", "label": edge["label"], "evidence": edge["evidence"][0]})])
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, summary(output))])
