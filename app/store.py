"""SQLite 持久化任务及有序进度，结果绑定不可变源码快照。"""
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from . import config


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connection():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(config.DATA_DIR / "wiki.db", timeout=30) as db:
        db.row_factory = sqlite3.Row
        yield db


def initialize():
    with connection() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, name TEXT, kind TEXT, source TEXT, mode TEXT,
                status TEXT, progress INTEGER, stage TEXT, error TEXT, created TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, at TEXT,
                stage TEXT, progress INTEGER, message TEXT
            );
        ''')
        columns = {row["name"] for row in db.execute("PRAGMA table_info(events)")}
        if "action" not in columns:
            db.execute("ALTER TABLE events ADD COLUMN action TEXT")
        db.execute("CREATE INDEX IF NOT EXISTS events_job_seq ON events(job_id,seq)")
        db.execute("UPDATE jobs SET status='stopped', stage='已停止', error='' WHERE status='stopping'")
        rows = db.execute("SELECT id FROM jobs WHERE status IN ('queued','running')").fetchall()
        for row in rows:
            db.execute("UPDATE jobs SET status='failed', error=?, stage='服务中断' WHERE id=?",
                       ("服务重启导致任务中断，可点击重试", row["id"]))


def create(name, kind, source, mode):
    identifier = uuid.uuid4().hex
    with connection() as db:
        db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (identifier, name, kind, source, mode, "queued", 0, "等待处理", "", now()))
    directory(identifier).mkdir(parents=True)
    return get(identifier)


def directory(identifier):
    if len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier):
        raise KeyError(identifier)
    return config.DATA_DIR / "jobs" / identifier


def get(identifier):
    with connection() as db:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone()
    if row is None:
        raise KeyError(identifier)
    return dict(row)


def listing():
    with connection() as db:
        return [dict(row) for row in db.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT 100")]


def event(identifier, stage, progress, message, status="running", error="", *, action=None):
    with connection() as db:
        if status == "stopped":
            condition = "status IN ('queued','running','stopping')"
        else:
            condition = "status NOT IN ('stopping','stopped')"
        changed = db.execute("UPDATE jobs SET stage=?, progress=?, status=?, error=? WHERE id=? AND " + condition,
                             (stage, progress, status, error, identifier))
        if changed.rowcount == 0:
            return False
        db.execute("INSERT INTO events(job_id,at,stage,progress,message,action) VALUES(?,?,?,?,?,?)",
                   (identifier, now(), stage, progress, message, json.dumps(action, ensure_ascii=False) if action else None))
    return True


def events(identifier, after=0):
    with connection() as db:
        rows = [dict(row) for row in db.execute(
            "SELECT * FROM events WHERE job_id=? AND seq>? ORDER BY seq LIMIT 500", (identifier, after))]
    for row in rows:
        row["action"] = json.loads(row["action"]) if row["action"] else None
    return rows


def save_result(identifier, payload):
    target = directory(identifier) / "result.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(target)


def result(identifier):
    return json.loads((directory(identifier) / "result.json").read_text(encoding="utf-8"))


def claim_continuation(identifier):
    """原子抢占失败任务，避免双击产生两个恢复执行器。"""
    with connection() as db:
        cursor = db.execute("UPDATE jobs SET status='queued', stage='等待继续', error='' WHERE id=? AND status IN ('failed','stopped') AND mode='agent'", (identifier,))
        return cursor.rowcount == 1


def request_stop(identifier):
    with connection() as db:
        changed = db.execute("UPDATE jobs SET status='stopping', stage='正在停止', error='' WHERE id=? AND status IN ('queued','running')", (identifier,))
        if changed.rowcount:
            progress = db.execute("SELECT progress FROM jobs WHERE id=?", (identifier,)).fetchone()[0]
            db.execute("INSERT INTO events(job_id,at,stage,progress,message) VALUES(?,?,?,?,?)",
                       (identifier, now(), "正在停止", progress, "已收到停止请求，正在终止执行并保存检查点"))
            return True
        row = db.execute("SELECT status FROM jobs WHERE id=?", (identifier,)).fetchone()
        return row is not None and row["status"] in {"stopping", "stopped"}
