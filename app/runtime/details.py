"""展示工具结果的路径、计数与错误摘要，不把源码全文混进日志。"""
import re


def outcome(tool, state, path, clean):
    metadata = state.get("metadata", {})
    display = metadata.get("display", {})
    output = state.get("output", "")
    if tool == "read":
        start, end = display.get("lineStart"), display.get("lineEnd")
        if start is not None and end is not None:
            return f"返回第 {start}–{end} 行" + (f" / 文件共 {display['totalLines']} 行" if display.get("totalLines") is not None else "")
        if display.get("type") == "directory":
            return "目录读取完成"
    if tool == "glob":
        names = []
        for line in output.splitlines():
            candidate = line.strip()
            if not candidate.startswith(("/", "repo/")):
                continue
            relative = path(candidate)
            if relative != "受限目录":
                names.append(relative)
        if names:
            return f"返回 {len(names)} 个路径：" + "、".join(names[:4]) + ("…" if len(names) > 4 else "")
        count = metadata.get("count")
        if isinstance(count, int):
            return f"返回 {count} 个路径"
    if tool == "grep":
        count = metadata.get("matches")
        if not isinstance(count, int):
            found = re.search(r"Found (\d+) match", output)
            count = int(found[1]) if found else None
        return f"匹配 {count} 处" if count is not None else "检索完成"
    return ""


def failure(error, clean):
    message = str(error or "")
    if "rule which prevents" in message or "permission" in message.lower():
        return "权限拒绝：目标不在允许操作范围"
    if "not found" in message.lower() or "ENOENT" in message:
        return "文件或目录不存在"
    if "directory" in message.lower():
        return "目标为目录，请使用目录读取或文件检索"
    return clean(message) if message else "工具未返回具体原因"


def plan(todos):
    labels = {"pending": "待处理", "in_progress": "进行中", "completed": "已完成", "cancelled": "已取消"}
    items = [item for item in todos if isinstance(item, dict)]
    count = sum(item.get("status") == "completed" for item in items)
    tasks = [labels.get(item.get("status"), "待处理") + "：" + str(item.get("content", item.get("description", ""))) for item in items[:4]]
    return f"{len(items)} 项任务，{count} 项已完成；" + "；".join(tasks)
