from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from threading import RLock
from typing import Any

from .db import DB_PATH
from .solinteg_command import SolintegCommandAdapter

_INIT_LOCK = RLock()
_INITIALIZED_PATH: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _init() -> None:
    global _INITIALIZED_PATH
    path = str(DB_PATH)
    with _INIT_LOCK:
        if _INITIALIZED_PATH == path:
            return
        with sqlite3.connect(DB_PATH, timeout=30) as c:
            c.execute("PRAGMA busy_timeout=30000")
            c.executescript(
            '''
            CREATE TABLE IF NOT EXISTS actuator_release_state(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                release_pending INTEGER NOT NULL,
                reason TEXT,
                first_pending_at TEXT,
                last_attempt_at TEXT,
                last_success_at TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            '''
            )
            if not c.execute("SELECT 1 FROM actuator_release_state WHERE singleton=1").fetchone():
                c.execute(
                    '''INSERT INTO actuator_release_state(
                       singleton,release_pending,reason,first_pending_at,last_attempt_at,
                       last_success_at,attempt_count,updated_at)
                       VALUES(1,0,NULL,NULL,NULL,NULL,0,?)''',
                    (_now(),),
                )
        _INITIALIZED_PATH = path


def mark_release_pending(reason: str) -> dict[str, Any]:
    _init()
    now = _now()
    with sqlite3.connect(DB_PATH, timeout=30) as c:
        c.execute("PRAGMA busy_timeout=30000")
        current = c.execute(
            "SELECT release_pending,first_pending_at,attempt_count FROM actuator_release_state WHERE singleton=1"
        ).fetchone()
        first = current[1] if current and current[0] else now
        attempts = int(current[2] or 0) if current else 0
        c.execute(
            '''UPDATE actuator_release_state SET
               release_pending=1,reason=?,first_pending_at=?,attempt_count=?,updated_at=?
               WHERE singleton=1''',
            (str(reason), first, attempts, now),
        )
    return release_status()


def mark_release_attempt(reason: str | None = None) -> dict[str, Any]:
    _init()
    now = _now()
    with sqlite3.connect(DB_PATH, timeout=30) as c:
        c.execute("PRAGMA busy_timeout=30000")
        c.execute(
            '''UPDATE actuator_release_state SET
               release_pending=1,
               reason=COALESCE(?,reason),
               first_pending_at=COALESCE(first_pending_at,?),
               last_attempt_at=?,attempt_count=attempt_count+1,updated_at=?
               WHERE singleton=1''',
            (None if reason is None else str(reason), now, now, now),
        )
    return release_status()


def mark_release_succeeded() -> dict[str, Any]:
    _init()
    now = _now()
    with sqlite3.connect(DB_PATH, timeout=30) as c:
        c.execute("PRAGMA busy_timeout=30000")
        c.execute(
            '''UPDATE actuator_release_state SET
               release_pending=0,reason=NULL,first_pending_at=NULL,
               last_success_at=?,attempt_count=0,updated_at=?
               WHERE singleton=1''',
            (now, now),
        )
    return release_status()


def release_status() -> dict[str, Any]:
    _init()
    with sqlite3.connect(DB_PATH, timeout=30) as c:
        c.execute("PRAGMA busy_timeout=30000")
        row = c.execute(
            '''SELECT release_pending,reason,first_pending_at,last_attempt_at,
                      last_success_at,attempt_count,updated_at
               FROM actuator_release_state WHERE singleton=1'''
        ).fetchone()
    return {
        "release_pending": bool(row[0]),
        "reason": row[1],
        "first_pending_at": row[2],
        "last_attempt_at": row[3],
        "last_success_at": row[4],
        "attempt_count": int(row[5] or 0),
        "updated_at": row[6],
        "recovery_policy": "retry_zero_target_and_safe_mode_until_acknowledged",
    }


class TrackedSolintegCommandAdapter(SolintegCommandAdapter):
    """Solinteg adapter whose safe-release obligation survives process restarts."""

    async def safe_release(self) -> dict[str, Any]:
        # Never let SQLite delay the physical zero/safe-mode request. The ACTIVE
        # production marker is already durable before writes are enabled, so a
        # process crash during this call still causes startup recovery.
        try:
            result = await super().safe_release()
        except Exception:
            try:
                await asyncio.to_thread(mark_release_attempt, "solinteg_safe_release_failed")
            except Exception:
                pass
            raise
        try:
            await asyncio.to_thread(mark_release_attempt, "solinteg_safe_release")
        except Exception:
            pass
        if result.get("released"):
            try:
                await asyncio.to_thread(mark_release_succeeded)
            except Exception:
                pass
        try:
            state = await asyncio.to_thread(release_status)
        except Exception as exc:
            state = {
                "release_pending": not bool(result.get("released")),
                "reason": "release_state_persistence_unavailable",
                "error": repr(exc),
                "recovery_policy": "retry_zero_target_and_safe_mode_until_acknowledged",
            }
        return {**result, "release_state": state}
