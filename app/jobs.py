"""小规模 MVP 的有界任务执行器；任务状态及事件持久化。"""
import asyncio
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from . import config, store
from .importing.sources import clone, unpack
from .analysis.index import build_index
from .runtime.analysis import analyze
from .analysis.preview import preview
from .analysis import checkpoints
from . import execution

POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="wiki")


def submit(identifier, resume=False):
    control = execution.Execution()
    execution.register(identifier, control)
    if store.get(identifier)["status"] in {"stopping", "stopped"}:
        control.stop()
    try:
        control.future = POOL.submit(run, identifier, resume, control)
        control.future.add_done_callback(lambda future: execution.discard(identifier, control))
    except Exception:
        execution.discard(identifier, control)
        raise
    return control.future


def run(identifier, resume=False, control=None):
    control = control or execution.Execution()
    checkpoint_writer = None
    failure = None
    stopped = False
    job = store.get(identifier)
    if job["status"] in {"stopping", "stopped"}:
        control.stop()
    directory = store.directory(identifier)
    root = directory / "source"
    try:
        control.check()
        if job["kind"] == "local" and not config.developer_enabled():
            raise ValueError("本地目录导入仅允许在开发模式运行")
        indexed = checkpoints.load(identifier, "source-index") if resume else None
        if indexed:
            files, fingerprint, skipped, revision = (indexed[name] for name in ("files", "fingerprint", "skipped", "revision"))
            store.event(identifier, "恢复检查点", max(40, job["progress"]), "复用已保存的源码快照和符号索引，不重新获取仓库")
        else:
            if resume:
                store.event(identifier, "恢复分析", job["progress"], "此任务没有源码检查点，将读取源码开始分析；无法恢复之前的 Agent 上下文")
            store.event(identifier, "获取源码", 5, "开始导入，只读取代码，不执行项目")
            revision = ""
            if root.exists():
                shutil.rmtree(root)
            if job["kind"] == "local":
                if not config.developer_enabled():
                    raise ValueError("本地目录导入仅允许在开发模式运行")
                from .developer import local_directory
                root = local_directory(job["source"])
                store.event(identifier, "获取源码", 18, "已连接本地项目目录，只读建立源码快照")
            elif job["kind"] == "git":
                revision = clone(job["source"], root,
                                 lambda msg: store.event(identifier, "获取源码", 18, msg), check=control.check)
            else:
                root = unpack(directory / "upload.zip", root, check=control.check)
            store.event(identifier, "扫描结构", 22, "识别文件、语言及函数／类定义")
            files, fingerprint, skipped = build_index(root, lambda msg: store.event(identifier, "扫描结构", 30, msg), check=control.check)
            checkpoints.save(identifier, "source-index", {"files": files, "fingerprint": fingerprint, "skipped": skipped, "revision": revision})
        control.check()
        usage = {}
        baseline = min(85, max(40, job["progress"])) if indexed else 40
        if job["mode"] == "agent":
            store.event(identifier, "Agent 分析", baseline, "OpenCode 完整代码智能体开始分析，原生会话持续保存")
            calls = 0
            def log(message):
                nonlocal calls
                control.check()
                calls += 1
                store.event(identifier, "Agent 分析", min(85, baseline + calls // 2), message)
                control.check()
            def action(payload):
                control.check()
                store.event(identifier, "Agent 分析", min(85, baseline + calls // 2), payload["title"], action=payload)
                control.check()
            log.action = action
            state = checkpoints.load(identifier, "agent-state") if resume else None
            if state and state.get("engine") == "opencode":
                store.event(identifier, "恢复检查点", baseline, "恢复 OpenCode 会话与已读源码记录，不重建分区对话")
            if state and state.get("messages"):
                store.event(identifier, "恢复检查点", baseline, f"恢复 {len(state['messages'])} 条框架消息与 {len(state.get('reads', {}))} 个文件的已读区间，本轮重新分配调用预算")
            checkpoint_writer = checkpoints.writer(identifier)
            async def controlled_analysis():
                try:
                    return await control.await_analysis(lambda: asyncio.wait_for(
                        analyze(files, log, state=state, checkpoint=checkpoint_writer), timeout=210))
                except asyncio.TimeoutError as exc:
                    raise TimeoutError("快速概览超过总分析时限（210 秒），已保存会话，可手动继续") from exc
            analysis, usage_info = asyncio.run(controlled_analysis())
            usage = asdict(usage_info)
        else:
            store.event(identifier, "结构预览", 70, "生成文件及符号导航，未调用模型")
            analysis = preview(files)
        control.check()
        store.event(identifier, "校验与生成", 92, "校验引用并生成固定 UI 的项目知识数据")
        groups = []
        for i, group in enumerate(analysis.featureGroups):
            data = group.model_dump()
            data.update(id=f"group-{i}", icon="◇")
            for j, feature in enumerate(data["features"]):
                feature.update(id=f"feature-{i}-{j}", code=f"{i + 1}.{j + 1}")
            groups.append(data)
        symbols = [{**symbol, "path": path, "note": symbol.get("doc", "")} for path, file in files.items() for symbol in file["symbols"]]
        payload = {**analysis.model_dump(), "featureGroups": groups, "files": files, "symbols": symbols,
                   "repoName": job["name"], "generatedAt": store.now(), "fileCount": len(files),
                   "totalLines": sum(file["lines"] for file in files.values()), "snapshot": fingerprint,
                   "revision": revision, "mode": job["mode"], "analysisDepth": "overview" if job["mode"] == "agent" else "structure", "skipped": skipped, "usage": usage}
        control.check()
        store.save_result(identifier, payload)
        control.check()
        store.event(identifier, "分析完成", 100, "结果已保存，可以进入项目阅读页面", status="completed")
    except (execution.TaskStopped, asyncio.CancelledError) as exc:
        stopped = control.requested.is_set() or isinstance(exc, execution.TaskStopped)
        if not stopped:
            failure = "执行被意外中断，可手动继续"
    except Exception as exc:
        stopped = control.requested.is_set()
        if not stopped:
            failure = checkpoints.error_message(exc, checkpoints.load(identifier, "agent-state"))
    finally:
        if checkpoint_writer is not None:
            checkpoint_writer.close()
        control.finished.set()
    if stopped or store.get(identifier)["status"] == "stopping":
        store.event(identifier, "已停止", store.get(identifier)["progress"], "执行已停止，已保存的源码和 Agent 检查点保留", status="stopped")
    elif failure is not None:
        store.event(identifier, "处理失败", store.get(identifier)["progress"], failure, status="failed", error=failure)


def stop(identifier):
    if not store.request_stop(identifier):
        return False
    control = execution.find(identifier)
    if control is not None:
        control.stop()
        if control.future is not None and control.future.cancel():
            control.finished.set()
            store.event(identifier, "已停止", store.get(identifier)["progress"], "排队任务已取消，未开始分析", status="stopped")
    else:
        store.event(identifier, "已停止", store.get(identifier)["progress"], "任务已停止，没有活动执行器", status="stopped")
    return True
