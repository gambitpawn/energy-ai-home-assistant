from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import HTTPException

from . import model_selector
from . import runtime_entry_v187 as v187
from .actuator_release_state import (
    TrackedSolintegCommandAdapter,
    mark_release_pending,
    release_status,
)
from .optimizer_store import latest_plan
from .production_state import mark_actuator_ready, set_mode, status as production_status

app = v187.app
core = v187.core
RUNTIME_BUILD = "1.0.87"

# Use a release-tracking adapter everywhere in the already-wired v1.87 runtime.
# DeterministicActuator stores its adapter as an instance attribute; replacing both
# references means every disarm/fault/shutdown path persists a failed release.
_TRACKED_ADAPTER = TrackedSolintegCommandAdapter(core.cfg, core.collector.ha)
v187.ADAPTER = _TRACKED_ADAPTER
v187.ACTUATOR.adapter = _TRACKED_ADAPTER
if v187._NEEDS_STARTUP_RELEASE:
    mark_release_pending("startup_detected_previous_active_control")

# If the previous process was ACTIVE, attempt inverter-side zero + General before
# entering the inherited lifespan. Application-side writes were already disabled
# synchronously by runtime_entry_v187 before this point. If the release fails, the
# persistent pending flag is retained and the watchdog below retries indefinitely.
_previous_lifespan = app.router.lifespan_context


@asynccontextmanager
async def _lifespan_v187_final(application):
    if release_status().get("release_pending"):
        try:
            release = await v187.ADAPTER.safe_release()
            if release.get("released"):
                v187._NEEDS_STARTUP_RELEASE = False
        except Exception:
            pass
    async with _previous_lifespan(application):
        yield


app.router.lifespan_context = _lifespan_v187_final


async def _hardened_actuator_watchdog_loop() -> None:
    # Release obligations are independent of production mode. A failed zero/General
    # transition must keep retrying even after local writes have been disabled.
    await asyncio.sleep(10)
    while True:
        try:
            if release_status().get("release_pending"):
                release = await v187.ADAPTER.safe_release()
                if release.get("released"):
                    v187._NEEDS_STARTUP_RELEASE = False
            else:
                await v187.ACTUATOR.watchdog_tick()
        except Exception:
            # Pending release remains persisted. On the next tick it is retried.
            pass
        await asyncio.sleep(
            max(10.0, float((core.cfg.get("actuator") or {}).get("watchdog_poll_seconds", 30.0)))
        )


# runtime_entry_v187._maintenance_loop_v187 resolves this module-global function
# when the lifespan starts, so this replacement removes the one-shot retry policy.
v187._actuator_watchdog_loop = _hardened_actuator_watchdog_loop


async def _current_candidate():
    plan = await asyncio.to_thread(latest_plan, 500)
    if str(plan.get("planner") or "") == "deterministic_battery_dp_v3_6_live":
        return v187._candidate_from_live_plan(plan)
    selection = await asyncio.to_thread(model_selector.latest_control_selection)
    return v187._candidate_from_selection(selection)


# Replace the older metadata-only production-mode route. ACTIVE is an atomic
# transition: a current candidate must exist and be dispatched immediately.
# Leaving ACTIVE always releases the inverter to zero + General first.
app.router.routes[:] = [
    route for route in app.router.routes
    if getattr(route, "path", None) != "/control/mode/{mode}"
]


@app.post("/control/mode/{mode}", tags=["control"], summary="Set production mode with deterministic actuator transition")
async def production_control_mode_v187(mode: str):
    mode = str(mode).strip().lower()
    if mode not in {"shadow", "active", "paused"}:
        raise HTTPException(400, f"unsupported mode {mode!r}")
    current = production_status()

    if mode == "active":
        release_state = release_status()
        if release_state.get("release_pending"):
            raise HTTPException(409, f"ACTIVE is blocked until pending Solinteg safe release succeeds: {release_state}")
        if not current.get("actuator_ready"):
            raise HTTPException(409, "ACTIVE requires a successful /actuator/arm?confirm=true zero-power handshake")
        candidate = await _current_candidate()
        if candidate is None:
            raise HTTPException(409, "ACTIVE requires a current selector/live control candidate")
        try:
            await asyncio.to_thread(set_mode, "active", reason="api_actuator_transition")
            actuation = await v187.ACTUATOR.process_candidate(candidate)
        except Exception as exc:
            try:
                await v187.ACTUATOR.fail_safe("active_transition_failed", {"error": repr(exc)})
            except Exception:
                pass
            raise HTTPException(500, f"ACTIVE transition failed: {exc!r}")
        if actuation.get("status") not in {"acknowledged", "held_existing"}:
            try:
                await v187.ACTUATOR.fail_safe("active_transition_unacknowledged", {"actuation": actuation})
            except Exception:
                pass
            raise HTTPException(409, f"ACTIVE transition did not produce an acknowledged command: {actuation}")
        return {**production_status(), "actuator_transition": actuation, "requested_mode": "active"}

    release = None
    if current.get("operating_mode") == "active" or current.get("physical_writes_enabled"):
        try:
            release = await v187.ADAPTER.safe_release()
            if not release.get("released"):
                raise RuntimeError(f"safe release incomplete: {release}")
        except Exception as exc:
            release = {"released": False, "error": repr(exc), "detail": release}
            # Do not claim a clean transition if the inverter cannot be returned
            # to zero/General. Disable writes locally and surface the failure. The
            # persistent release watchdog continues retrying in PAUSED.
            mark_actuator_ready(False, detail="mode_exit_safe_release_failed")
            await asyncio.to_thread(set_mode, "paused", reason="safe_release_failed")
            raise HTTPException(503, f"Could not safely release Solinteg before leaving ACTIVE: {release}")
    prod = await asyncio.to_thread(set_mode, mode, reason="api_actuator_transition")
    return {**prod, "actuator_transition": {"safe_release": release}, "requested_mode": mode}


# Augment the actuator status with the persistent release obligation.
app.router.routes[:] = [
    route for route in app.router.routes
    if getattr(route, "path", None) != "/actuator/status"
]


@app.get("/actuator/status", tags=["actuator"], summary="Deterministic actuator, Solinteg path and safety status")
async def actuator_status_v187_final():
    data = await v187.ACTUATOR.status()
    data["safe_release"] = await asyncio.to_thread(release_status)
    return data


core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD
app.openapi_schema = None
