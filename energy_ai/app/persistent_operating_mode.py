from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException

from . import production_state
from .db import DB_PATH
from .settings_store import load_setting_overrides

OPTIONS_PATH = Path("/data/options.json")
_VALID_MODES = {"shadow", "active", "paused"}
_NOTIFICATION_LOCK = asyncio.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    return c


def _init() -> None:
    with _connect() as c:
        c.execute(
            '''CREATE TABLE IF NOT EXISTS operating_mode_lifecycle(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                desired_mode TEXT NOT NULL,
                previous_shutdown_clean INTEGER NOT NULL DEFAULT 1,
                boot_started_at TEXT,
                last_clean_shutdown_at TEXT,
                fault_sequence INTEGER NOT NULL DEFAULT 0,
                last_fault_reason TEXT,
                last_fault_detail_json TEXT NOT NULL DEFAULT '{}',
                last_fault_at TEXT,
                last_notified_fault_sequence INTEGER NOT NULL DEFAULT 0,
                last_notification_error TEXT,
                updated_at TEXT NOT NULL
            )'''
        )


def prepare_startup() -> dict[str, Any]:
    """Capture operator intent before runtime.py performs its mandatory disarm.

    The boot marker is changed to unclean immediately. A crash/SIGKILL therefore
    leaves durable evidence and the next process starts PAUSED rather than
    automatically restoring physical writes.
    """
    _init()
    current = production_state.status()
    inferred = str(current.get("operating_mode") or "shadow").lower()
    if inferred not in _VALID_MODES:
        inferred = "shadow"
    now = _now()
    with _connect() as c:
        row = c.execute(
            "SELECT desired_mode,previous_shutdown_clean,fault_sequence,last_notified_fault_sequence "
            "FROM operating_mode_lifecycle WHERE singleton=1"
        ).fetchone()
        migrated = row is None
        if row is None:
            c.execute(
                "INSERT INTO operating_mode_lifecycle(singleton,desired_mode,previous_shutdown_clean,"
                "fault_sequence,last_notified_fault_sequence,updated_at) VALUES(1,?,1,0,0,?)",
                (inferred, now),
            )
            row = (inferred, 1, 0, 0)

        desired = str(row[0] or "shadow")
        was_clean = bool(row[1])
        fault_sequence = int(row[2] or 0)
        startup_fault = None
        if not was_clean:
            fault_sequence += 1
            desired = "paused"
            startup_fault = {
                "sequence": fault_sequence,
                "reason": "unclean_previous_shutdown",
                "detail": {"previous_persisted_mode": str(row[0] or "shadow")},
                "at": now,
            }
            c.execute(
                "UPDATE operating_mode_lifecycle SET desired_mode='paused',fault_sequence=?,"
                "last_fault_reason=?,last_fault_detail_json=?,last_fault_at=?,updated_at=? WHERE singleton=1",
                (
                    fault_sequence,
                    startup_fault["reason"],
                    json.dumps(startup_fault["detail"], ensure_ascii=False),
                    now,
                    now,
                ),
            )
        c.execute(
            "UPDATE operating_mode_lifecycle SET previous_shutdown_clean=0,boot_started_at=?,updated_at=? WHERE singleton=1",
            (now, now),
        )
    return {
        "desired_mode": desired,
        "previous_shutdown_clean": was_clean,
        "legacy_migration": migrated,
        "startup_fault": startup_fault,
        "boot_started_at": now,
    }


def lifecycle_status() -> dict[str, Any]:
    _init()
    with _connect() as c:
        row = c.execute(
            "SELECT desired_mode,previous_shutdown_clean,boot_started_at,last_clean_shutdown_at,"
            "fault_sequence,last_fault_reason,last_fault_detail_json,last_fault_at,"
            "last_notified_fault_sequence,last_notification_error,updated_at "
            "FROM operating_mode_lifecycle WHERE singleton=1"
        ).fetchone()
    if row is None:
        return {}
    try:
        detail = json.loads(row[6] or "{}")
    except Exception:
        detail = {}
    return {
        "desired_mode": str(row[0]),
        "previous_shutdown_clean": bool(row[1]),
        "boot_started_at": row[2],
        "last_clean_shutdown_at": row[3],
        "fault_sequence": int(row[4] or 0),
        "last_fault_reason": row[5],
        "last_fault_detail": detail,
        "last_fault_at": row[7],
        "last_notified_fault_sequence": int(row[8] or 0),
        "last_notification_error": row[9],
        "updated_at": row[10],
    }


def set_desired_mode(mode: str, *, reason: str) -> dict[str, Any]:
    _init()
    mode = str(mode).lower().strip()
    if mode not in _VALID_MODES:
        raise ValueError(f"unsupported persistent operating mode {mode!r}")
    with _connect() as c:
        c.execute(
            "UPDATE operating_mode_lifecycle SET desired_mode=?,updated_at=? WHERE singleton=1",
            (mode, _now()),
        )
    return {**lifecycle_status(), "desired_mode_reason": reason}


def mark_clean_shutdown() -> dict[str, Any]:
    _init()
    now = _now()
    with _connect() as c:
        c.execute(
            "UPDATE operating_mode_lifecycle SET previous_shutdown_clean=1,last_clean_shutdown_at=?,"
            "updated_at=? WHERE singleton=1",
            (now, now),
        )
    return lifecycle_status()


def record_fault(reason: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    _init()
    now = _now()
    payload = dict(detail or {})
    with _connect() as c:
        row = c.execute(
            "SELECT fault_sequence FROM operating_mode_lifecycle WHERE singleton=1"
        ).fetchone()
        sequence = int((row or [0])[0] or 0) + 1
        c.execute(
            "UPDATE operating_mode_lifecycle SET desired_mode='paused',fault_sequence=?,"
            "last_fault_reason=?,last_fault_detail_json=?,last_fault_at=?,updated_at=? WHERE singleton=1",
            (sequence, str(reason), json.dumps(payload, ensure_ascii=False, default=str), now, now),
        )
    return {"sequence": sequence, "reason": str(reason), "detail": payload, "at": now}


def pending_fault() -> dict[str, Any] | None:
    state = lifecycle_status()
    sequence = int(state.get("fault_sequence") or 0)
    if sequence <= int(state.get("last_notified_fault_sequence") or 0):
        return None
    return {
        "sequence": sequence,
        "reason": state.get("last_fault_reason") or "unknown_fault",
        "detail": state.get("last_fault_detail") or {},
        "at": state.get("last_fault_at"),
    }


def _mark_notification_success(sequence: int) -> None:
    with _connect() as c:
        c.execute(
            "UPDATE operating_mode_lifecycle SET last_notified_fault_sequence=MAX(last_notified_fault_sequence,?),"
            "last_notification_error=NULL,updated_at=? WHERE singleton=1",
            (int(sequence), _now()),
        )


def _mark_notification_error(error: str) -> None:
    with _connect() as c:
        c.execute(
            "UPDATE operating_mode_lifecycle SET last_notification_error=?,updated_at=? WHERE singleton=1",
            (str(error)[:1000], _now()),
        )


def _raw_options() -> dict[str, Any]:
    try:
        value = json.loads(OPTIONS_PATH.read_text(encoding="utf-8")) if OPTIONS_PATH.exists() else {}
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def notification_settings() -> dict[str, Any]:
    raw = _raw_options()
    try:
        overrides = load_setting_overrides()
    except Exception:
        overrides = {}
    effective = {**raw, **overrides}
    return {
        "enabled": bool(effective.get("fault_notification_enabled", False)),
        "service": str(effective.get("fault_notification_service") or "").strip(),
        "target": str(effective.get("fault_notification_target") or "").strip(),
    }


async def send_pending_fault_notification(ha) -> dict[str, Any]:
    """Best-effort mail through an existing Home Assistant notify service.

    All SQLite/config reads are moved off the event loop. Notification transport
    never participates in the actuator safety decision and delivery failure leaves
    the fault pending for a later startup retry.
    """
    async with _NOTIFICATION_LOCK:
        fault = await asyncio.to_thread(pending_fault)
        if fault is None:
            return {"status": "nothing_pending"}
        settings = await asyncio.to_thread(notification_settings)
        if not settings["enabled"]:
            return {"status": "disabled", "fault": fault}
        service_spec = settings["service"]
        if not service_spec:
            error = "fault_notification_service_not_configured"
            await asyncio.to_thread(_mark_notification_error, error)
            return {"status": "not_configured", "error": error, "fault": fault}
        if "." in service_spec:
            domain, service = service_spec.split(".", 1)
        else:
            domain, service = "notify", service_spec
        if domain != "notify" or not service:
            error = "fault_notification_service_must_be_notify_service"
            await asyncio.to_thread(_mark_notification_error, error)
            return {"status": "invalid_configuration", "error": error, "fault": fault}
        if not getattr(ha, "authenticated", False):
            error = "home_assistant_not_authenticated"
            await asyncio.to_thread(_mark_notification_error, error)
            return {"status": "delivery_failed", "error": error, "fault": fault}

        title = "Energy AI paused"
        message = (
            f"Energy AI switched to PAUSED/Shadow after a fault. "
            f"Reason: {fault['reason']}. Time: {fault.get('at') or 'unknown'}. "
            "Physical battery writes are disabled until Active is selected again."
        )
        body: dict[str, Any] = {"title": title, "message": message}
        if settings["target"]:
            body["target"] = [settings["target"]]

        last_error = None
        for delay in (0.0, 5.0, 15.0):
            if delay:
                await asyncio.sleep(delay)
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        f"{ha.base_url}/services/{domain}/{service}",
                        headers=ha._headers(),
                        json=body,
                        timeout=10.0,
                    )
                    response.raise_for_status()
                await asyncio.to_thread(_mark_notification_success, int(fault["sequence"]))
                return {"status": "sent", "fault": fault, "service": service_spec}
            except Exception as exc:
                last_error = repr(exc)
        await asyncio.to_thread(_mark_notification_error, last_error or "unknown_notification_error")
        return {"status": "delivery_failed", "error": last_error, "fault": fault}


def _schedule_notification(ha) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(send_pending_fault_notification(ha), name="energy-ai-fault-notification")


def _find_route(app, path: str, method: str):
    method = method.upper()
    for route in app.router.routes:
        if getattr(route, "path", None) != path:
            continue
        methods = {str(x).upper() for x in (getattr(route, "methods", None) or set())}
        if method in methods:
            return route
    raise RuntimeError(f"operator mode route missing: {method} {path}")


def install_persistent_operating_mode(*, app, actuator, ha, startup_state: dict[str, Any]) -> dict[str, Any]:
    if getattr(app.state, "persistent_operating_mode_installed", False):
        return {"installed": True, "already_installed": True, **lifecycle_status()}
    app.state.persistent_operating_mode_installed = True

    # All actuator fail-safe paths converge here: watchdog, quarter actuation and
    # live-replan exceptions. The physical safety transition runs first. Fault
    # persistence then runs in a worker thread; mail is fire-and-forget.
    original_fail_safe = actuator.fail_safe

    async def persistent_fail_safe(reason: str, payload: dict[str, Any] | None = None):
        try:
            result = await original_fail_safe(reason, payload)
        except Exception as exc:
            try:
                await asyncio.to_thread(
                    production_state.set_mode,
                    "paused",
                    reason=f"actuator_fault_wrapper:{reason}",
                )
            except Exception:
                pass
            await asyncio.to_thread(
                record_fault,
                reason,
                {"payload": payload or {}, "fail_safe_error": repr(exc)},
            )
            _schedule_notification(ha)
            raise
        await asyncio.to_thread(record_fault, reason, payload or {})
        _schedule_notification(ha)
        return result

    actuator.fail_safe = persistent_fail_safe

    # Serialize automatic restore with manual Active/Shadow transitions. The
    # fail-safe path intentionally does not take this lock and therefore can
    # always pre-empt a normal mode transition.
    transition_lock = asyncio.Lock()

    def lock_route(path: str, desired_on_success: str):
        route = _find_route(app, path, "POST")
        original = route.dependant.call

        async def locked_call():
            async with transition_lock:
                result = await original()
                prod = (result or {}).get("production") or {}
                if desired_on_success == "active":
                    successful = (
                        prod.get("operating_mode") == "active"
                        and bool(prod.get("physical_writes_enabled"))
                    )
                else:
                    successful = (
                        prod.get("operating_mode") == "shadow"
                        and not bool(prod.get("physical_writes_enabled"))
                    )
                if successful:
                    await asyncio.to_thread(
                        set_desired_mode,
                        desired_on_success,
                        reason=f"operator_mode_{desired_on_success}",
                    )
                return result

        route.dependant.call = locked_call
        route.endpoint = locked_call
        return locked_call

    active_call = lock_route("/control/operator-mode/active", "active")
    lock_route("/control/operator-mode/shadow", "shadow")

    target_mode = str(startup_state.get("desired_mode") or "shadow")
    if target_mode == "paused":
        try:
            production_state.set_mode("paused", reason="persistent_paused_restore")
        except Exception:
            pass

    base_lifespan = app.router.lifespan_context

    async def desired_mode() -> str:
        state = await asyncio.to_thread(lifecycle_status)
        return str(state.get("desired_mode") or "shadow")

    async def startup_restore_worker() -> None:
        await asyncio.sleep(5.0)
        await send_pending_fault_notification(ha)
        if target_mode != "active":
            return
        last_error = None
        for attempt in range(18):
            if await desired_mode() != "active":
                return
            try:
                result = await active_call()
                prod = (result or {}).get("production") or {}
                if prod.get("operating_mode") == "active" and prod.get("physical_writes_enabled"):
                    return
                last_error = f"activation_returned_non_active:{result!r}"
            except HTTPException as exc:
                last_error = repr(exc.detail)
            except Exception as exc:
                last_error = repr(exc)
            if await desired_mode() != "active":
                return
            if attempt + 1 < 18:
                await asyncio.sleep(5.0)

        await asyncio.to_thread(
            record_fault,
            "startup_active_restore_failed",
            {"attempts": 18, "last_error": last_error},
        )
        try:
            await asyncio.to_thread(
                production_state.set_mode,
                "paused",
                reason="startup_active_restore_failed",
            )
        except Exception:
            pass
        _schedule_notification(ha)

    @asynccontextmanager
    async def persistent_lifespan(application):
        clean = False
        restore_task = None
        try:
            async with base_lifespan(application) as state:
                restore_task = asyncio.create_task(
                    startup_restore_worker(), name="energy-ai-persistent-mode-restore"
                )
                yield state
            clean = True
        finally:
            if restore_task is not None and not restore_task.done():
                restore_task.cancel()
                try:
                    await restore_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
            if clean:
                await asyncio.to_thread(mark_clean_shutdown)
            # Exceptional lifespan exit intentionally leaves the boot marker
            # unclean. The next startup converts it to PAUSED and retries mail.

    app.router.lifespan_context = persistent_lifespan
    return {
        "installed": True,
        "startup": startup_state,
        "policy": "persistent_operator_intent_safe_rearm_v1",
        "active_restore_attempts": 18,
        "active_restore_retry_seconds": 5,
        **lifecycle_status(),
    }
