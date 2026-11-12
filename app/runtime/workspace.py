"""从不可变索引建立只读源码目录，不使用上传项目的运行配置。"""
import hashlib
import json
import subprocess
from pathlib import Path


def materialize(directory, files):
    workspace = (directory / "workspace").resolve()
    repo = workspace / "repo"
    fingerprint = hashlib.sha256(json.dumps({key: value["content"] for key, value in sorted(files.items())},
                                          ensure_ascii=False).encode()).hexdigest()
    manifest = directory / "snapshot.sha256"
    if manifest.exists():
        if manifest.read_text() != fingerprint:
            raise ValueError("运行时源码与当前快照不一致，拒绝复用会话")
        if repo.is_dir():
            return workspace
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-c", "init.templateDir=", "init", "--quiet", str(workspace)],
                   check=True, timeout=10, capture_output=True)
    for name, file in files.items():
        target = repo.joinpath(name).resolve()
        if not target.is_relative_to(repo) or target == repo:
            raise ValueError("源码快照包含越界路径")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(file["content"], encoding="utf-8")
        target.chmod(0o444)
    manifest.write_text(fingerprint)
    return workspace


def collect_reads(messages, workspace, files, reads, seen, log):
    repo = (workspace / "repo").resolve()
    for message in messages:
        for part in message.get("parts", []):
            state = part.get("state", {})
            identity = part.get("id")
            if part.get("type") != "tool" or state.get("status") != "completed" or identity in seen:
                continue
            seen.add(identity)
            if part.get("tool") != "read":
                log("OpenCode 工具完成：" + part.get("tool", "unknown"))
                continue
            path = Path(state.get("input", {}).get("filePath", ""))
            path = path.resolve() if path.is_absolute() else (workspace / path).resolve()
            if not path.is_relative_to(repo):
                continue
            name = path.relative_to(repo).as_posix()
            if name not in files:
                continue
            # 只信运行时实际返回的行号与内容；输入 limit 不能当成已读证据。
            lines = state.get("output", "").split("<content>", 1)[-1].split("</content>", 1)[0].splitlines()
            actual = []
            source = files[name]["content"].splitlines()
            for line in lines:
                number, separator, content = line.partition(": ")
                if separator and number.isdigit():
                    number = int(number)
                    if 1 <= number <= len(source) and content == source[number - 1]:
                        actual.append(number)
            if actual:
                # 单行裁剪或有缺口时分开记区间，避免把没返回的行算作读过。
                begin = previous = actual[0]
                for number in actual[1:] + [None]:
                    if number != previous + 1:
                        reads.setdefault(name, []).append((begin, previous))
                        if number is None:
                            break
                        begin = number
                    previous = number
                log(f"阅读 {name}:{min(actual)}-{max(actual)}")
