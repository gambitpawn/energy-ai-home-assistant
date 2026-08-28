from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import HTTPException

from . import model_selector
from . import runtime_entry_v187 as v187
from .optimizer_store import latest_plan
from .production_state import mark_actuator_ready, set_mode, status as production_status

app = v187.app
core = v187.core
RUNTIME_BUILD = "1.0.87"

# If the previous process was ACTIVE, attempt inverter-side zero + General before
# entering the inherited lifespan. That places the release before collector /
# optimizer startup refreshes. A failed attempt remains flagged and v187's
# watchdog retries after startup; application-side writes are already disabled.
_previous_lifespan = app.router.lifespan_context


@asynccontextmanager
async def _lifespan_v187_final(application):
    if v187._NEEDS_STARTUP_RELEASE:
        try:
            await v187.ADAPTER.safe_release()
            v187._NEEDS_STARTUP_RELEASE = False
        except Exception:
            pass
    async with _previous_lifespan(application):
        yield


app.router.lifespan_context = _lifespan_v187_final


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
        if not current.get("actuator_ready"):
            raise HTTPException(409, "ACTIVE requires a successful /actuator/arm?confirm=true zero-power handshake")
        candidate = await _current_candidate()
        if candidate is None:
            raise HTTPException(409, "ACTIVE requires a current selector/live control candidate")
        try:
            prod = await asyncio.to_thread(set_mode, "active", reason="api_actuator_transition")
            actuation = await v187.ACTUATOR.process_candidate(candidate)
        except Exception as exc:
            try:
                await v187.ACTUATOR.fail_safe("active_transition_failed", {"error": repr(exc)})
            except Exception:
                pass
            raise HTTPException(500, f"ACTIVE transition failed: {exc!r}")
        if actuation.get("status") not in {"acknowledged", "held_existing"}:
            raise HTTPException(409, f"ACTIVE transition did not produce an acknowledged command: {actuation}")
        return {**production_status(), "actuator_transition": actuation, "requested_mode": "active"}

    release = None
    if current.get("operating_mode") == "active" or current.get("physical_writes_enabled"):
        try:
            release = await v187.ADAPTER.safe_release()
        except Exception as exc:
            release = {"released": False, "error": repr(exc)}
            # Do not claim a clean transition if the inverter cannot be returned
            # to zero/General. Disable writes locally and surface the failure.
            mark_actuator_ready(False, detail="mode_exit_safe_release_failed")
            await asyncio.to_thread(set_mode, "paused", reason="safe_release_failed")
            raise HTTPException(503, f"Could not safely release Solinteg before leaving ACTIVE: {release}")
    prod = await asyncio.to_thread(set_mode, mode, reason="api_actuator_transition")
    return {**prod, "actuator_transition": {"safe_release": release}, "requested_mode": mode}


core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD
app.openapi_schema = None
