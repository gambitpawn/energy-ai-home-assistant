from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import db

# Retention is deliberately not run during startup, collection, or planning.
# Window protects the 14-day optimizer evaluation and its retry margin.
RETENTION_DAYS = {
    "raw_state": 30,
    "optimizer_plan": 16,
    "optimizer_plan_summary": 30,
    "engine_decision": 30,
    "engine_information_vintage": 30,
    "pv_forecast_15m": 16,
    "load_forecast_15m": 16,
    "pv_remaining_day_forecast": 16,
}
TABLE_DATE_COLUMNS = {
    "raw_state": "collected_at",
    "optimizer_plan": "generated_at",
    "optimizer_plan_summary": "generated_at",
    "engine_decision": "generated_at",
    "engine_information_vintage": "generated_at",
    "pv_forecast_15m": "generated_at",
    "load_forecast_15m": "generated_at",
    "pv_remaining_day_forecast": "generated_at",
}
# No automatic VACUUM/REINDEX; both can be expensive and require extra disk.
# These tables are intentionally excluded: learned parameters, samples, daily
# evaluations, state_15m, operating mode, and all actuator/safety records.
BATCH_SIZE = 256
STATE_NAME = "retention_status.json"
_STOCKHOLM = ZoneInfo("Europe/Stockholm")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _status_path() -> Path:
    return db.DB_PATH.parent / STATE_NAME


def read_retention_status() -> dict:
    try:
        value = json.loads(_status_path().read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return value
    except (OSError, ValueError, TypeError):
        pass
    return {"last_run": None, "policy": RETENTION_DAYS, "automatic_vacuum": False}


def _save_status(report: dict) -> None:
    path = _status_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _older_than(value: object, cutoff: datetime) -> bool:
    """Fail closed on invalid or timezone-naive timestamps."""
    if not isinstance(value, str):
        return False
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.tzinfo is not None and stamp.astimezone(timezone.utc) < cutoff
    except ValueError:
        return False


def _safe_raw_row(connection: sqlite3.Connection, timestamp: str) -> bool:
    """Never discard a raw observation without a materialized 15-min bucket."""
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return False
        dt = dt.astimezone(timezone.utc)
        bucket = dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0).isoformat()
        return connection.execute(
            "SELECT 1 FROM state_15m WHERE bucket_start=?", (bucket,)
        ).fetchone() is not None
    except (ValueError, sqlite3.Error):
        return False


def _evaluated_plan_row(connection: sqlite3.Connection, timestamp: str) -> bool:
    """Keep plan rows if the day-level evaluator has not produced an artifact."""
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return False
        local_date = dt.astimezone(_STOCKHOLM).date().isoformat()
        row = connection.execute(
            "SELECT payload_json FROM optimizer_day_eval WHERE local_date=?",
            (local_date,),
        ).fetchone()
        if not row:
            return False
        payload = json.loads(row[0])
        return payload.get("status") in {"ok", "partial_plan_coverage"}
    except (ValueError, TypeError, sqlite3.Error):
        return False


def _candidate_rows(connection: sqlite3.Connection, table: str, cursor: int, limit: int):
    """Scan a bounded rowid window, advancing past protected old rows."""
    column = TABLE_DATE_COLUMNS[table]
    return connection.execute(
        f"SELECT rowid,{column} FROM {table} WHERE rowid>? ORDER BY rowid ASC LIMIT ?",
        (cursor, limit),
    ).fetchall()


def run_retention_batch(*, batch_size: int = BATCH_SIZE, now: datetime | None = None) -> dict:
    """One bounded, low-priority, crash-safe SQLite maintenance step.

    Transactions are per table (not one transaction across all tables), and
    run_low_priority's disposable worker and deadline supply additional limits.
    """
    now = (now or _now()).astimezone(timezone.utc)
    limit = max(1, min(int(batch_size), 512))
    removed: dict[str, int] = {}
    protected: dict[str, int] = {}
    errors: dict[str, str] = {}
    cursors = dict(read_retention_status().get("cursors") or {})
    for table, days in RETENTION_DAYS.items():
        cutoff = now - timedelta(days=days)
        try:
            with db.connect_db(timeout=2.0) as c:
                available = c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                if not available:
                    continue
                # No expensive index creation or full-table counting.
                cursor = int(cursors.get(table) or 0)
                candidates = _candidate_rows(c, table, cursor, limit)
                if not candidates and cursor:
                    candidates = _candidate_rows(c, table, 0, limit)
                cursors[table] = candidates[-1][0] if len(candidates) == limit else 0
                latest_plan_generation = None
                if table in {"optimizer_plan", "optimizer_plan_summary"}:
                    if c.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='optimizer_plan_summary'"
                    ).fetchone():
                        latest = c.execute("SELECT MAX(generated_at) FROM optimizer_plan_summary").fetchone()
                        latest_plan_generation = latest[0] if latest else None
                ids = []
                held = 0
                for rowid, timestamp in candidates:
                    if latest_plan_generation is not None and timestamp == latest_plan_generation:
                        held += 1
                        continue
                    if not _older_than(timestamp, cutoff):
                        continue
                    if table == "raw_state" and not _safe_raw_row(c, str(timestamp)):
                        held += 1
                        continue
                    if table == "optimizer_plan" and not _evaluated_plan_row(c, str(timestamp)):
                        held += 1
                        continue
                    if table == "engine_information_vintage":
                        vintage_id = c.execute(
                            "SELECT information_vintage_id FROM engine_information_vintage WHERE rowid=?",
                            (rowid,),
                        ).fetchone()
                        if vintage_id and c.execute(
                            "SELECT 1 FROM engine_decision WHERE information_vintage_id=? LIMIT 1",
                            (vintage_id[0],),
                        ).fetchone():
                            held += 1
                            continue
                    ids.append(rowid)
                if ids:
                    # IDs are drawn from a strict allowlist and bound parameters.
                    markers = ",".join("?" for _ in ids)
                    c.execute(f"DELETE FROM {table} WHERE rowid IN ({markers})", ids)
                removed[table] = len(ids)
                protected[table] = held
        except (sqlite3.Error, ValueError, OSError) as exc:
            errors[table] = repr(exc)
            # A corruption/lock/resource error must not trigger more writes.
            break
    report = {
        "last_run": now.isoformat(),
        "policy": RETENTION_DAYS,
        "batch_size": limit,
        "removed": removed,
        "protected": protected,
        "errors": errors,
        "cursors": cursors,
        "automatic_vacuum": False,
        "note": "SQLite file size is not reduced by DELETE; offline rebuild required",
    }
    try:
        _save_status(report)
    except OSError:
        pass
    if errors:
        raise RuntimeError(f"retention interrupted: {errors}")
    return report
