"""Time-limited Balboa comfort override; independent of outage actions."""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import HTTPException, Request

from .db import connect_db
from .balboa_permission import authorized, set_authorized, require_authorized, BalboaControlDisabled

ENTITY = "climate.bp6013g1"
DURATION = timedelta(hours=3)
_POLL_SECONDS = 15


def _now():
    return datetime.now(timezone.utc)


def _stamp(dt):
    return dt.astimezone(timezone.utc).isoformat()


def _parse(value):
    return datetime.fromisoformat(value)


def _init():
    with connect_db(timeout=10) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS balboa_comfort_override (
            id INTEGER PRIMARY KEY CHECK (id=1),
            state TEXT NOT NULL,
            ends_at TEXT,
            previous_preset TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL
        )""")
        db.execute(
            "INSERT OR IGNORE INTO balboa_comfort_override(id,state,updated_at) VALUES(1,'idle',?)",
            (_stamp(_now()),),
        )


def _state():
    _init()
    with connect_db(timeout=10) as db:
        row = db.execute(
            "SELECT state, ends_at, previous_preset, last_error FROM balboa_comfort_override WHERE id=1"
        ).fetchone()
    return dict(zip(("state", "ends_at", "previous_preset", "last_error"), row))


def _save(state, ends_at=None, previous_preset=None, last_error=None):
    _init()
    with connect_db(timeout=10) as db:
        db.execute(
            """UPDATE balboa_comfort_override
            SET state=?, ends_at=?, previous_preset=?, last_error=?, updated_at=? WHERE id=1""",
            (state, ends_at, previous_preset, last_error, _stamp(_now())),
        )


class BalboaComfort:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.task = None
        self._stop = asyncio.Event()

    def _connection(self):
        token = os.getenv("SUPERVISOR_TOKEN") or os.getenv("HA_ACCESS_TOKEN")
        if not token:
            raise RuntimeError("Home Assistant API authentication unavailable")
        base = "http://supervisor/core/api" if os.getenv("SUPERVISOR_TOKEN") else (
            (os.getenv("HA_BASE_URL") or "http://homeassistant.local").rstrip("/")
        )
        if not base.endswith("/api"):
            base += "/api"
        return base, {"Authorization": "Bearer " + token, "Content-Type": "application/json"}

    async def _read(self):
        base, headers = self._connection()
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(f"{base}/states/{ENTITY}", headers=headers)
            r.raise_for_status()
            data = r.json()
        if data.get("state") in ("unknown", "unavailable", None):
            raise RuntimeError("Balboa climate entity unavailable")
        return data

    async def _set_preset(self, preset):
        await asyncio.to_thread(require_authorized)
        if preset not in ("ready", "rest"):
            raise ValueError("Unsupported preset")
        base, headers = self._connection()
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.post(
                f"{base}/services/climate/set_preset_mode",
                json={"entity_id": ENTITY, "preset_mode": preset},
                headers=headers,
            )
            r.raise_for_status()

    async def status(self):
        s = await asyncio.to_thread(_state)
        s["active"] = s["state"] == "active" and bool(s["ends_at"]) and _now() < _parse(s["ends_at"])
        return s

    async def activate(self):
        async with self.lock:
            s = await asyncio.to_thread(_state)
            if not await asyncio.to_thread(authorized):
                raise HTTPException(409, "Balboa control manually disabled")
            if s["state"] in ("active", "restoring", "error"):
                raise HTTPException(409, "Previous spa override must be resolved first")
            try:
                data = await self._read()
                a = data.get("attributes") or {}
                preset = a.get("preset_mode")
                if preset not in ("ready", "rest", "ready_in_rest"):
                    raise HTTPException(409, f"Unexpected Balboa preset: {preset}")
                if "ready" not in (a.get("preset_modes") or []):
                    raise HTTPException(409, "Balboa Ready mode is unavailable")
                # Persist intent before issuing physical control, so restart reconciles it.
                until = _stamp(_now() + DURATION)
                await asyncio.to_thread(_save, "active", until, preset)
                if preset != "ready":
                    await self._set_preset("ready")
                return await self.status()
            except HTTPException:
                raise
            except Exception as exc:
                current = await asyncio.to_thread(_state)
                if current["state"] == "active":
                    await asyncio.to_thread(_save, "error", current["ends_at"], current["previous_preset"], str(exc))
                raise HTTPException(503, "Could not enable Balboa Ready mode") from exc

    async def finish(self):
        async with self.lock:
            s = await asyncio.to_thread(_state)
            if s["state"] == "idle":
                return s
            if not await asyncio.to_thread(authorized):
                await asyncio.to_thread(_save, "idle")
                return await self.status()
            if s["state"] not in ("active", "restoring", "error"):
                raise HTTPException(409, "Unknown spa override state")
            await asyncio.to_thread(_save, "restoring", s["ends_at"], s["previous_preset"])
            try:
                data = await self._read()
                preset = (data.get("attributes") or {}).get("preset_mode")
                original = s["previous_preset"]
                # Never undo an unrelated manual change to a different preset.
                if preset == "ready" and original in ("rest", "ready_in_rest"):
                    await self._set_preset("rest")
                elif preset not in ("ready", "rest", "ready_in_rest"):
                    raise RuntimeError(f"Unexpected Balboa preset during restore: {preset}")
                await asyncio.to_thread(_save, "idle")
            except Exception as exc:
                await asyncio.to_thread(_save, "error", s["ends_at"], s["previous_preset"], str(exc))
                raise HTTPException(503, "Could not verify or restore Balboa mode") from exc
            return await self.status()

    async def reconcile(self):
        s = await asyncio.to_thread(_state)
        if s["state"] in ("active", "restoring") and s["ends_at"] and _now() >= _parse(s["ends_at"]):
            try:
                await self.finish()
            except HTTPException:
                pass

    async def run(self):
        while not self._stop.is_set():
            await self.reconcile()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_POLL_SECONDS)
            except asyncio.TimeoutError:
                continue

    def start(self):
        if not self.task or self.task.done():
            self._stop.clear()
            self.task = asyncio.create_task(self.run(), name="balboa-comfort-override")

    async def stop(self):
        self._stop.set()
        if self.task:
            await self.task
            self.task = None


def install_balboa_comfort(app):
    if getattr(app.state, "balboa_comfort_installed", False):
        return
    comfort = BalboaComfort()
    from .balboa_heat_scheduler import BalboaHeatScheduler
    scheduler = BalboaHeatScheduler(comfort)
    from .balboa_heat_pause import BalboaHeatPause
    heat_pause = BalboaHeatPause(comfort)

    @app.get("/spa/heat-pause")
    async def heat_pause_status():
        return await heat_pause.status()

    @app.post("/spa/heat-pause")
    async def heat_pause_start(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        return await heat_pause.activate((body or {}).get("hours", 6))

    @app.delete("/spa/heat-pause")
    async def heat_pause_end():
        return await heat_pause.finish()

    @app.post("/spa/heating-control")
    async def enable_spa_heat_control():
        if (await heat_pause.status())["status"] != "idle":
            raise HTTPException(409, "Värmepaus pågår; avsluta den först")
        from .balboa_heat_scheduler import set_control_enabled
        async with comfort.lock:
            await asyncio.to_thread(set_control_enabled, True)
            await asyncio.to_thread(set_authorized, True)
        return {"enabled": True, "write_authorized": True}

    @app.delete("/spa/heating-control")
    async def disable_spa_heat_control():
        from .balboa_heat_scheduler import set_control_enabled
        async with comfort.lock:
            # No new writes can start after this handler holds the shared lock.
            await asyncio.to_thread(set_authorized, False)
            await asyncio.to_thread(set_control_enabled, False)
            await asyncio.to_thread(_save, "idle")
        return {"enabled": False, "write_authorized": False}

    @app.get("/spa/control-permission")
    async def spa_control_permission():
        permission = await asyncio.to_thread(authorized)
        from .balboa_heat_scheduler import control_enabled
        heating_enabled = await asyncio.to_thread(control_enabled)
        result = {
            "write_authorized": permission,
            "scheduler_enabled": heating_enabled and permission,
            "balboa": None,
            "readback_error": None,
        }
        try:
            data = await comfort._read()
            attrs = data.get("attributes") or {}
            result["balboa"] = {
                "state": data.get("state"),
                "hvac_action": attrs.get("hvac_action"),
                "preset_mode": attrs.get("preset_mode"),
                "current_temperature": attrs.get("current_temperature"),
                "target_temperature": attrs.get("temperature"),
            }
        except Exception as exc:
            result["readback_error"] = str(exc)
        return result

    @app.get("/spa/heating-plan")
    async def spa_heating_plan():
        return scheduler.status()

    @app.get("/spa/bada-nu")
    async def get_spa_comfort():
        return await comfort.status()

    @app.post("/spa/bada-nu")
    async def start_spa_comfort():
        return await comfort.activate()

    @app.delete("/spa/bada-nu")
    async def stop_spa_comfort():
        return await comfort.finish()

    from contextlib import asynccontextmanager
    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def spa_lifespan(application):
        async with previous_lifespan(application) as state:
            # Preserve explicit opt-in only; stale Bada nu must never restart writes.
            if not await asyncio.to_thread(authorized):
                await asyncio.to_thread(_save, "idle")
            comfort.start()
            scheduler.start()
            heat_pause.start()
            try:
                yield state
            finally:
                await heat_pause.stop()
                await scheduler.stop()
                await comfort.stop()

    app.router.lifespan_context = spa_lifespan
    app.state.balboa_comfort_installed = True
