"""Fail-safe Balboa heat/off scheduler, using existing optimizer interval forecasts."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from statistics import median

import httpx

from .optimizer_store import latest_plan
from .db import connect_db
from .balboa_comfort import ENTITY, _now, _state, BalboaComfort

MIN_TARGET_MARGIN_C = 1.0
MAX_OFF_MINUTES = 90
MIN_COMMAND_SPACING_MINUTES = 15
POLL_SECONDS = 60

def control_enabled():
    with connect_db(timeout=10) as db:
        db.execute('CREATE TABLE IF NOT EXISTS balboa_heat_settings (id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0)')
        db.execute('INSERT OR IGNORE INTO balboa_heat_settings(id,enabled) VALUES(1,0)')
        return bool(db.execute('SELECT enabled FROM balboa_heat_settings WHERE id=1').fetchone()[0])

def set_control_enabled(enabled):
    control_enabled()
    with connect_db(timeout=10) as db:
        db.execute('UPDATE balboa_heat_settings SET enabled=? WHERE id=1', (int(bool(enabled)),))


def calculate_heat_plan(now, temperature, target, hvac_mode, rows, *, override=False):
    """Pure, bounded policy: never suppress heat with unsafe or incomplete inputs."""
    now = now.astimezone(timezone.utc)
    mandatory = temperature < target - MIN_TARGET_MARGIN_C
    slots = []
    for r in rows:
        try:
            start = datetime.fromisoformat(r["start"].replace("Z", "+00:00")).astimezone(timezone.utc)
            price = r.get("price_ore_kwh")
            if price is None or r.get("price_known") is False:
                continue
            price = float(price)
            if start + timedelta(minutes=15) <= now or start > now + timedelta(hours=24):
                continue
            grid = float(r.get("grid_import_kw") or 0)
            # Penalize slots with a high predicted import (incorporates EV and battery plan).
            score = price + max(0, grid - 8) * 30
            slots.append({"start": start, "price": price, "grid_kw": grid, "score": score})
        except (ValueError, TypeError, KeyError):
            continue
    slots.sort(key=lambda r: r["start"])
    valid_current = next((r for r in slots if r["start"] <= now < r["start"] + timedelta(minutes=15)), None)
    if override:
        return {"desired": "heat", "reason": "bada_nu", "slots": slots}
    if mandatory:
        return {"desired": "heat", "reason": "temperature_guard", "slots": slots}
    if not slots or valid_current is None:
        return {"desired": "heat", "reason": "missing_price_forecast", "slots": slots}
    # Do not enter a long 'off' period; even a cheap schedule can be wrong due to
    # unknown insulation, wind, open cover, or manual bathing.
    upcoming = [r for r in slots if r["start"] < now + timedelta(minutes=MAX_OFF_MINUTES)]
    if len(upcoming) < 3:
        return {"desired": "heat", "reason": "insufficient_near_term_coverage", "slots": slots}
    cutoff = median(r["score"] for r in upcoming)
    for slot in slots:
        neighbors = [r["score"] for r in slots if slot["start"] <= r["start"] < slot["start"] + timedelta(minutes=MAX_OFF_MINUTES)]
        slot["scheduled_mode"] = "heat" if slot["score"] <= (median(neighbors) if neighbors else cutoff) else "off"
    desired = "heat" if valid_current["score"] <= cutoff else "off"
    # Never claim the requested temperature is reached just because heating is enabled.
    return {"desired": desired, "reason": "cost_window", "slots": slots}


class BalboaHeatScheduler:
    def __init__(self, comfort: BalboaComfort):
        self.comfort = comfort
        self.task = None
        self._stop = asyncio.Event()
        self.last_command_at = None
        self.last_off_at = None
        self.last_error = None
        self.last_plan = {}
        self.enabled = False

    async def _set_mode(self, mode):
        base, headers = self.comfort._connection()
        async with httpx.AsyncClient(timeout=8.0) as client:
            result = await client.post(
                f"{base}/services/climate/set_hvac_mode",
                json={"entity_id": ENTITY, "hvac_mode": mode}, headers=headers
            )
            result.raise_for_status()

    async def tick(self):
        self.enabled = await asyncio.to_thread(control_enabled)
        async with self.comfort.lock:
            data = await self.comfort._read()
            attrs = data.get("attributes") or {}
            hvac_mode = data.get("state")
            temperature = float(attrs["current_temperature"])
            target = float(attrs["temperature"])
            if not 0 <= temperature <= 45 or not 26.5 <= target <= 40:
                raise ValueError("Implausible spa temperature/setpoint")
            if not {"heat", "off"}.issubset(set(attrs.get("hvac_modes") or [])):
                raise ValueError("Balboa heat/off modes unavailable")
            override_state = await asyncio.to_thread(_state)
            override = override_state["state"] in ("active", "restoring", "error")
            plan = await asyncio.to_thread(latest_plan)
            rows = plan.get("rows") or []
            decision = calculate_heat_plan(_now(), temperature, target, hvac_mode, rows, override=override)
            desired = decision["desired"]
            # Invariant: comfort override and low-temperature guards always request heat.
            if self.last_off_at and _now() - self.last_off_at > timedelta(minutes=MAX_OFF_MINUTES):
                desired = "heat"
                decision["reason"] = "max_off_duration"
            if self.last_command_at and _now() - self.last_command_at < timedelta(minutes=MIN_COMMAND_SPACING_MINUTES) and desired == "off":
                desired = "heat" if hvac_mode == "heat" else "off"
                decision["reason"] = "minimum_switch_interval"
            # Ready is required for continuous heating outside filter cycles.
            # Changing this preset does not change the user-selected target.
            preset = attrs.get("preset_mode")
            if preset not in ("ready", "rest", "ready_in_rest"):
                raise ValueError("Unexpected Balboa heating preset")
            if self.enabled and preset != "ready":
                if "ready" not in (attrs.get("preset_modes") or []):
                    raise ValueError("Balboa Ready preset unavailable")
                await self.comfort._set_preset("ready")
            if self.enabled and desired != hvac_mode and (not self.last_command_at or _now() - self.last_command_at >= timedelta(minutes=MIN_COMMAND_SPACING_MINUTES) or decision["reason"] in ("temperature_guard", "bada_nu", "max_off_duration")):
                await self._set_mode(desired)
                self.last_command_at = _now()
                if desired == "off":
                    self.last_off_at = _now()
                else:
                    self.last_off_at = None
            elif hvac_mode == "heat":
                self.last_off_at = None
            elif hvac_mode == "off" and self.last_off_at is None:
                # Unknown downtime on restart: force heat next tick, do not
                # assume that the OFF period started recently.
                self.last_off_at = _now() - timedelta(minutes=MAX_OFF_MINUTES + 1)
            self.last_plan = {
                "updated_at": _now().isoformat(), "control_enabled": self.enabled, "current_temperature": temperature,
                "target_temperature": target, "observed_mode": hvac_mode,
                "desired_mode": desired, "reason": decision["reason"],
                "forecast_generated_at": plan.get("generated_at"),
                "next_windows": [
                    {"start": s["start"].isoformat(), "price_ore_kwh": s["price"],
                     "forecast_grid_kw": s["grid_kw"], "score": round(s["score"], 2), "scheduled_mode": s.get("scheduled_mode", "heat")}
                    for s in decision["slots"][:48]
                ]
            }
            self.last_error = None
            return self.last_plan

    async def run(self):
        while not self._stop.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = repr(exc)
                # Fail open: restore heating when possible; never set OFF on error.
                try:
                    if await asyncio.to_thread(control_enabled):
                        data = await self.comfort._read()
                    else:
                        data = None
                    if data and data.get("state") == "off":
                        await self._set_mode("heat")
                except Exception:
                    pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=POLL_SECONDS)
            except asyncio.TimeoutError:
                pass

    def start(self):
        if self.task is None or self.task.done():
            self._stop.clear()
            self.task = asyncio.create_task(self.run(), name="balboa-heat-scheduler")

    async def stop(self):
        self._stop.set()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        # On shutdown, leave heater enabled; HA/Balboa handles target.
        try:
            if not await asyncio.to_thread(control_enabled):
                return
            data = await self.comfort._read()
            if data.get("state") == "off":
                await self._set_mode("heat")
        except Exception:
            pass

    def status(self):
        return {"policy": "heat_off_guarded_v1", "enabled": self.enabled, "last_error": self.last_error,
                "plan": self.last_plan}
