"""Explicit temporary Balboa low-setpoint pause, independent of optimizer authorization."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import HTTPException

from .db import connect_db
from .balboa_permission import authorized, set_authorized
from .balboa_heat_scheduler import set_control_enabled, control_enabled

LOW_TARGET = 10.0
POLL_SECONDS = 30


def now():
    return datetime.now(timezone.utc)


def read_record():
    with connect_db(timeout=10) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS balboa_heat_pause (
            id INTEGER PRIMARY KEY CHECK(id=1), status TEXT NOT NULL,
            expires_at TEXT, original_target REAL, original_range TEXT,
            last_error TEXT, updated_at TEXT NOT NULL
        )""")
        db.execute("INSERT OR IGNORE INTO balboa_heat_pause(id,status,updated_at) VALUES(1,'idle',?)", (now().isoformat(),))
        row = db.execute("SELECT status,expires_at,original_target,original_range,last_error,updated_at FROM balboa_heat_pause WHERE id=1").fetchone()
    return dict(zip(("status","expires_at","original_target","original_range","last_error","updated_at"), row))


def save_record(status, expires=None, target=None, range_mode=None, error=None):
    read_record()
    with connect_db(timeout=10) as db:
        db.execute("""UPDATE balboa_heat_pause SET status=?, expires_at=?, original_target=?,
            original_range=?, last_error=?, updated_at=? WHERE id=1""",
            (status, expires, target, range_mode, error, now().isoformat()))


class BalboaHeatPause:
    def __init__(self, comfort):
        self.comfort = comfort
        self.task = None
        self.stop_event = asyncio.Event()

    async def _service(self, domain, service, payload):
        base, headers = self.comfort._connection()
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post(f"{base}/services/{domain}/{service}", headers=headers, json=payload)
            response.raise_for_status()

    async def _range(self):
        base, headers = self.comfort._connection()
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(f"{base}/states/select.bp6013g1_temperaturintervall", headers=headers)
            response.raise_for_status()
            value = response.json().get("state")
        if value not in ("high", "low"):
            raise RuntimeError(f"Invalid Balboa temperature range {value!r}")
        return value

    async def _set_target(self, value):
        await self._service("climate","set_temperature", {"entity_id": "climate.bp6013g1", "temperature": value})

    async def _set_range(self, value):
        await self._service("select","select_option", {"entity_id": "select.bp6013g1_temperaturintervall", "option": value})

    async def status(self):
        state = await asyncio.to_thread(read_record)
        state["active"] = state["status"] in ("active", "starting", "restoring")
        return state

    async def activate(self, hours=6):
        if not isinstance(hours, (int, float)) or not 1 <= hours <= 24:
            raise HTTPException(400, "Värmepaus måste vara mellan 1 och 24 timmar")
        async with self.comfort.lock:
            record = await asyncio.to_thread(read_record)
            if record["status"] != "idle":
                raise HTTPException(409, "Värmepaus pågår eller kräver felsökning")
            from .balboa_comfort import _state
            override = await asyncio.to_thread(_state)
            if override["state"] != "idle":
                raise HTTPException(409, "Avsluta Bada nu innan värmepaus")
            data = await self.comfort._read()
            attrs = data.get("attributes") or {}
            try:
                target = float(attrs["temperature"])
            except (TypeError, ValueError, KeyError) as exc:
                raise HTTPException(503, "Balboa måltemperatur saknas") from exc
            range_mode = await self._range()
            if not 10 <= target <= 40:
                raise HTTPException(409, "Balboa måltemperatur utanför rimligt intervall")
            expires = (now() + timedelta(hours=float(hours))).isoformat()
            # Disconnect all unattended writers before changing the thermostat.
            await asyncio.to_thread(set_authorized, False)
            await asyncio.to_thread(set_control_enabled, False)
            await asyncio.to_thread(save_record, "starting", expires, target, range_mode)
            try:
                if range_mode != "low":
                    await self._set_range("low")
                await self._set_target(LOW_TARGET)
                await self._verify_target(LOW_TARGET)
                await asyncio.to_thread(save_record, "active", expires, target, range_mode)
            except Exception as exc:
                await asyncio.to_thread(save_record, "error", expires, target, range_mode, repr(exc))
                raise HTTPException(503, "Värmepaus kunde inte verifieras; kontrollera Balboa manuellt") from exc
            return await self.status()

    async def _verify_target(self, expected):
        # Give the HA integration time to receive a Balboa state update.
        for _ in range(5):
            await asyncio.sleep(1)
            actual = (await self.comfort._read()).get("attributes", {}).get("temperature")
            if actual is not None and abs(float(actual) - expected) <= 0.26:
                return
        raise RuntimeError("Balboa did not confirm target temperature")

    async def finish(self):
        async with self.comfort.lock:
            record = await asyncio.to_thread(read_record)
            if record["status"] == "idle":
                return await self.status()
            await asyncio.to_thread(save_record, "restoring", record["expires_at"], record["original_target"], record["original_range"])
            try:
                data = await self.comfort._read()
                current = float((data.get("attributes") or {})["temperature"])
                # A manual change during pause wins. Never overwrite it.
                if abs(current - LOW_TARGET) > 0.26:
                    await asyncio.to_thread(save_record, "idle")
                    return {"status": "idle", "restored": False, "reason": "manual_target_change"}
                old_target = float(record["original_target"])
                old_range = record["original_range"]
                if old_range == "high":
                    await self._set_range("high")
                if abs(old_target - LOW_TARGET) > 0.26:
                    await self._set_target(old_target)
                    await self._verify_target(old_target)
                await asyncio.to_thread(save_record, "idle")
                return {"status": "idle", "restored": True}
            except Exception as exc:
                await asyncio.to_thread(save_record, "error", record["expires_at"], record["original_target"], record["original_range"], repr(exc))
                raise HTTPException(503, "Återställning misslyckades, kontrollera Balboa manuellt") from exc

    async def reconcile(self):
        record = await asyncio.to_thread(read_record)
        if record["status"] == "active" and record["expires_at"] and now() >= datetime.fromisoformat(record["expires_at"]):
            try:
                await self.finish()
            except HTTPException:
                pass
        elif record["status"] in ("starting", "restoring"):
            # Recover after restart, not silently leave a half-finished pause.
            try:
                await self.finish()
            except HTTPException:
                pass

    async def run(self):
        while not self.stop_event.is_set():
            try:
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=POLL_SECONDS)
            except asyncio.TimeoutError:
                pass

    def start(self):
        if not self.task or self.task.done():
            self.stop_event.clear()
            self.task = asyncio.create_task(self.run(), name="balboa-heat-pause")

    async def stop(self):
        self.stop_event.set()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
