"""将原生事件映射为可公开的动作；不转发源码输出、密钥或思考文本。"""
from pathlib import Path
from ..config import model_config

TOOL_NAMES = {"read": "读取源码", "grep": "搜索代码", "glob": "查找文件", "todowrite": "更新分析计划"}


class Actions:
    def __init__(self, runtime):
        self.runtime = runtime
        self.previous = {}
        self.compaction = None

    def clean(self, value):
        text = str(value or "")
        key = model_config()["api_key"]
        if key:
            text = text.replace(key, "[已隐藏]")
        return text[:240]

    def publish(self, identity, kind, status, title, detail="", **extra):
        action = {"id": identity, "kind": kind, "status": status, "title": self.clean(title),
                  "detail": self.clean(detail), **extra}
        previous = self.previous.get(identity)
        if kind in {"tool", "model", "compaction"} and previous and previous["status"] in {"completed", "failed"} and status in {"queued", "running"}:
            return
        if previous == action:
            return
        self.previous[identity] = action
        if len(self.previous) > 1000:
            self.previous.pop(next(iter(self.previous)))
        callback = getattr(self.runtime.log, "action", None)
        if callback:
            callback(action)
        else:
            self.runtime.log("OpenCode " + action["title"] + " · " + status + ("：" + action["detail"] if detail else ""))

    def path(self, raw):
        if not raw:
            return ""
        path = Path(str(raw))
        workspace = self.runtime.workspace.resolve()
        path = path.resolve() if path.is_absolute() else (workspace / path).resolve()
        if path.is_relative_to(workspace / "repo"):
            return path.relative_to(workspace / "repo").as_posix()
        return "受限目录"

    def part(self, part):
        identity = part.get("id")
        if not identity:
            return
        kind = part.get("type")
        if kind == "step-start":
            self.publish("step-" + part.get("messageID", identity), "model", "running", "模型正在处理请求")
        elif kind == "step-finish":
            # 与同一消息的 step-start 合并，由 snapshot/message 更新补齐 ID。
            self.publish("step-" + part.get("messageID", identity), "model", "completed", "模型请求完成")
        elif kind == "compaction":
            self.compaction = identity
            self.publish(identity, "compaction", "running", "正在压缩上下文", "原生运行时整理已有分析成果")
        elif kind == "tool":
            state = part.get("state", {})
            status = {"pending": "queued", "running": "running", "completed": "completed", "error": "failed"}.get(state.get("status"))
            if not status:
                return
            tool = part.get("tool", "")
            args = state.get("input", {})
            path = self.path(args.get("filePath") or args.get("path"))
            if tool == "read":
                detail = path
                if args.get("offset"):
                    detail += " · 起始行 " + str(args["offset"])
                if args.get("limit"):
                    detail += " · 最多 " + str(args["limit"]) + " 行"
            elif tool in {"grep", "glob"}:
                detail = self.clean(args.get("pattern", "")) + (" · " + path if path else "")
            elif tool == "todowrite":
                todos = args.get("todos", [])
                detail = f"{len(todos)} 项任务，{sum(item.get('status') == 'completed' for item in todos if isinstance(item, dict))} 项已完成"
            else:
                detail = ""
            clock = state.get("time", {})
            duration = max(0, clock.get("end", 0) - clock.get("start", 0)) if clock.get("end") else None
            self.publish(identity, "tool", status, TOOL_NAMES.get(tool, "执行工具 " + tool), detail, tool=tool, durationMs=duration)

    def snapshot(self, messages):
        for message in messages:
            for part in message.get("parts", []):
                if part.get("type") != "compaction":
                    self.part(part)

    def event(self, event):
        properties = event.get("properties", {})
        if not isinstance(properties, dict):
            return
        part = properties.get("part", {})
        if not isinstance(part, dict):
            return
        session = properties.get("sessionID") or part.get("sessionID") or properties.get("info", {}).get("sessionID")
        if session != self.runtime.session_id:
            return
        kind = event.get("type")
        if kind == "message.part.updated":
            if part.get("type") == "step-start":
                part = {**part, "id": "step-" + part.get("messageID", part.get("id", ""))}
            self.part(part)
            return part if part.get("type") == "tool" and part.get("state", {}).get("status") == "completed" else None
        if kind == "session.status":
            status = properties.get("status", {}).get("type")
            if status == "busy":
                self.publish("session-work", "session", "running", "OpenCode 正在分析", "等待模型响应或执行工具")
            elif status == "idle":
                self.publish("session-work", "session", "completed", "本轮模型请求已结束")
            elif status == "retry":
                attempt = properties["status"].get("attempt", 1)
                self.publish("native-retry-" + str(attempt), "retry", "waiting", "模型接口正在重试", f"原生重试第 {attempt} 次")
        elif kind == "session.compacted":
            self.publish(self.compaction or event.get("id", "compaction"), "compaction", "completed", "上下文已压缩", "原生会话保留分析成果")
            self.compaction = None
        elif kind == "session.error":
            active = [identity for identity, action in self.previous.items()
                      if action["kind"] == "model" and action["status"] == "running"]
            for identity in active or [event.get("id", "model-error")]:
                self.publish(identity, "model", "failed", "模型请求失败", "会话已保留，正在处理错误")
        # reasoning/text/input 的逐 token 增量不进入 UI 或日志。
