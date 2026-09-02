from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any
from zoneinfo import ZoneInfo

from .db import DB_PATH

MODES = {"shadow", "active", "paused"}
LOCAL_TZ = ZoneInfo("Europe/Stockholm")
_LOCK = RLock()
_INITIALIZED_PATH: str | None = None
_CACHE: dict[str, Any] | None = None
_LAST_PERSISTENCE_ERROR: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _init() -> None:
    global _CACHE, _INITIALIZED_PATH
    path = str(DB_PATH)
    with _LOCK:
        if _CACHE is not None and _INITIALIZED_PATH == path:
            return
        with sqlite3.connect(DB_PATH, timeout=30) as c:
            c.execute("PRAGMA busy_timeout=30000")
            c.executescript(
                '''
                CREATE TABLE IF NOT EXISTS production_state(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    operating_mode TEXT NOT NULL,
                    physical_writes_enabled INTEGER NOT NULL DEFAULT 0,
                    actuator_ready INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS user_override(
                    override_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    starts_at TEXT NOT NULL,
                    ends_at TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_user_override_active
                    ON user_override(status,starts_at,ends_at);
                '''
            )
            row = c.execute(
                "SELECT operating_mode,physical_writes_enabled,actuator_ready,updated_at,payload_json "
                "FROM production_state WHERE singleton=1"
            ).fetchone()
            if not row:
                created = _now()
                payload = {"reason": "first_start_safe_default"}
                c.execute(
                    "INSERT INTO production_state(singleton,operating_mode,physical_writes_enabled,actuator_ready,updated_at,payload_json) VALUES(1,'shadow',0,0,?,?)",
                    (created, json.dumps(payload)),
                )
                row = ("shadow", 0, 0, created, json.dumps(payload))
        try:
            payload = json.loads(row[4] or "{}")
        except Exception:
            payload = {}
        _CACHE = {
            "operating_mode": str(row[0]),
            "physical_writes_enabled": bool(row[1]),
            "actuator_ready": bool(row[2]),
            "updated_at": str(row[3]),
            "startup_policy": "always_disarmed_requires_new_zero_handshake",
            "payload": payload,
        }
        _INITIALIZED_PATH = path


def _persist(state: dict[str, Any]) -> None:
    with sqlite3.connect(DB_PATH, timeout=30) as c:
        c.execute("PRAGMA busy_timeout=30000")
        c.execute(
            "UPDATE production_state SET operating_mode=?,physical_writes_enabled=?,actuator_ready=?,updated_at=?,payload_json=? WHERE singleton=1",
            (
                state["operating_mode"],
                int(bool(state["physical_writes_enabled"])),
                int(bool(state["actuator_ready"])),
                state["updated_at"],
                json.dumps(state.get("payload") or {}, ensure_ascii=False),
            ),
        )


def status() -> dict[str, Any]:
    _init()
    with _LOCK:
        result = dict(_CACHE or {})
        result["payload"] = dict(result.get("payload") or {})
        result["control_state_source"] = "process_memory"
        result["persistence_error"] = _LAST_PERSISTENCE_ERROR
        return result


def set_mode(mode: str, *, reason: str = "ui") -> dict[str, Any]:
    global _CACHE, _LAST_PERSISTENCE_ERROR
    _init()
    mode = str(mode).strip().lower()
    if mode not in MODES:
        raise ValueError(f"unsupported mode {mode!r}")
    with _LOCK:
        current = dict(_CACHE or {})
        if mode == "active" and not current["actuator_ready"]:
            raise RuntimeError("ACTIVE is unavailable until deterministic actuator safety is ready")
        next_state = {
            **current,
            "operating_mode": mode,
            "physical_writes_enabled": bool(mode == "active" and current["actuator_ready"]),
            "updated_at": _now(),
            "payload": {"reason": reason, "previous_mode": current["operating_mode"]},
        }
        if mode == "active":
            # ACTIVE is the only transition that may enable writes. Require its
            # durable marker before exposing it in memory.
            _persist(next_state)
            _CACHE = next_state
            _LAST_PERSISTENCE_ERROR = None
        else:
            # Safe transitions must take effect in memory even if audit storage
            # is temporarily unavailable after the inverter has been released.
            _CACHE = next_state
            try:
                _persist(next_state)
                _LAST_PERSISTENCE_ERROR = None
            except sqlite3.Error as exc:
                _LAST_PERSISTENCE_ERROR = repr(exc)
    return status()


def mark_actuator_ready(ready: bool, *, detail: str = "") -> dict[str, Any]:
    global _CACHE, _LAST_PERSISTENCE_ERROR
    _init()
    with _LOCK:
        current = dict(_CACHE or {})
        next_state = {
            **current,
            "actuator_ready": bool(ready),
            "physical_writes_enabled": bool(ready and current["operating_mode"] == "active"),
            "updated_at": _now(),
            "payload": {"reason": "actuator_readiness", "detail": detail},
        }
        if ready:
            _persist(next_state)
            _CACHE = next_state
            _LAST_PERSISTENCE_ERROR = None
        else:
            _CACHE = next_state
            try:
                _persist(next_state)
                _LAST_PERSISTENCE_ERROR = None
            except sqlite3.Error as exc:
                _LAST_PERSISTENCE_ERROR = repr(exc)
    return status()


def _parse(value: str) -> datetime:
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=LOCAL_TZ)
    return d.astimezone(timezone.utc)


def create_override(kind: str, *, starts_at: str | None = None, duration_minutes: int | None = None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    _init()
    now = datetime.now(timezone.utc)
    start = _parse(starts_at) if starts_at else now
    end = start + timedelta(minutes=int(duration_minutes)) if duration_minutes is not None else None
    created = _now()
    data = dict(payload or {})
    if duration_minutes is not None:
        data["duration_minutes"] = int(duration_minutes)
    with sqlite3.connect(DB_PATH) as c:
        cur = c.execute(
            "INSERT INTO user_override(kind,status,starts_at,ends_at,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (str(kind), "active", start.isoformat(), None if end is None else end.isoformat(), json.dumps(data, ensure_ascii=False), created, created),
        )
        oid = int(cur.lastrowid)
    return get_override(oid)


def get_override(override_id: int) -> dict[str, Any]:
    _init()
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute(
            "SELECT override_id,kind,status,starts_at,ends_at,payload_json,created_at,updated_at FROM user_override WHERE override_id=?",
            (int(override_id),),
        ).fetchone()
    if not row:
        raise KeyError(override_id)
    try:
        payload = json.loads(row[5] or "{}")
    except Exception:
        payload = {}
    return {"override_id": row[0], "kind": row[1], "status": row[2], "starts_at": row[3], "ends_at": row[4], "payload": payload, "created_at": row[6], "updated_at": row[7]}


def cancel_override(override_id: int) -> dict[str, Any]:
    _init()
    with sqlite3.connect(DB_PATH) as c:
        c.execute("UPDATE user_override SET status='cancelled',updated_at=? WHERE override_id=?", (_now(), int(override_id)))
    return get_override(override_id)


def active_overrides(at: datetime | None = None) -> list[dict[str, Any]]:
    _init()
    now = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            "SELECT override_id FROM user_override WHERE status='active' AND starts_at<=? AND (ends_at IS NULL OR ends_at>?) ORDER BY created_at DESC",
            (now.isoformat(), now.isoformat()),
        ).fetchall()
    return [get_override(int(r[0])) for r in rows]


def scheduled_overrides() -> list[dict[str, Any]]:
    _init()
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            "SELECT override_id FROM user_override WHERE status='active' ORDER BY starts_at ASC,override_id ASC"
        ).fetchall()
    return [get_override(int(r[0])) for r in rows]
