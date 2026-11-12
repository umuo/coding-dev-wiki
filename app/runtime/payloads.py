"""保存并按需提供真实工具参数/响应，兼容原生日志回读。"""
import json
import sqlite3
from .. import config, store

MAX_TEXT = 500000
SENSITIVE = {"apikey", "authorization", "accesstoken", "password", "secret", "token"}


def capture(part, workspace):
    secrets = [config.model_config()["api_key"], config.ACCESS_TOKEN]
    prefix = str(workspace.resolve()) + "/"
    def scrub(value):
        if isinstance(value, dict):
            return {key: "[已隐藏]" if str(key).lower().replace("_", "").replace("-", "") in SENSITIVE else scrub(item)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if isinstance(value, str):
            for secret in secrets:
                if secret:
                    value = value.replace(secret, "[已隐藏]")
            return value.replace(prefix, "")
        return value
    state = part.get("state", {})
    response = scrub(state.get("output"))
    error = scrub(state.get("error"))
    if response is not None and not isinstance(response, str):
        response = json.dumps(response, ensure_ascii=False, indent=4)
    if error is not None and not isinstance(error, str):
        error = json.dumps(error, ensure_ascii=False, indent=4)
    return {"tool": part.get("tool", ""), "callId": part.get("callID", part.get("id", "")),
            "status": state.get("status"), "parameters": scrub(state.get("input", {})),
            "response": response[:MAX_TEXT] if response is not None else None,
            "error": error[:MAX_TEXT] if error is not None else None,
            "truncated": any(len(text) > MAX_TEXT for text in (response, error) if text is not None)}


def preview(event):
    action = event.get("action")
    if action and action.get("kind") == "tool":
        return {**event, "action": {key: value for key, value in action.items() if key != "payload"}}
    return event


def load(identifier, sequence):
    with store.connection() as db:
        row = db.execute("SELECT action FROM events WHERE job_id=? AND seq=?", (identifier, sequence)).fetchone()
    action = json.loads(row["action"]) if row and row["action"] else None
    if not action or action.get("kind") != "tool":
        raise KeyError(sequence)
    if action.get("payload") is not None:
        return action["payload"]
    # 旧事件没有保存正文时，只读现存的原生 SQLite，不启动 Agent 或请求模型。
    directory = store.directory(identifier) / "opencode"
    database = directory / "data/opencode/opencode.db"
    if not database.is_file():
        raise KeyError(sequence)
    try:
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=5) as db:
            row = db.execute("SELECT data FROM part WHERE id=?", (action["id"],)).fetchone()
        part = json.loads(row[0]) if row else None
        if part and part.get("type") == "tool":
            return capture(part, directory / "workspace")
    except (sqlite3.DatabaseError, ValueError):
        pass
    raise KeyError(sequence)
