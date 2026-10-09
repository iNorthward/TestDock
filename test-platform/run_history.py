# -*- coding: utf-8 -*-
"""测试结果落库（SQLite）：run history + flaky 检测。

- 库文件：当前 Pack 的数据目录/run_history.db（gitignore）
- 表结构：
    runs(id, ts, mode, suite, source, passed, failed, skipped, incomplete, total, status, duration_ms)
    case_results(id, run_id, case_id, ok, status, subs_json, ts)
- 入口：
    record_run(result, source, duration_ms)  # result 为 catalog.run_all 的返回
    record_case(case_result, source)         # 单条用例（面板"运行"按钮）
    recent_runs(limit) / run_detail(run_id)
    flaky_cases(window) / case_history(case_id, limit)
"""
import json
import sqlite3
import threading
import time
from case_result_status import normalize_case_result, summarize_cases
from project_paths import runtime_data_path
from identity_pool import redact_secrets

DB_PATH = runtime_data_path("run_history.db")
_LOCK = threading.Lock()
_SCHEMA_LOCK = threading.Lock()
MAX_HISTORY_LIMIT = 500
MAX_HISTORY_WINDOW = 500
MAX_SUMMARY_DAYS = 3650

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    mode TEXT NOT NULL,
    suite TEXT,
    source TEXT NOT NULL,
    passed INTEGER NOT NULL,
    failed INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    incomplete INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'failed',
    duration_ms INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS case_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    case_id TEXT NOT NULL,
    ok INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'failed',
    subs_json TEXT,
    ts INTEGER NOT NULL,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_case_results_case ON case_results(case_id, ts);
CREATE INDEX IF NOT EXISTS idx_case_results_run ON case_results(run_id);
"""


def _migrate(conn):
    run_cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
    case_cols = {r["name"] for r in conn.execute("PRAGMA table_info(case_results)")}
    if case_cols and "duration_ms" not in case_cols:
        conn.execute("ALTER TABLE case_results ADD COLUMN duration_ms INTEGER")
    if run_cols:
        for name, definition in (
            ("failed", "INTEGER NOT NULL DEFAULT 0"),
            ("skipped", "INTEGER NOT NULL DEFAULT 0"),
            ("incomplete", "INTEGER NOT NULL DEFAULT 0"),
            ("status", "TEXT NOT NULL DEFAULT 'legacy'"),
            ("metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
        ):
            if name not in run_cols:
                conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}")
    if case_cols and "status" not in case_cols:
        conn.execute("ALTER TABLE case_results ADD COLUMN status TEXT NOT NULL DEFAULT 'legacy'")

    # Reclassify old binary history using the assertion details already stored.
    legacy_cases = conn.execute(
        "SELECT id, case_id, ok, subs_json FROM case_results WHERE status='legacy'"
    ).fetchall()
    for row in legacy_cases:
        try:
            subs = json.loads(row["subs_json"] or "[]")
        except (TypeError, ValueError):
            subs = []
        normalized = normalize_case_result({
            "id": row["case_id"], "ok": bool(row["ok"]), "subs": subs,
        })
        status = normalized["status"]
        conn.execute(
            "UPDATE case_results SET status=?, ok=? WHERE id=?",
            (status, 1 if status == "passed" else 0, row["id"]),
        )

    legacy_runs = conn.execute("SELECT id, passed, total FROM runs WHERE status='legacy'").fetchall()
    for row in legacy_runs:
        statuses = [r["status"] for r in conn.execute(
            "SELECT status FROM case_results WHERE run_id=?", (row["id"],)
        ).fetchall()]
        counts = {status: statuses.count(status) for status in
                  ("passed", "failed", "skipped", "incomplete")}
        missing = max(0, int(row["total"]) - len(statuses))
        counts["incomplete"] += missing
        total = max(int(row["total"]), len(statuses))
        run_status = ("failed" if counts["failed"] else
                      "incomplete" if counts["skipped"] or counts["incomplete"] or not total else
                      "passed")
        conn.execute(
            """UPDATE runs SET passed=?, failed=?, skipped=?, incomplete=?, total=?, status=?
               WHERE id=?""",
            (counts["passed"], counts["failed"], counts["skipped"],
             counts["incomplete"], total, run_status, row["id"]),
        )


def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        with _SCHEMA_LOCK:
            # Serialize schema inspection and ALTER TABLE across processes.
            # executescript commits before running, so begin the transaction
            # inside the script and commit only after migration completes.
            conn.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA)
            _migrate(conn)
            conn.commit()
    except Exception:
        conn.close()
        raise
    return conn


def validate_history_parameter(value, *, name, maximum):
    """Validate bounded positive history query parameters before constructing SQL."""
    if type(value) is int:
        parsed = value
    elif (isinstance(value, str) and 1 <= len(value) <= 10
          and value.isascii() and value.isdecimal()):
        parsed = int(value)
    else:
        raise ValueError(f"{name} 必须是 1 到 {maximum} 的十进制整数")
    if not 1 <= parsed <= maximum:
        raise ValueError(f"{name} 必须是 1 到 {maximum} 的十进制整数")
    return parsed


def _stored_case(case_result):
    """规范化持久化结果，确保执行异常不会变成通过或丢失详情。"""
    case_result = dict(case_result) if isinstance(case_result, dict) else case_result
    if not isinstance(case_result, dict):
        return normalize_case_result(case_result)
    error = case_result.get("error")
    if error:
        case_result["ok"] = False
        raw_subs = case_result.get("subs")
        subs = list(raw_subs) if isinstance(raw_subs, list) else []
        if not any(
            isinstance(sub, dict) and sub.get("label") == "执行异常"
            and sub.get("detail") == str(error)
            for sub in subs
        ):
            subs.append({"label": "执行异常", "ok": False, "detail": str(error)})
        case_result["subs"] = subs
    return normalize_case_result(case_result)


def record_run(result, source="panel", duration_ms=None, metadata=None):
    """落库一次批量执行（result 为 catalog.run_all 返回结构）。返回 run_id。"""
    cases = [_stored_case(case_result) for case_result in (result.get("cases") or [])]
    execution_metadata = metadata if metadata is not None else result.get("executionMetadata")
    if execution_metadata is None:
        from execution_provenance import execution_metadata as capture_metadata
        execution_metadata = capture_metadata({"mode": result.get("mode", "safe"),
                                               "suite": result.get("suite"), "source": source})
    if not isinstance(execution_metadata, dict):
        raise ValueError("执行元数据须为对象")
    encoded_metadata = json.dumps(redact_secrets(execution_metadata), ensure_ascii=False, allow_nan=False)
    now = int(time.time())
    counts = summarize_cases(cases)
    with _LOCK, _conn() as conn:
        cur = conn.execute(
            """INSERT INTO runs(ts, mode, suite, source, passed, failed, skipped,
               incomplete, total, status, duration_ms, metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now, result.get("mode", "safe"), result.get("suite"), source,
             counts["passed"], counts["failed"], counts["skipped"], counts["incomplete"],
             counts["total"], counts["status"], duration_ms,
             encoded_metadata),
        )
        run_id = cur.lastrowid
        conn.executemany(
            """INSERT INTO case_results(run_id, case_id, ok, status, subs_json, ts, duration_ms)
               VALUES (?,?,?,?,?,?,?)""",
            [
                (run_id, c.get("id", "?"), 1 if c.get("status") == "passed" else 0,
                 c.get("status", "failed"),
                 json.dumps(c.get("subs") or [], ensure_ascii=False), now,
                 c.get("durationMs"))
                for c in cases
            ],
        )
    return run_id


def record_case(case_result, source="panel", mode="safe", duration_ms=None, metadata=None):
    """落库单条用例执行，包装成 total=1 的 run。"""
    normalized = _stored_case(case_result)
    ok = 1 if normalized.get("status") == "passed" else 0
    return record_run(
        {"mode": mode, "suite": normalized.get("suite"), "cases": [normalized],
         "passed": ok, "total": 1},
        source=source, duration_ms=duration_ms,
        metadata=metadata if metadata is not None else case_result.get("executionMetadata"),
    )


def recent_runs(limit=50):
    limit = validate_history_parameter(limit, name="limit", maximum=MAX_HISTORY_LIMIT)
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_run_row(r) for r in rows]


def _run_row(row):
    out = dict(row)
    out["executionMetadata"] = json.loads(out.pop("metadata_json", "{}"))
    return out


def run_detail(run_id):
    with _conn() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id=?", (int(run_id),)).fetchone()
        if not run:
            return None
        cases = conn.execute(
            "SELECT case_id, ok, status, subs_json, duration_ms FROM case_results WHERE run_id=? ORDER BY id",
            (int(run_id),),
        ).fetchall()
    out = _run_row(run)
    out["cases"] = [
        {"id": c["case_id"], "ok": c["status"] == "passed", "status": c["status"],
         "subs": json.loads(c["subs_json"] or "[]"),
         "durationMs": c["duration_ms"]}
        for c in cases
    ]
    return out


def case_history(case_id, limit=20):
    limit = validate_history_parameter(limit, name="limit", maximum=MAX_HISTORY_LIMIT)
    with _conn() as conn:
        rows = conn.execute(
            """SELECT cr.ts, cr.ok, cr.status, cr.subs_json, cr.duration_ms, r.source, r.mode, r.metadata_json
               FROM case_results cr JOIN runs r ON r.id = cr.run_id
               WHERE cr.case_id=? ORDER BY cr.id DESC LIMIT ?""",
            (case_id, limit),
        ).fetchall()
    return [
        {"ts": r["ts"], "ok": r["status"] == "passed", "status": r["status"],
         "source": r["source"], "mode": r["mode"],
         "durationMs": r["duration_ms"], "subs": json.loads(r["subs_json"] or "[]"),
         "executionMetadata": json.loads(r["metadata_json"] or "{}")}
        for r in rows
    ]


def case_badges(window=10):
    """每个 case_id 的最近一次结果 + 近 window 次通过率（面板用例表徽标用）。"""
    window = validate_history_parameter(window, name="window", maximum=MAX_HISTORY_WINDOW)
    with _conn() as conn:
        rows = conn.execute(
            """SELECT case_id, ok, status, ts, duration_ms, rn FROM (
                   SELECT case_id, ok, status, ts, duration_ms,
                          ROW_NUMBER() OVER (PARTITION BY case_id ORDER BY id DESC) AS rn
                   FROM case_results
               ) WHERE rn <= ?""",
            (window,),
        ).fetchall()
    out = {}
    for r in rows:
        b = out.setdefault(r["case_id"], {
            "ok": None, "status": None, "ts": None, "durationMs": None,
            "passed": 0, "failed": 0, "skipped": 0, "incomplete": 0,
            "total": 0, "evaluated": 0,
        })
        if r["rn"] == 1:
            b["ok"] = True if r["status"] == "passed" else False if r["status"] == "failed" else None
            b["status"] = r["status"]
            b["ts"] = r["ts"]
            b["durationMs"] = r["duration_ms"]
        b["total"] += 1
        b[r["status"]] += 1
        b["evaluated"] += r["status"] in ("passed", "failed")
    return out


def flaky_cases(window=10, min_samples=3):
    """近 window 次执行内既有通过又有失败的用例 → flaky 候选。"""
    window = validate_history_parameter(window, name="window", maximum=MAX_HISTORY_WINDOW)
    min_samples = validate_history_parameter(
        min_samples, name="min_samples", maximum=MAX_HISTORY_WINDOW,
    )
    with _conn() as conn:
        rows = conn.execute(
            """SELECT case_id, status FROM (
                   SELECT case_id, status,
                          ROW_NUMBER() OVER (PARTITION BY case_id ORDER BY id DESC) AS rn
                   FROM case_results
               ) WHERE rn <= ? AND status IN ('passed', 'failed')""",
            (window,),
        ).fetchall()
    stats = {}
    for r in rows:
        s = stats.setdefault(r["case_id"], {"total": 0, "passed": 0})
        s["total"] += 1
        s["passed"] += r["status"] == "passed"
    out = []
    for cid, s in stats.items():
        if s["total"] >= min_samples and 0 < s["passed"] < s["total"]:
            out.append({
                "case_id": cid, "samples": s["total"], "passed": s["passed"],
                "pass_rate": round(s["passed"] / s["total"], 3),
            })
    out.sort(key=lambda x: x["pass_rate"])
    return out


def summary(days=30):
    """近 N 天每日汇总（趋势图数据源）。"""
    days = validate_history_parameter(days, name="days", maximum=MAX_SUMMARY_DAYS)
    since = int(time.time()) - days * 86400
    with _conn() as conn:
        rows = conn.execute(
            """SELECT date(ts, 'unixepoch', 'localtime') AS day,
                      COUNT(*) AS runs, SUM(passed) AS passed, SUM(failed) AS failed,
                      SUM(skipped) AS skipped, SUM(incomplete) AS incomplete, SUM(total) AS total
               FROM runs WHERE ts >= ? GROUP BY day ORDER BY day""",
            (since,),
        ).fetchall()
    return [dict(r) for r in rows]
