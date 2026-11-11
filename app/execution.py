"""任务取消令牌与活动执行器；不替代 Pydantic AI 的 Agent 循环。"""
import asyncio
import threading
from dataclasses import dataclass, field


class TaskStopped(Exception):
    pass


@dataclass
class Execution:
    requested: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    future: object = None
    loop: object = None
    task: object = None

    def check(self):
        if self.requested.is_set():
            raise TaskStopped("用户请求停止")

    def stop(self):
        with self.lock:
            if self.requested.is_set():
                return
            self.requested.set()
            if self.loop is not None and self.task is not None:
                try:
                    self.loop.call_soon_threadsafe(self.task.cancel)
                except RuntimeError:
                    pass

    async def await_analysis(self, factory):
        self.check()
        with self.lock:
            self.loop = asyncio.get_running_loop()
            self.task = asyncio.create_task(factory())
            if self.requested.is_set():
                self.task.cancel()
            task = self.task
        try:
            return await task
        finally:
            with self.lock:
                self.loop = None
                self.task = None


ACTIVE = {}
LOCK = threading.Lock()


def register(identifier, execution):
    with LOCK:
        ACTIVE[identifier] = execution


def discard(identifier, execution):
    with LOCK:
        if ACTIVE.get(identifier) is execution:
            del ACTIVE[identifier]


def find(identifier):
    with LOCK:
        return ACTIVE.get(identifier)
