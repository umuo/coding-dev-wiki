"""官方 headless HTTP API 适配；只管理会话和进程，不执行模型循环。"""
import asyncio
import contextlib
import os
import re
import secrets
import signal
import time
import httpx
from .settings import binary, environment, VERSION


class Runtime:
    def __init__(self, directory, workspace, log):
        self.directory, self.workspace, self.log = directory, workspace, log
        self.process = None
        self.client = None
        self.session_id = None

    async def __aenter__(self):
        password = secrets.token_urlsafe(32)
        env = environment(self.directory, password)
        try:
            executable = binary()
            probe = await asyncio.create_subprocess_exec(executable, "--version", env=env,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            try:
                stdout, _ = await asyncio.wait_for(probe.communicate(), 10)
            finally:
                if probe.returncode is None:
                    probe.kill()
                    await probe.wait()
            if stdout.decode().strip() != VERSION:
                raise RuntimeError(f"请使用已验证的 OpenCode {VERSION}，运行 python -m app.runtime.install 安装")
            self.process = await asyncio.create_subprocess_exec(executable, "serve", "--pure", "--hostname", "127.0.0.1", "--port", "0",
                            cwd=self.workspace, env=env, start_new_session=True,
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            async def address():
                while line := await self.process.stdout.readline():
                    found = re.search(rb"http://127\.0\.0\.1:(\d+)", line)
                    if found:
                        return "http://127.0.0.1:" + found[1].decode()
                raise RuntimeError("OpenCode 启动失败，请检查安装及运行目录权限")
            url = await asyncio.wait_for(address(), 30)
            self.client = httpx.AsyncClient(base_url=url, auth=("opencode", password), trust_env=False,
                                           timeout=httpx.Timeout(200, connect=5))
            health = await self.get("/global/health")
            if health.get("version") != VERSION:
                raise RuntimeError("OpenCode 服务版本不一致")
            return self
        except BaseException:
            await self.close()
            raise

    async def __aexit__(self, *args):
        await self.close()

    async def close(self):
        if self.client is not None:
            await self.client.aclose()
        if self.process is not None and self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                await self.process.wait()

    async def get(self, path):
        response = await self.client.get(path)
        response.raise_for_status()
        return response.json()

    async def session(self, previous=None):
        if previous:
            if not re.fullmatch(r"ses_[a-zA-Z0-9]+", previous):
                raise ValueError("无效运行时会话标识")
            response = await self.client.get("/session/" + previous)
            if response.status_code == 200:
                self.session_id = previous
                self.log("恢复 OpenCode 持久会话，不重放旧框架长对话")
                return previous
            if response.status_code != 404:
                response.raise_for_status()
            self.log("原运行时会话已不存在，保留源码和已读记录，创建新会话")
        response = await self.client.post("/session", json={"title": "Project understanding"})
        response.raise_for_status()
        self.session_id = response.json()["id"]
        return self.session_id

    async def abort(self):
        if self.session_id and self.client:
            with contextlib.suppress(Exception):
                await self.client.post("/session/" + self.session_id + "/abort", timeout=5)

    async def messages(self):
        result = await self.get("/session/" + self.session_id + "/message?limit=100")
        if not isinstance(result, list):
            raise RuntimeError("OpenCode 会话消息格式异常")
        return result

    async def prompt(self, text, observe):
        task = asyncio.create_task(self.client.post("/session/" + self.session_id + "/message", json={
            "agent": "wiki", "model": {"providerID": "wiki", "modelID": "project"},
            "parts": [{"type": "text", "text": text}]}))
        started = time.monotonic()
        heartbeat = started
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=1)
                messages = await self.messages()
                observe(messages)
                if time.monotonic() - heartbeat >= 10:
                    statuses = await self.get("/session/status")
                    status = statuses.get(self.session_id, {})
                    if status.get("type") == "retry":
                        self.log("模型接口重试中，当前尝试：" + str(status.get("attempt", "?")))
                        if status.get("attempt", 0) > 1:
                            raise RuntimeError("模型接口连续失败，已停止自动重试；可检查接口后手动继续")
                    self.log(f"OpenCode 正在分析，已等待 {int(time.monotonic() - started)} 秒；可随时停止")
                    heartbeat = time.monotonic()
                if time.monotonic() - started > 300:
                    raise TimeoutError("本轮智能体分析超过 300 秒，已保留会话；可手动继续")
            response = await task
            response.raise_for_status()
            result = response.json()
            observe(await self.messages())
            info = result.get("info", {})
            if info.get("error"):
                error = info["error"]
                raise RuntimeError("OpenCode 模型调用失败：" + str(error.get("data", {}).get("message", error.get("name", "unknown")))[:400])
            if info.get("finish") == "length":
                tokens = info.get("tokens", {})
                raise RuntimeError(f"模型输出达到上限（输出 {tokens.get('output', 0)}，思考 {tokens.get('reasoning', 0)} token），会话已保存；请检查模型的思考模式或输出限制")
            return "".join(part.get("text", "") for part in result.get("parts", []) if part.get("type") == "text")
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await self.abort()
            raise
