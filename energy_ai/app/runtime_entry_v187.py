from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

from fastapi import HTTPException, Query

from . import model_selector
from . import runtime_entry_v186 as v186
from . import ui_v164
from .actuator_config import install_actuator_config
from .deterministic_actuator import DeterministicActuator
from .optimizer_store import latest_plan
from .production_state import mark_actuator_ready, set_mode, status as production_status
from .runtime_entry_v186_final import app, core
from .solinteg_command import SolintegCommandAdapter

RUNTIME_BUILD = "1.0.87"
ACTUATOR_CONFIG = install_actuator_config(core.cfg)
ADAPTER = SolintegCommandAdapter(core.cfg, core.collector.ha)
ACTUATOR = DeterministicActuator(core.cfg, ADAPTER)

# Never inherit ACTIVE across a process restart. Capture whether a safe release
# is needed, then disable writes synchronously before FastAPI's startup refreshes.
_PREVIOUS_PRODUCTION = production_status()
_NEEDS_STARTUP_RELEASE = bool(
    _PREVIOUS_PRODUCTION.get("operating_mode") == "active"
    or _PREVIOUS_PRODUCTION.get("physical_writes_enabled")
)
try:
    set_mode("shadow", reason="startup_disarm_before_validation")
finally:
    mark_actuator_ready(False, detail="startup_requires_new_zero_handshake")


def _candidate_from_selection(selection: dict[str, Any] | None) -> dict[str, Any] | None:
    if not selection or selection.get("requested_action_kw") is None or not selection.get("decision_start"):
        return None
    start = model_selector._dt(str(selection["decision_start"]))
    return {
        "source": "selector_quarter_control",
        "source_id": selection.get("information_vintage_id"),
        "engine_id": selection.get("routed_engine_id"),
        "decision_start": start.isoformat(),
        "valid_until": (start + timedelta(minutes=15)).isoformat(),
        "requested_action_kw": float(selection["requested_action_kw"]),
        "selector_fallback_used": bool(selection.get("fallback_used")),
        "selector_reason": selection.get("reason"),
    }


def _candidate_from_live_plan(plan: dict[str, Any]) -> dict[str, Any] | None:
    rows = plan.get("rows") or []
    if not rows:
        return None
    row = rows[0]
    start = str(row.get("start") or "")
    if not start or row.get("battery_action_kw") is None:
        return None
    valid_until = row.get("end")
    if not valid_until:
        valid_until = (model_selector._dt(start) + timedelta(minutes=float(row.get("duration_minutes") or 15.0))).isoformat()
    return {
        "source": "live_soc_replan_safety_override",
        "source_id": plan.get("generated_at"),
        "engine_id": "deterministic_v36_live",
        "decision_start": start,
        "valid_until": str(valid_until),
        "requested_action_kw": float(row["battery_action_kw"]),
        "replan_reason": plan.get("replan_reason"),
    }


_previous_optimizer_refresh = core._refresh_optimizer_plan


async def _optimizer_refresh_with_actuator():
    result = await _previous_optimizer_refresh()
    try:
        selection = await asyncio.to_thread(model_selector.latest_control_selection)
        candidate = _candidate_from_selection(selection)
        actuation = (
            {"status": "no_control_candidate", "physical_write_performed": False}
            if candidate is None
            else await ACTUATOR.process_candidate(candidate)
        )
    except Exception as exc:
        if production_status().get("physical_writes_enabled"):
            actuation = await ACTUATOR.fail_safe("quarter_actuation_exception", {"error": repr(exc)})
        else:
            actuation = {"status": "failed", "error": repr(exc), "physical_write_performed": False}
    return {**result, "actuator": actuation}


core._refresh_optimizer_plan = _optimizer_refresh_with_actuator

# v1.86's SOC monitor resolves _run_live_replan from its module globals at run
# time, so replacing that symbol lets the new live plan flow through the same
# deterministic actuator without contaminating shared challenger vintages.
_previous_live_replan = v186._run_live_replan


async def _live_replan_with_actuator(reason: str):
    result = await _previous_live_replan(reason)
    try:
        plan = await asyncio.to_thread(latest_plan, 500)
        candidate = _candidate_from_live_plan(plan)
        actuation = (
            {"status": "no_live_candidate", "physical_write_performed": False}
            if candidate is None
            else await ACTUATOR.process_candidate(candidate)
        )
    except Exception as exc:
        if production_status().get("physical_writes_enabled"):
            actuation = await ACTUATOR.fail_safe("live_replan_actuation_exception", {"error": repr(exc)})
        else:
            actuation = {"status": "failed", "error": repr(exc), "physical_write_performed": False}
    return {**result, "actuator": actuation}


v186._run_live_replan = _live_replan_with_actuator

_previous_maintenance_loop = core._forecast_maintenance_loop


async def _actuator_watchdog_loop() -> None:
    # Allow the inherited startup collector/forecast work to settle first.
    await asyncio.sleep(10)
    if _NEEDS_STARTUP_RELEASE:
        try:
            await ADAPTER.safe_release()
        except Exception:
            # Writes are already disabled in DB; status/preflight will expose the
            # unresolved inverter-side release instead of silently re-arming.
            pass
    while True:
        try:
            await ACTUATOR.watchdog_tick()
        except Exception as exc:
            if production_status().get("physical_writes_enabled"):
                try:
                    await ACTUATOR.fail_safe("watchdog_loop_exception", {"error": repr(exc)})
                except Exception:
                    pass
        await asyncio.sleep(max(10.0, float((core.cfg.get("actuator") or {}).get("watchdog_poll_seconds", 30.0))))


async def _maintenance_loop_v187() -> None:
    await asyncio.gather(_previous_maintenance_loop(), _actuator_watchdog_loop())


core._forecast_maintenance_loop = _maintenance_loop_v187

# Clean process shutdown gets an explicit zero-target + General-mode release when
# ACTIVE. A hard power/process crash cannot be guaranteed safe by the currently
# exposed Solinteg HA entities and is reported as such in actuator status.
_previous_lifespan = app.router.lifespan_context


@asynccontextmanager
async def _lifespan_v187(application):
    async with _previous_lifespan(application):
        try:
            yield
        finally:
            if production_status().get("physical_writes_enabled"):
                try:
                    await ADAPTER.safe_release()
                except Exception:
                    pass
            try:
                set_mode("shadow", reason="clean_shutdown")
            finally:
                mark_actuator_ready(False, detail="clean_shutdown")


app.router.lifespan_context = _lifespan_v187


def _install_actuator_parameters() -> None:
    definitions = [
        ui_v164.p("Actuator – Solinteg", "entity_solinteg_working_mode", "Solinteg Working Mode entity", "str", "", "Home Assistant select entity exposing Solinteg Working Mode. Leave blank for strict auto-discovery.", physical="SolaX Modbus Solinteg Working Mode select."),
        ui_v164.p("Actuator – Solinteg", "entity_solinteg_battery_power_target", "Solinteg battery power target entity", "str", "", "Home Assistant number entity exposing EMS BattCtrl Charge Discharge Power Target. Leave blank for strict auto-discovery.", physical="SolaX Modbus Solinteg register 50207 entity."),
        ui_v164.p("Actuator – Solinteg", "actuator_control_working_mode", "Control working mode", "str", "EMS BattCtrl", "Working Mode option used while Energy AI controls battery power."),
        ui_v164.p("Actuator – Solinteg", "actuator_safe_working_mode", "Safe release working mode", "str", "General", "Working Mode restored on disarm, fault or clean shutdown."),
        ui_v164.p("Actuator – safety", "actuator_soc_guard_margin_pct", "Hard-SOC guard margin", "float", 1.0, "Additional margin inside hard SOC limits enforced by the actuator over a full 15-minute control interval.", unit="percentage points", recommended="1 percentage point.", minimum=0, maximum=10, step=0.5),
        ui_v164.p("Actuator – safety", "actuator_state_max_age_seconds", "Maximum actual-state age", "int", 180, "Reject physical control when SOC/load/PV state is older than this.", unit="s", recommended="180 s with 60 s collection.", minimum=15, maximum=1800, step=15),
        ui_v164.p("Actuator – safety", "actuator_candidate_grace_seconds", "Control candidate grace", "int", 120, "Grace after the end of a decision interval before watchdog forces safe release.", unit="s", recommended="120 s.", minimum=0, maximum=900, step=15),
        ui_v164.p("Actuator – safety", "actuator_ack_timeout_seconds", "Solinteg acknowledgement timeout", "float", 8.0, "Maximum wait for Working Mode / power-target entity readback after a command.", unit="s", recommended="8 s.", minimum=1, maximum=30, step=0.5),
        ui_v164.p("Actuator – safety", "actuator_ack_tolerance_kw", "Power-target acknowledgement tolerance", "float", 0.10, "Maximum difference between requested safe target and Solinteg number-entity readback.", unit="kW", recommended="0.10 kW.", minimum=0.01, maximum=2, step=0.01),
        ui_v164.p("Actuator – safety", "actuator_zero_deadband_kw", "Zero deadband", "float", 0.05, "Safe actions smaller than this are sent as zero.", unit="kW", minimum=0, maximum=1, step=0.01),
        ui_v164.p("Actuator – safety", "actuator_min_action_change_kw", "Minimum command change", "float", 0.10, "Do not rewrite the Solinteg target for tiny optimizer changes if the previous target remains inside the current safety envelope.", unit="kW", recommended="0.10 kW.", minimum=0, maximum=2, step=0.01),
        ui_v164.p("Actuator – safety", "actuator_watchdog_poll_seconds", "Watchdog interval", "int", 30, "How often ACTIVE mode verifies Solinteg mode, target readback, candidate validity and the current safety envelope.", unit="s", recommended="30 s.", minimum=10, maximum=300, step=10),
    ]
    existing = {item.get("key") for item in ui_v164.PARAMETERS}
    for item in definitions:
        if item["key"] not in existing:
            ui_v164.PARAMETERS.append(item)
    ui_v164.PARAM_BY_KEY.clear()
    ui_v164.PARAM_BY_KEY.update({item["key"]: item for item in ui_v164.PARAMETERS})


_install_actuator_parameters()


@app.get("/actuator/status", tags=["actuator"], summary="Deterministic actuator, Solinteg path and safety status")
async def actuator_status_v187():
    return await ACTUATOR.status()


@app.get("/actuator/discover", tags=["actuator"], summary="Discover Solinteg command entities in Home Assistant")
async def actuator_discover_v187():
    try:
        return await ADAPTER.discovery_report()
    except Exception as exc:
        raise HTTPException(503, f"Solinteg discovery failed: {exc!r}")


@app.post("/actuator/preflight", tags=["actuator"], summary="Validate actuator without physical writes")
async def actuator_preflight_v187():
    return await ACTUATOR.preflight()


@app.post("/actuator/arm", tags=["actuator"], summary="Zero-power Solinteg handshake and arm actuator")
async def actuator_arm_v187(confirm: bool = Query(False)):
    if not confirm:
        raise HTTPException(400, "confirm=true is required; arming performs a physical zero-target/mode handshake")
    return await ACTUATOR.zero_handshake_and_arm()


@app.post("/actuator/disarm", tags=["actuator"], summary="Zero target, restore General mode and disarm")
async def actuator_disarm_v187():
    return await ACTUATOR.disarm("api")


@app.post("/actuator/run", tags=["actuator"], summary="Process latest selector/live control candidate now")
async def actuator_run_v187():
    plan = await asyncio.to_thread(latest_plan, 500)
    if str(plan.get("planner") or "") == "deterministic_battery_dp_v3_6_live":
        candidate = _candidate_from_live_plan(plan)
    else:
        selection = await asyncio.to_thread(model_selector.latest_control_selection)
        candidate = _candidate_from_selection(selection)
    if candidate is None:
        raise HTTPException(409, "No current control candidate is available")
    return await ACTUATOR.process_candidate(candidate)


core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD
app.openapi_schema = None
