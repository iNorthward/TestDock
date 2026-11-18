"""Pack-scoped durable jobs, idempotent requests and cooperative cancellation."""
import hashlib
import json
import math
import os
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import Context, ContextVar
from pathlib import Path

from execution_queue import QueueFull
from identity_pool import redact_secrets
from project_paths import runtime_data_path

TERMINAL = ("done", "failed", "incomplete", "skipped", "interrupted", "cancelled", "timed_out")
ACTIVE = ("queued", "running", "cancel_requested")
_CONTROL = ContextVar("execution_control", default=None)
_STORE = None
_EXECUTOR = None
_EXECUTOR_PID = None
_EXECUTOR_LOCK = threading.Lock()
_SHUTTING_DOWN_PID = None


class RequestConflict(ValueError):
    pass


class JobCancelled(RuntimeError):
    pass


class JobTimedOut(RuntimeError):
    pass


def _process_tag(pid):
    try:
        value = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True,
                               text=True, timeout=2, env={**os.environ,"TZ":"UTC","LC_ALL":"C"}).stdout.strip()
        return value or None
    except (OSError, subprocess.SubprocessError):
        return None


def _owner_alive(pid, tag):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    actual = _process_tag(pid)
    return not tag or actual is None or actual == tag


def _json(value):
    return json.dumps(redact_secrets(value), ensure_ascii=False, sort_keys=True, allow_nan=False)


def request_fingerprint(value):
    # Fingerprint all supplied values before redaction; store only the digest.
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def validate_job_id(job_id):
    if not isinstance(job_id, str) or not job_id.strip() or job_id != job_id.strip() or len(job_id) > 128:
        raise ValueError("任务 id 必须是 1 到 128 字符的非空标识")
    return job_id


def _validate_request_key(request_key):
    if (not isinstance(request_key, str) or not request_key.strip()
            or request_key != request_key.strip() or len(request_key) > 128):
        raise ValueError("requestId 必须是 1 到 128 字符的非空标识")


class JobStore:
    def __init__(self, path):
        self.path = Path(path)
        self.pid = os.getpid()
        self.owner_tag = _process_tag(self.pid)

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        self.path.chmod(0o600)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("""CREATE TABLE IF NOT EXISTS execution_jobs (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, request_key TEXT UNIQUE,
                fingerprint TEXT, status TEXT NOT NULL, created_at REAL NOT NULL,
                updated_at REAL NOT NULL, deadline_at REAL NOT NULL,
                owner_pid INTEGER, owner_tag TEXT, metadata_json TEXT NOT NULL,
                progress_json TEXT NOT NULL, result_json TEXT, error TEXT
            )""")
            connection.execute("CREATE INDEX IF NOT EXISTS jobs_updated ON execution_jobs(status,updated_at)")
            connection.execute("""CREATE TABLE IF NOT EXISTS execution_job_tombstones (
                id TEXT PRIMARY KEY, request_key_hash TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                deadline_at REAL NOT NULL, expired_at REAL NOT NULL
            )""")
            # Version 0 stored the independent error column without redaction.
            # Migrate once, under the same write lock used for idempotent create.
            if connection.execute("PRAGMA user_version").fetchone()[0] < 1:
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute("PRAGMA user_version").fetchone()[0] < 1:
                    for row in connection.execute("SELECT id,error FROM execution_jobs WHERE error IS NOT NULL").fetchall():
                        safe_error = redact_secrets(row["error"])
                        if safe_error != row["error"]:
                            connection.execute("UPDATE execution_jobs SET error=? WHERE id=?", (safe_error, row["id"]))
                    connection.execute("PRAGMA user_version=1")
                connection.commit()
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _row(row):
        if row is None:
            return None
        data = dict(row)
        data["meta"] = json.loads(data.pop("metadata_json"))
        progress = json.loads(data.pop("progress_json"))
        data["events"], data["subs"] = progress.get("events", []), progress.get("subs", {})
        result = data.pop("result_json")
        data["result"] = json.loads(result) if result is not None else None
        data["error"] = redact_secrets(data["error"])
        for key in ("request_key", "fingerprint", "owner_pid", "owner_tag"):
            data.pop(key, None)
        return data

    @staticmethod
    def _expired_row(row):
        if row is None:
            return None
        message = "任务结果已过期；本 requestId 不会重放，请先核验业务状态，再决定是否使用新标识执行"
        result = {"ok": None, "status": "incomplete", "executionStatus": "result_expired",
                  "resultExpired": True, "originalExecutionStatus": row["status"], "error": message}
        return {"id": row["id"], "kind": row["kind"], "status": "incomplete",
                "created_at": row["created_at"], "updated_at": row["updated_at"],
                "deadline_at": row["deadline_at"], "expired_at": row["expired_at"],
                "meta": {}, "events": [], "subs": {}, "result": result,
                "resultExpired": True, "error": message}

    def create(self, *, kind, metadata=None, progress=None, request_key=None, fingerprint=None, timeout_seconds=1200):
        if not isinstance(kind, str) or not kind.strip():
            raise ValueError("任务 kind 必须是非空字符串")
        if request_key is not None:
            _validate_request_key(request_key)
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 86400:
            raise ValueError("任务超时必须为 0 到 86400 秒内的有限正数")
        if request_key is not None and (not isinstance(fingerprint, str) or len(fingerprint) != 64):
            raise ValueError("幂等请求须提供参数指纹")
        if metadata is not None and not isinstance(metadata, dict) or progress is not None and not isinstance(progress, dict):
            raise ValueError("任务元数据与进度须为对象")
        encoded_meta, encoded_progress = _json(metadata or {}), _json(progress or {"events": [], "subs": {}})
        now = time.time()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if request_key is not None:
                existing = conn.execute("SELECT * FROM execution_jobs WHERE request_key=?", (request_key,)).fetchone()
                if existing is not None:
                    if existing["kind"] != kind or existing["fingerprint"] != fingerprint:
                        raise RequestConflict("同一 requestId 的操作或参数不同；未重复执行")
                    return self._row(existing), False
                tombstone = conn.execute("SELECT * FROM execution_job_tombstones WHERE request_key_hash=?",
                                         (hashlib.sha256(request_key.encode()).hexdigest(),)).fetchone()
                if tombstone is not None:
                    if tombstone["kind"] != kind or tombstone["fingerprint"] != fingerprint:
                        raise RequestConflict("同一 requestId 的操作或参数不同；原结果已过期，未重复执行")
                    return self._expired_row(tombstone), False
            identifier = uuid.uuid4().hex
            conn.execute("""INSERT INTO execution_jobs
                (id,kind,request_key,fingerprint,status,created_at,updated_at,deadline_at,
                 owner_pid,owner_tag,metadata_json,progress_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (identifier, kind, request_key, fingerprint, "queued", now, now, now+timeout_seconds,
                 self.pid, self.owner_tag, encoded_meta, encoded_progress))
            return self._row(conn.execute("SELECT * FROM execution_jobs WHERE id=?", (identifier,)).fetchone()), True

    def get(self, job_id):
        validate_job_id(job_id)
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM execution_jobs WHERE id=?", (job_id,)).fetchone()
            if row is not None:
                return self._row(row)
            return self._expired_row(conn.execute("SELECT * FROM execution_job_tombstones WHERE id=?", (job_id,)).fetchone())

    def request_receipt(self, request_key, *, kind):
        """Read execution identity and state without opening the store for writes."""
        _validate_request_key(request_key)
        if not isinstance(kind, str) or not kind.strip() or kind != kind.strip() or len(kind) > 128:
            raise ValueError("任务 kind 必须是 1 到 128 字符的非空标识")
        if not self.path.exists():
            return None
        conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            # The active row and its expiry record share one SQLite snapshot.
            conn.execute("BEGIN")
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "execution_jobs" in tables:
                row = conn.execute(
                    "SELECT id, kind, status FROM execution_jobs WHERE request_key=? AND kind=?",
                    (request_key, kind)).fetchone()
                if row is not None:
                    return {**dict(row), "state": "active" if row["status"] in ACTIVE else "terminal",
                            "resultExpired": False}
            if "execution_job_tombstones" in tables:
                row = conn.execute(
                    "SELECT id, kind, status FROM execution_job_tombstones WHERE request_key_hash=? AND kind=?",
                    (hashlib.sha256(request_key.encode()).hexdigest(), kind)).fetchone()
                if row is not None:
                    return {"id": row["id"], "kind": row["kind"], "state": "expired", "status": "incomplete",
                            "resultExpired": True, "originalExecutionStatus": row["status"]}
            return None
        finally:
            conn.close()

    def start(self, job_id):
        with self.connection() as conn:
            changed = conn.execute("UPDATE execution_jobs SET status='running',updated_at=? WHERE id=? AND status='queued'",
                                   (time.time(), job_id)).rowcount
            return bool(changed)

    def progress(self, job_id, update):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT progress_json,status FROM execution_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["status"] not in ACTIVE:
                return
            data = json.loads(row["progress_json"])
            update(data)
            data["events"] = list(data.get("events") or [])[-300:]
            conn.execute("UPDATE execution_jobs SET progress_json=?,updated_at=? WHERE id=?", (_json(data),time.time(),job_id))

    def finish(self, job_id, *, result=None, error=None, status=None):
        if status is None:
            stopped = isinstance(result, dict) and result.get("executionStatus") in ("cancelled", "timed_out")
            failed = bool(error) or isinstance(result, dict) and (
                result.get("ok") is False or result.get("status") == "failed"
                or stopped and bool(result.get("error")))
            status = "failed" if failed else "incomplete"
            if not failed and isinstance(result, dict) and result.get("status") in ("incomplete", "skipped"):
                status = result["status"]
            elif not failed and isinstance(result, dict):
                if result.get("error"):
                    status = "failed"
                    failed = True
                elif result.get("ok") is True or result.get("status") == "passed":
                    status = "done"
            if not failed and isinstance(result,dict) and result.get('executionStatus') in ('cancelled','timed_out') and result.get('ok') is not True:
                status = result['executionStatus']
        if status not in TERMINAL:
            raise ValueError("任务结束状态无效")
        encoded = _json(result) if result is not None else None
        with self.connection() as conn:
            conn.execute("UPDATE execution_jobs SET status=?,result_json=?,error=?,updated_at=? WHERE id=? AND status IN ('queued','running','cancel_requested')",
                         (status,encoded,redact_secrets(str(error)) if error else None,time.time(),job_id))
        self.prune()

    def cancel(self, job_id):
        validate_job_id(job_id)
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status FROM execution_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return self._expired_row(conn.execute("SELECT * FROM execution_job_tombstones WHERE id=?", (job_id,)).fetchone())
            if row["status"] == "queued":
                conn.execute("UPDATE execution_jobs SET status='cancelled',updated_at=? WHERE id=?", (time.time(),job_id))
            elif row["status"] == "running":
                conn.execute("UPDATE execution_jobs SET status='cancel_requested',updated_at=? WHERE id=?", (time.time(),job_id))
        if _EXECUTOR is not None and _EXECUTOR_PID == os.getpid():_EXECUTOR.cancel_queued(job_id)
        return self.get(job_id)

    def control_state(self, job_id):
        with self.connection() as conn:
            row = conn.execute("SELECT status,deadline_at FROM execution_jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row is not None else None

    def recover(self):
        count = 0
        with self.connection() as conn:
            rows = conn.execute("SELECT id,owner_pid,owner_tag FROM execution_jobs WHERE status IN ('queued','running','cancel_requested')").fetchall()
            for row in rows:
                if not _owner_alive(row["owner_pid"], row["owner_tag"]):
                    count += conn.execute("UPDATE execution_jobs SET status='interrupted',error=?,updated_at=? WHERE id=? AND status IN ('queued','running','cancel_requested')",
                                          ("执行进程已结束；任务未自动重放，请核验业务状态",time.time(),row["id"])).rowcount
        return count

    def prune(self, *, retention_seconds=7*86400, maximum_completed=1000):
        if type(retention_seconds) not in (int,float) or not math.isfinite(retention_seconds) or retention_seconds < 86400 or type(maximum_completed) is not int or maximum_completed < 1:
            raise ValueError("任务保留至少 24 小时，完成记录上限须为正整数")
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in TERMINAL)
            # Evict result bodies only; request hashes remain to prevent replay.
            now = time.time()
            expired = conn.execute(f"SELECT id FROM execution_jobs WHERE status IN ({placeholders}) AND updated_at<?",
                                   (*TERMINAL,now-retention_seconds)).fetchall()
            overflow = conn.execute(f"SELECT id FROM execution_jobs WHERE status IN ({placeholders}) ORDER BY updated_at DESC LIMIT -1 OFFSET ?",
                                    (*TERMINAL,maximum_completed)).fetchall()
            fresh = {row[0] for row in conn.execute("SELECT id FROM execution_jobs WHERE updated_at>=?", (now-86400,))}
            ids = {row[0] for row in expired+overflow} - fresh
            for identifier in ids:
                row = conn.execute("SELECT * FROM execution_jobs WHERE id=?", (identifier,)).fetchone()
                if row["request_key"] is not None:
                    conn.execute("""INSERT INTO execution_job_tombstones
                        (id,request_key_hash,kind,fingerprint,status,created_at,updated_at,deadline_at,expired_at)
                        VALUES (?,?,?,?,?,?,?,?,?)""",
                        (row["id"], hashlib.sha256(row["request_key"].encode()).hexdigest(), row["kind"],
                         row["fingerprint"], row["status"], row["created_at"], row["updated_at"], row["deadline_at"], now))
                conn.execute("DELETE FROM execution_jobs WHERE id=?", (identifier,))
            return len(ids)


def get_store():
    global _STORE
    path = runtime_data_path("execution_jobs.db")
    if _STORE is None or _STORE.path != path or _STORE.pid != os.getpid():
        _STORE = JobStore(path)
    return _STORE


def get_executor():
    global _EXECUTOR, _EXECUTOR_PID
    from execution_queue import BoundedExecutor
    from platform_config import project_setting
    with _EXECUTOR_LOCK:
        if _SHUTTING_DOWN_PID == os.getpid():
            raise QueueFull("服务正在等待任务清理，停止接收新的执行")
        if _EXECUTOR is None or _EXECUTOR_PID != os.getpid():
            values=[]
            for key,default,maximum in [('PLATFORM_EXECUTION_WORKERS','2',16),('PLATFORM_EXECUTION_QUEUE_LIMIT','32',1000)]:
                raw=project_setting(key,default=default)
                if not isinstance(raw,str) or not raw.isascii() or not raw.isdecimal() or not 1<=int(raw)<=maximum:raise ValueError(key+' has invalid limit')
                values.append(int(raw))
            _EXECUTOR=BoundedExecutor(*values);_EXECUTOR_PID=os.getpid()
        return _EXECUTOR


def begin_shutdown():
    """Close admission, then ask only this process's jobs to finish cleanup."""
    global _SHUTTING_DOWN_PID
    with _EXECUTOR_LOCK:
        _SHUTTING_DOWN_PID = os.getpid()
        executor = _EXECUTOR if _EXECUTOR_PID == os.getpid() else None
        if executor is not None:
            executor.shutdown(wait=False)
    store = get_store()
    with store.connection() as connection:
        rows = connection.execute(
            "SELECT id FROM execution_jobs WHERE owner_pid=? AND owner_tag IS ? "
            "AND status IN ('queued','running','cancel_requested')",
            (store.pid, store.owner_tag),
        ).fetchall()
    for row in rows:
        store.cancel(row["id"])


def wait_for_shutdown():
    executor = _EXECUTOR if _EXECUTOR_PID == os.getpid() else None
    if executor is not None:
        executor.shutdown(wait=True)


def stopped_result(result, exception):
    """Keep completed assertions/history when a final control check stops a job."""
    result = dict(result) if isinstance(result, dict) else {}
    failed = result.get("ok") is False or result.get("status") == "failed" or bool(result.get("error"))
    result["executionStatus"] = "cancelled" if isinstance(exception, JobCancelled) else "timed_out"
    result["stopReason"] = str(exception)
    if not failed:
        result.update(ok=None, status="incomplete")
    else:
        result.update(ok=False, status="failed")
    return result

def start_background_job(*, kind, request_id=None, payload, operation,
                         metadata=None, progress=None, timeout_seconds=1200):
    """Submit once; detached workers own their context, control and leases.

    operation(job_id, store) must retain its own resource guard until joined
    workers and cleanup finish. Captured options belong in payload/closures;
    parent control and resource leases must never escape into this worker.
    """
    from execution_guard import ExecutionBusy

    store = get_store()
    job, created = store.create(kind=kind, request_key=request_id,
                                fingerprint=request_fingerprint({"payload": payload, "timeoutSeconds": timeout_seconds}),
                                metadata=metadata, progress=progress, timeout_seconds=timeout_seconds)
    identifier = job["id"]
    if not created:
        return identifier

    def run():
        result = None
        try:
            if not store.start(identifier):
                return
            with control_context(identifier, store=store):
                result = operation(identifier, store)
                if (isinstance(result, dict) and result.get('executionStatus') in ('cancelled', 'timed_out')
                        and result.get('ok') is not False and result.get('status') != 'failed' and not result.get('error')):
                    store.finish(identifier, result=result, status=result['executionStatus'])
                    return
                # A cleanup failure has priority over a pending cancellation.
                if not (isinstance(result, dict) and (result.get("ok") is False or result.get("status") == "failed" or result.get("error"))):
                    checkpoint()
            store.finish(identifier, result=result)
        except (JobCancelled, JobTimedOut) as exc:
            result = stopped_result(result, exc)
            store.finish(identifier, result=result,
                         status="failed" if result.get("ok") is False else result["executionStatus"])
        except ExecutionBusy as exc:
            store.finish(identifier, result={"ok": None, "status": "incomplete", "error": str(exc)}, status="incomplete")
        except Exception as exc:
            store.finish(identifier, result={"ok": False, "error": str(exc)}, error=str(exc))

        except BaseException as exc:
            message='Worker exited unexpectedly: '+type(exc).__name__
            store.finish(identifier,result={'ok':False,'status':'failed','error':message},error=message)

    try:
        get_executor().submit(identifier, lambda: Context().run(run))
    except Exception as exc:
        from execution_queue import QueueFull
        if isinstance(exc,QueueFull):
            store.finish(identifier,result={'ok':None,'status':'incomplete','error':str(exc)},status='incomplete')
        else:
            store.finish(identifier,result={'ok':False,'error':str(exc)},error=str(exc))
            raise
    return identifier


def run_idempotent(*, kind, request_id, payload, operation, metadata=None, timeout_seconds=1200):
    """Return the stored result or a pending state; never replay an old request."""
    from execution_queue import QueueFull
    if request_id is None:
        request_id = uuid.uuid4().hex
    store = get_store()
    job, created = store.create(kind=kind, metadata=metadata, request_key=request_id,
                                fingerprint=request_fingerprint(payload), timeout_seconds=timeout_seconds)
    if not created:
        if job["result"] is not None:
            return {**job["result"], "executionId": job["id"], "deduplicated": True}
        return {"ok": None, "status": "incomplete", "executionId": job["id"],
                "executionStatus": job["status"], "pending": job["status"] in ACTIVE,
                "deduplicated": True, "error": job.get("error")}
    if not store.start(job["id"]):
        return {"ok": None, "status": "incomplete", "executionId": job["id"], "executionStatus": "cancelled"}
    result = None
    try:
        with get_executor().synchronous_slot(), control_context(job["id"], store=store):
            result = operation()
            # Match the background worker's final checkpoint, retaining failures.
            if (isinstance(result, dict) and result.get('executionStatus') not in ('cancelled','timed_out')
                    and result.get('ok') is not False and result.get('status') != 'failed' and not result.get('error')):
                checkpoint()
        result = {**result, "executionId": job["id"]}
        store.finish(job["id"], result=result)
        return redact_secrets(result)
    except QueueFull as exc:
        result={'ok':None,'status':'incomplete','executionId':job['id'],'error':str(exc)}
        store.finish(job['id'],result=result,status='incomplete');return result
    except (JobCancelled, JobTimedOut) as exc:
        result = {**stopped_result(result, exc), "executionId": job["id"]}
        status = "failed" if result.get("ok") is False else result["executionStatus"]
        store.finish(job["id"], result=result, status=status)
        return redact_secrets(result)
    except Exception as exc:
        store.finish(job["id"], result={"ok": False, "error": str(exc)}, error=str(exc))
        raise


class JobControl:
    def __init__(self, store, job_id):
        self.store, self.job_id = store, job_id
        state = store.control_state(job_id)
        if state is None:
            raise JobCancelled("任务已不存在，停止执行")
        self.deadline = time.monotonic()+max(0, state["deadline_at"]-time.time())

    def checkpoint(self):
        if _SHUTTING_DOWN_PID == os.getpid():
            raise JobCancelled("服务正在停止，停止新增业务操作并等待清理")
        state = self.store.control_state(self.job_id)
        if state is None or state["status"] not in ("queued", "running"):
            raise JobCancelled("任务收到取消请求，停止新增业务操作")
        if time.monotonic() >= self.deadline:
            raise JobTimedOut("任务超过执行时限，停止新增业务操作")


@contextmanager
def control_context(job_id, *, store=None):
    token = _CONTROL.set(JobControl(store or get_store(), job_id))
    try:
        checkpoint()
        yield
    finally:
        _CONTROL.reset(token)


@contextmanager
def cleanup_context():
    token = _CONTROL.set(None)
    try:
        yield
    finally:
        _CONTROL.reset(token)


def checkpoint():
    control = _CONTROL.get()
    if control is not None:
        control.checkpoint()


def cooperative_sleep(seconds):
    if type(seconds) not in (int,float) or not math.isfinite(seconds) or seconds < 0:
        raise ValueError("等待时间须为有限非负数")
    deadline = time.monotonic()+seconds
    while True:
        checkpoint()
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining,0.25))
