"""Offline-only SQLite database compaction with verified, recoverable promotion.

Compaction runs before uvicorn starts, never alongside live SQLite users.
An optional monthly maintenance task stages a marker and asks Supervisor to
restart this add-on only when physical actuation is inactive. All other code
continues using the original file until the clean process exit.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

from . import db

MARKER_NAME = "compaction_pending.json"
STATUS_NAME = "compaction_status.json"
MIN_INTERVAL_DAYS = 28
MIN_FREE_PADDING_BYTES = 512 * 1024 * 1024
CONTROL_TABLES = (
    "raw_state", "state_15m", "optimizer_plan", "optimizer_plan_summary",
    "neural_training_sample", "adaptive_parameter_state", "engine_decision",
    "engine_information_vintage", "production_state",
)


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _paths(database: Path) -> tuple[Path, Path, Path, Path]:
    directory = database.parent
    return (directory / MARKER_NAME, directory / STATUS_NAME,
            directory / (database.name + ".compact.tmp"),
            directory / (database.name + ".precompact"))


def _write_json(path: Path, data: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as writer:
        json.dump(data, writer, sort_keys=True)
        writer.flush()
        os.fsync(writer.fileno())
    os.replace(temporary, path)


def status(database: Path | None = None) -> dict:
    source = database or db.DB_PATH
    marker, report, _, _ = _paths(source)
    try:
        stored = json.loads(report.read_text(encoding="utf-8"))
        if not isinstance(stored, dict):
            stored = {}
    except (ValueError, OSError):
        stored = {}
    return {**stored, "pending": marker.exists(), "database_bytes": source.stat().st_size if source.exists() else None}


def _space_ok(database: Path) -> tuple[bool, int]:
    if not database.is_file():
        return False, 0
    free = shutil.disk_usage(database.parent).free
    required = database.stat().st_size + MIN_FREE_PADDING_BYTES
    return free >= required, free


def due(database: Path | None = None, now: datetime | None = None) -> bool:
    source = database or db.DB_PATH
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    info = status(source)
    if info.get("pending"):
        return False
    raw = info.get("last_success_at")
    if not raw:
        return True
    try:
        previous = datetime.fromisoformat(str(raw))
        if previous.tzinfo is None:
            return True
        return current - previous.astimezone(timezone.utc) >= timedelta(days=MIN_INTERVAL_DAYS)
    except ValueError:
        return True


def prepare_restart_if_safe(*, database: Path | None = None, production: dict | None = None) -> dict:
    """Fail closed without changing modes or interrupting physical controls."""
    source = database or db.DB_PATH
    if not due(source):
        return {"status": "not_due"}
    if not isinstance(production, dict):
        return {"status": "production_state_unavailable"}
    if production.get("operating_mode") not in ("shadow", "paused", "off"):
        return {"status": "active_mode_deferred"}
    if production.get("physical_writes_enabled") or production.get("actuator_ready"):
        return {"status": "actuator_enabled_deferred"}
    has_space, free = _space_ok(source)
    if not has_space:
        return {"status": "insufficient_disk_space", "free_bytes": free,
                "required_bytes": source.stat().st_size + MIN_FREE_PADDING_BYTES if source.exists() else None}
    marker, report, _, _ = _paths(source)
    _write_json(marker, {"requested_at": _stamp(), "source_bytes": source.stat().st_size})
    # Preserve the previous last_success_at while recording the request.
    prior = status(source)
    _write_json(report, {**prior, "last_request_at": _stamp(), "last_result": "restart_requested"})
    return {"status": "restart_requested", "free_bytes": free}


def ask_supervisor_to_restart(*, opener=None) -> dict:
    """Only self-restart is permitted. No wider Supervisor permissions required."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return {"status": "supervisor_token_missing"}
    request = urllib.request.Request(
        "http://supervisor/addons/self/restart",
        data=b"{}",
        method="POST",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    try:
        client = opener or urllib.request.urlopen
        with client(request, timeout=8) as response:
            code = getattr(response, "status", 200)
            return {"status": "restart_accepted" if 200 <= code < 300 else "restart_rejected", "http_status": code}
    except Exception as exc:
        return {"status": "restart_request_failed", "error": repr(exc)}


def _row_counts(connection: sqlite3.Connection) -> dict[str, int]:
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    counts = {}
    for table in CONTROL_TABLES:
        if table in tables:
            counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    return counts


def _integrity(connection: sqlite3.Connection) -> None:
    errors = connection.execute("PRAGMA integrity_check").fetchall()
    if errors != [("ok",)]:
        raise RuntimeError("SQLite integrity_check failed: " + repr(errors[:8]))
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchmany(1)
    if foreign_key_errors:
        raise RuntimeError("SQLite foreign_key_check failed: " + repr(foreign_key_errors))


def compact_before_server_start(database: Path | None = None) -> dict:
    """Run only at cold process startup, with no other DB connections alive.

    A failure never overwrites the source. The transactionally copied result
    is checked before promotion; the original is retained until post-swap
    verification succeeds. A stale rollback file is treated as an error.
    """
    source = database or db.DB_PATH
    marker, report, target, rollback = _paths(source)
    # Recover an interrupted rename before main.py can create a fresh empty DB.
    if not source.exists() and rollback.exists():
        os.replace(rollback, source)
        result = {"status": "interrupted_swap_recovered", "last_result": "restored_original"}
        marker.unlink(missing_ok=True)
        _write_json(report, result)
        return result
    if not marker.exists():
        return {"status": "not_requested"}
    if not source.exists():
        # Explicit failure: never silently initialize a new empty database.
        return {"status": "database_missing"}
    if rollback.exists():
        result = {"status": "rollback_file_exists", "error": "manual inspection required"}
        _write_json(report, {**status(source), "last_result": result["status"]})
        return result
    sufficient, free = _space_ok(source)
    if not sufficient:
        marker.unlink(missing_ok=True)
        result = {"status": "insufficient_disk_space", "free_bytes": free}
        _write_json(report, {**status(source), "last_result": result["status"]})
        return result
    if target.exists():
        # Only remove our reserved prior incomplete output; never source.
        target.unlink()
    started = _stamp()
    original_bytes = source.stat().st_size
    promoted = False
    try:
        with sqlite3.connect(source, timeout=5, isolation_level=None) as old:
            # WAL can hold committed writes; checkpoint before snapshot and swap.
            checkpoint = old.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is None or int(checkpoint[0]) != 0 or int(checkpoint[1]) != int(checkpoint[2]):
                raise RuntimeError("could not checkpoint SQLite WAL")
            before = _row_counts(old)
            # VACUUM INTO creates a logically compact, consistent output.
            old.execute("VACUUM INTO ?", (str(target),))
        with sqlite3.connect(f"file:{target}?mode=ro", uri=True) as candidate:
            _integrity(candidate)
            after = _row_counts(candidate)
        if before != after:
            raise RuntimeError("table counts changed during compaction: " + repr((before, after)))
        if target.stat().st_size == 0:
            raise RuntimeError("empty compaction result")
        # No server process is running now. Rename is atomic within /data.
        os.replace(source, rollback)
        try:
            # All committed WAL frames were checkpointed while no other
            # process existed. Remove old sidecars before opening the new DB.
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(source) + suffix)
                if sidecar.exists():
                    sidecar.unlink()
            os.replace(target, source)
            promoted = True
            with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as check:
                _integrity(check)
                if _row_counts(check) != before:
                    raise RuntimeError("promoted database row count mismatch")
        except Exception:
            if promoted:
                source.unlink(missing_ok=True)
            os.replace(rollback, source)
            promoted = False
            raise
        compact_bytes = source.stat().st_size
        rollback.unlink()
        marker.unlink(missing_ok=True)
        result = {"status": "success", "last_success_at": _stamp(),
                  "last_started_at": started, "last_result": "success",
                  "previous_bytes": original_bytes, "compacted_bytes": compact_bytes,
                  "saved_bytes": max(0, original_bytes - compact_bytes)}
        _write_json(report, result)
        return result
    except Exception as exc:
        if target.exists():
            target.unlink()
        marker.unlink(missing_ok=True)
        result = {"status": "failed", "last_started_at": started,
                  "last_result": "failed", "last_error": repr(exc)}
        _write_json(report, {**status(source), **result})
        return result
