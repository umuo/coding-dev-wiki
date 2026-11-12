"""复用 httpx-sse 消费官方 /event；先订阅再提交模型请求。"""
import asyncio
import json
import contextlib
import httpx
from httpx_sse import aconnect_sse, SSEError
from .actions import Actions


class EventStream:
    def __init__(self, runtime, observe):
        self.runtime, self.observe = runtime, observe
        self.actions = Actions(runtime)
        self.ready = asyncio.Event()
        self.connected = False
        self.task = None

    async def start(self):
        self.task = asyncio.create_task(self.consume())
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.ready.wait(), 5)
        self.check()

    def check(self):
        if self.task.done() and not self.task.cancelled():
            error = self.task.exception()
            if error:
                raise error

    async def close(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    async def consume(self):
        backoff = .25
        while True:
            try:
                async with aconnect_sse(self.runtime.client, "GET", "/event", timeout=None) as source:
                    source.response.raise_for_status()
                    self.connected = True
                    self.ready.set()
                    self.actions.publish("connection", "connection", "connected", "实时动作流已连接")
                    # /event 无历史重放；重连后从消息快照补齐工具状态与读取证据。
                    messages = await self.runtime.messages()
                    self.observe(messages)
                    self.actions.snapshot(messages)
                    backoff = .25
                    async for item in source.aiter_sse():
                        try:
                            event = json.loads(item.data)
                        except (ValueError, TypeError):
                            continue
                        if not isinstance(event, dict):
                            continue
                        completed = self.actions.event(event)
                        if completed:
                            self.observe([{"parts": [completed]}])
                self.connected = False
            except (httpx.HTTPError, SSEError):
                self.connected = False
            self.actions.publish("connection", "connection", "reconnecting", "实时动作流正在重连", "分析继续，期间通过消息快照补齐状态")
            await asyncio.sleep(backoff)
            backoff = min(2, backoff * 2)
