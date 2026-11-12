"""只统计生成流与工具调用，不保存或输出 reasoning 内容。"""
import time


class Generation:
    def __init__(self):
        self.round = 0
        self.reset()

    def reset(self):
        self.round += 1
        self.lengths = {}
        self.kinds = {}
        self.tools = set()
        self.started = self.updated = time.monotonic()
        self.emitted = 0

    def part(self, part):
        identity = part.get("id")
        kind = part.get("type")
        if kind == "tool":
            self.tools.add(identity)
            self.updated = time.monotonic()
        elif kind in {"text", "reasoning"} and identity:
            self.kinds[identity] = kind
            length = len(part.get("text", ""))
            if length > self.lengths.get(identity, 0):
                self.lengths[identity] = length
                self.updated = time.monotonic()

    def delta(self, properties):
        identity = properties.get("partID")
        if identity in self.kinds and properties.get("field") == "text":
            self.lengths[identity] = self.lengths.get(identity, 0) + len(properties.get("delta", ""))
            self.updated = time.monotonic()

    def summary(self, elapsed=None):
        elapsed = int(time.monotonic() - self.started) if elapsed is None else elapsed
        count = sum(self.lengths.values())
        flow = f"已接收 {count} 字符的响应流" if count else "尚未收到模型生成流"
        calls = f"当前请求工具调用 {len(self.tools)} 次"
        if not self.tools:
            calls += "，尚未观察到源码工具调用"
        return f"{flow}；{calls}；耗时 {elapsed} 秒；最近响应 {int(time.monotonic() - self.updated)} 秒前"

    def due(self):
        now = time.monotonic()
        if sum(self.lengths.values()) and now - self.emitted >= 5:
            self.emitted = now
            return True
        return False
