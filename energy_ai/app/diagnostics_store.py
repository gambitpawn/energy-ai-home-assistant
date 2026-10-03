from __future__ import annotations

import json
import sqlite3
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any
from uuid import uuid4

from . import db as db_module
from .db import connect_db

RETENTION_DAYS = 90
_CURRENT_CONTROL_RUN_ID: ContextVar[str | None] = ContextVar("energy_ai_control_run_id", default=None)
_TABLES_LOCK = RLock()
_TABLES_INITIALIZED_PATH: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cutoff() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()


def _init_tables() -> None:
    global _TABLES_INITIALIZED_PATH
    path = str(db_module.DB_PATH)
    with _TABLES_LOCK:
        if _TABLES_INITIALIZED_PATH == path:
            return
        with connect_db(timeout=1.0) as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS diagnostic_control_run(
                    run_id TEXT PRIMARY KEY,
                    trigger TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    decision_start TEXT,
                    plan_generated_at TEXT,
                    information_vintage_id TEXT,
                    routed_engine_id TEXT,
                    candidate_valid_until TEXT,
                    requested_action_kw REAL,
                    actuation_status TEXT,
                    error TEXT,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_diagnostic_control_run_started
                    ON diagnostic_control_run(started_at DESC);
                CREATE INDEX IF NOT EXISTS idx_diagnostic_control_run_decision_start
                    ON diagnostic_control_run(decision_start);

                CREATE TABLE IF NOT EXISTS diagnostic_control_event(
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_diagnostic_control_event_run
                    ON diagnostic_control_event(run_id,event_id);

                CREATE TABLE IF NOT EXISTS diagnostic_maintenance_run(
                    job_id TEXT PRIMARY KEY,
                    lane TEXT NOT NULL,
                    label TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    timeout_seconds REAL,
                    worker_pid INTEGER,
                    restart_count INTEGER,
                    duration_seconds REAL,
                    error TEXT,
                    traceback TEXT,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_diagnostic_maintenance_started
                    ON diagnostic_maintenance_run(started_at DESC);
                CREATE INDEX IF NOT EXISTS idx_diagnostic_maintenance_label
                    ON diagnostic_maintenance_run(label,started_at DESC);
                """
            )
        _TABLES_INITIALIZED_PATH = path

def new_control_run_id() -> str:
    return uuid4().hex


def set_current_control_run(run_id: str | None) -> None:
    _CURRENT_CONTROL_RUN_ID.set(run_id)


def current_control_run_id() -> str | None:
    return _CURRENT_CONTROL_RUN_ID.get()


def start_control_run(run_id: str, *, trigger: str, stage: str = "quarter_start", payload: dict[str, Any] | None = None) -> bool:
    try:
        _init_tables()
        now = _now()
        with connect_db(timeout=0.25) as c:
            c.execute(
                """INSERT OR REPLACE INTO diagnostic_control_run(
                   run_id,trigger,started_at,completed_at,status,stage,payload_json)
                   VALUES (?,?,?,?,?,?,?)""",
                (str(run_id), str(trigger), now, None, "running", str(stage), json.dumps(payload or {}, ensure_ascii=False, default=str)),
            )
            c.execute(
                "INSERT INTO diagnostic_control_event(run_id,created_at,stage,status,payload_json) VALUES (?,?,?,?,?)",
                (str(run_id), now, str(stage), "running", json.dumps(payload or {}, ensure_ascii=False, default=str)),
            )
            c.execute("DELETE FROM diagnostic_control_event WHERE created_at < ?", (_cutoff(),))
            c.execute("DELETE FROM diagnostic_control_run WHERE started_at < ?", (_cutoff(),))
        return True
    except Exception:
        return False


def checkpoint_control_run(
    run_id: str | None,
    *,
    stage: str,
    status: str = "running",
    payload: dict[str, Any] | None = None,
    decision_start: str | None = None,
    plan_generated_at: str | None = None,
    information_vintage_id: str | None = None,
    routed_engine_id: str | None = None,
    candidate_valid_until: str | None = None,
    requested_action_kw: float | None = None,
    actuation_status: str | None = None,
    error: str | None = None,
    completed: bool = False,
) -> bool:
    if not run_id:
        return False
    try:
        _init_tables()
        now = _now()
        with connect_db(timeout=0.25) as c:
            c.execute(
                """UPDATE diagnostic_control_run SET
                   completed_at=CASE WHEN ? THEN ? ELSE completed_at END,
                   status=?, stage=?,
                   decision_start=COALESCE(?,decision_start),
                   plan_generated_at=COALESCE(?,plan_generated_at),
                   information_vintage_id=COALESCE(?,information_vintage_id),
                   routed_engine_id=COALESCE(?,routed_engine_id),
                   candidate_valid_until=COALESCE(?,candidate_valid_until),
                   requested_action_kw=COALESCE(?,requested_action_kw),
                   actuation_status=COALESCE(?,actuation_status),
                   error=COALESCE(?,error),
                   payload_json=?
                   WHERE run_id=?""",
                (
                    1 if completed else 0, now, str(status), str(stage),
                    decision_start, plan_generated_at, information_vintage_id,
                    routed_engine_id, candidate_valid_until, requested_action_kw,
                    actuation_status, error,
                    json.dumps(payload or {}, ensure_ascii=False, default=str),
                    str(run_id),
                ),
            )
            c.execute(
                "INSERT INTO diagnostic_control_event(run_id,created_at,stage,status,payload_json) VALUES (?,?,?,?,?)",
                (str(run_id), now, str(stage), str(status), json.dumps(payload or {}, ensure_ascii=False, default=str)),
            )
        return True
    except Exception:
        return False


def start_maintenance_run(
    job_id: str,
    *,
    lane: str,
    label: str,
    timeout_seconds: float,
    payload: dict[str, Any] | None = None,
) -> bool:
    try:
        _init_tables()
        now = _now()
        with connect_db(timeout=0.5) as c:
            c.execute(
                """INSERT OR REPLACE INTO diagnostic_maintenance_run(
                   job_id,lane,label,started_at,completed_at,status,timeout_seconds,
                   worker_pid,restart_count,duration_seconds,error,traceback,payload_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(job_id), str(lane), str(label), now, None, "running",
                    float(timeout_seconds), None, None, None, None, None,
                    json.dumps(payload or {}, ensure_ascii=False, default=str),
                ),
            )
            c.execute("DELETE FROM diagnostic_maintenance_run WHERE started_at < ?", (_cutoff(),))
        return True
    except Exception:
        return False


def finish_maintenance_run(
    job_id: str,
    *,
    status: str,
    worker_pid: int | None = None,
    restart_count: int | None = None,
    duration_seconds: float | None = None,
    error: str | None = None,
    traceback_text: str | None = None,
    payload: dict[str, Any] | None = None,
) -> bool:
    try:
        _init_tables()
        with connect_db(timeout=0.5) as c:
            c.execute(
                """UPDATE diagnostic_maintenance_run SET
                   completed_at=?,status=?,worker_pid=?,restart_count=?,duration_seconds=?,
                   error=?,traceback=?,payload_json=?
                   WHERE job_id=?""",
                (
                    _now(), str(status), worker_pid, restart_count, duration_seconds,
                    error, traceback_text,
                    json.dumps(payload or {}, ensure_ascii=False, default=str),
                    str(job_id),
                ),
            )
        return True
    except Exception:
        return False


def control_history(*, start: str | None = None, end: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    _init_tables()
    clauses: list[str] = []
    params: list[Any] = []
    if start:
        clauses.append("started_at>=?")
        params.append(str(start))
    if end:
        clauses.append("started_at<=?")
        params.append(str(end))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    lim = max(1, min(int(limit), 1000))
    with connect_db(timeout=5.0) as c:
        rows = c.execute(
            f"""SELECT run_id,trigger,started_at,completed_at,status,stage,decision_start,
                       plan_generated_at,information_vintage_id,routed_engine_id,
                       candidate_valid_until,requested_action_kw,actuation_status,error,payload_json
                FROM diagnostic_control_run{where}
                ORDER BY started_at DESC LIMIT ?""",
            (*params, lim),
        ).fetchall()
        events_by_run: dict[str, list[dict[str, Any]]] = {}
        if rows:
            ids = [str(r[0]) for r in rows]
            marks = ",".join("?" for _ in ids)
            for event_id, run_id, created_at, stage, status, payload_json in c.execute(
                f"""SELECT event_id,run_id,created_at,stage,status,payload_json
                    FROM diagnostic_control_event
                    WHERE run_id IN ({marks})
                    ORDER BY event_id""",
                ids,
            ).fetchall():
                events_by_run.setdefault(str(run_id), []).append({
                    "event_id": event_id,
                    "created_at": created_at,
                    "stage": stage,
                    "status": status,
                    "payload": json.loads(payload_json or "{}"),
                })
    result = []
    for r in rows:
        result.append({
            "run_id": r[0], "trigger": r[1], "started_at": r[2], "completed_at": r[3],
            "status": r[4], "stage": r[5], "decision_start": r[6],
            "plan_generated_at": r[7], "information_vintage_id": r[8],
            "routed_engine_id": r[9], "candidate_valid_until": r[10],
            "requested_action_kw": r[11], "actuation_status": r[12], "error": r[13],
            "payload": json.loads(r[14] or "{}"),
            "events": events_by_run.get(str(r[0]), []),
        })
    return result


def maintenance_history(
    *,
    start: str | None = None,
    end: str | None = None,
    lane: str | None = None,
    label: str | None = None,
    status: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    _init_tables()
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (("started_at", start),):
        if value:
            clauses.append(f"{column}>=?")
            params.append(str(value))
    if end:
        clauses.append("started_at<=?")
        params.append(str(end))
    if lane:
        clauses.append("lane=?")
        params.append(str(lane))
    if label:
        clauses.append("label=?")
        params.append(str(label))
    if status:
        clauses.append("status=?")
        params.append(str(status))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    lim = max(1, min(int(limit), 1000))
    with connect_db(timeout=5.0) as c:
        rows = c.execute(
            f"""SELECT job_id,lane,label,started_at,completed_at,status,timeout_seconds,
                       worker_pid,restart_count,duration_seconds,error,traceback,payload_json
                FROM diagnostic_maintenance_run{where}
                ORDER BY started_at DESC LIMIT ?""",
            (*params, lim),
        ).fetchall()
    return [{
        "job_id": r[0], "lane": r[1], "label": r[2], "started_at": r[3],
        "completed_at": r[4], "status": r[5], "timeout_seconds": r[6],
        "worker_pid": r[7], "restart_count": r[8], "duration_seconds": r[9],
        "error": r[10], "traceback": r[11], "payload": json.loads(r[12] or "{}"),
    } for r in rows]


def diagnostic_window(*, start: str, end: str, limit: int = 500) -> dict[str, Any]:
    _init_tables()
    lim = max(1, min(int(limit), 1000))
    with connect_db(timeout=5.0) as c:
        optimizer_plans = [
            {"generated_at": r[0], "planner": r[1], "horizon_hours": r[2]}
            for r in c.execute(
                """SELECT generated_at,planner,horizon_hours
                   FROM optimizer_plan_summary
                   WHERE generated_at>=? AND generated_at<=?
                   ORDER BY generated_at""",
                (start, end),
            ).fetchall()
        ]
        vintages = [
            {"information_vintage_id": r[0], "generated_at": r[1], "decision_start": r[2], "initial_soc_pct": r[3]}
            for r in c.execute(
                """SELECT information_vintage_id,generated_at,decision_start,initial_soc_pct
                   FROM engine_information_vintage
                   WHERE decision_start>=? AND decision_start<=?
                   ORDER BY decision_start,generated_at""",
                (start, end),
            ).fetchall()
        ]
        decisions = [
            {"decision_id": r[0], "information_vintage_id": r[1], "generated_at": r[2], "decision_start": r[3],
             "engine_id": r[4], "status": r[5], "requested_action_kw": r[6]}
            for r in c.execute(
                """SELECT decision_id,information_vintage_id,generated_at,decision_start,
                          engine_id,status,requested_action_kw
                   FROM engine_decision
                   WHERE decision_start>=? AND decision_start<=?
                   ORDER BY decision_start,engine_id""",
                (start, end),
            ).fetchall()
        ]
        selections = [
            {"information_vintage_id": r[0], "decision_start": r[1], "created_at": r[2],
             "configured_selected_engine_id": r[3], "routed_engine_id": r[4],
             "decision_id": r[5], "requested_action_kw": r[6], "fallback_used": bool(r[7]), "reason": r[8]}
            for r in c.execute(
                """SELECT information_vintage_id,decision_start,created_at,configured_selected_engine_id,
                          routed_engine_id,decision_id,requested_action_kw,fallback_used,reason
                   FROM engine_control_selection
                   WHERE decision_start>=? AND decision_start<=?
                   ORDER BY decision_start,created_at""",
                (start, end),
            ).fetchall()
        ]
        actuator_commands = [
            {"command_id": r[0], "created_at": r[1], "source": r[2], "engine_id": r[3],
             "decision_start": r[4], "valid_until": r[5], "requested_action_kw": r[6],
             "safe_action_kw": r[7], "physical_write": bool(r[8]), "status": r[9], "reason": r[10]}
            for r in c.execute(
                """SELECT command_id,created_at,source,engine_id,decision_start,valid_until,
                          requested_action_kw,safe_action_kw,physical_write,status,reason
                   FROM actuator_command
                   WHERE created_at>=? AND created_at<=?
                   ORDER BY command_id LIMIT ?""",
                (start, end, lim),
            ).fetchall()
        ]
        actuator_events = [
            {"event_id": r[0], "created_at": r[1], "event_type": r[2], "reason": r[3],
             "payload": json.loads(r[4] or "{}")}
            for r in c.execute(
                """SELECT event_id,created_at,event_type,reason,payload_json
                   FROM actuator_event
                   WHERE created_at>=? AND created_at<=?
                   ORDER BY event_id LIMIT ?""",
                (start, end, lim),
            ).fetchall()
        ]
    return {
        "start": start,
        "end": end,
        "control_runs": control_history(start=start, end=end, limit=lim),
        "maintenance_runs": maintenance_history(start=start, end=end, limit=lim),
        "optimizer_plans": optimizer_plans,
        "engine_information_vintages": vintages,
        "engine_decisions": decisions,
        "engine_control_selections": selections,
        "actuator_commands": actuator_commands,
        "actuator_events": actuator_events,
    }
