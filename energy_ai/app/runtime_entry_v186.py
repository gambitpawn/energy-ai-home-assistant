from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from fastapi import Query

from .optimizer_store import insert_plan, latest_plan
from .optimizer_v36_live import build_live_plan
from .runtime_entry_v185 import app, core
from .soc_replanning import replanning_snapshot
from .ui_v186 import install_ui_v186

RUNTIME_BUILD = "1.0.86"

# Normal quarter-aligned refreshes remain the frozen deterministic_v35 shared
# information vintage and continue through the robust selector. Intra-quarter
# SOC corrections are deterministic-only live replans and are deliberately not
# submitted as comparable 15-minute vintages to the challenger engines.
_normal_optimizer_refresh = core._refresh_optimizer_plan
_previous_maintenance_loop = core._forecast_maintenance_loop
_optimizer_refresh_lock = asyncio.Lock()
_replan_state: dict[str, Any] = {
    "status": "starting",
    "last_checked_at": None,
    "last_triggered_at": None,
    "last_trigger_reason": None,
    "last_error": None,
    "trigger_count": 0,
}


async def _locked_normal_optimizer_refresh():
    async with _optimizer_refresh_lock:
        return await _normal_optimizer_refresh()


# Make quarter maintenance, manual /optimizer/refresh and override-triggered
# normal refreshes mutually exclusive with an intra-quarter live replan.
core._refresh_optimizer_plan = _locked_normal_optimizer_refresh


async def _run_live_replan(reason: str) -> dict[str, Any]:
    async with _optimizer_refresh_lock:
        plan = await asyncio.to_thread(build_live_plan, core.cfg, replan_reason=reason)
        inserted = await asyncio.to_thread(insert_plan, plan)
    _replan_state.update(
        {
            "status": "replanned",
            "last_triggered_at": plan["generated_at"],
            "last_trigger_reason": reason,
            "last_error": None,
            "trigger_count": int(_replan_state.get("trigger_count") or 0) + 1,
            "last_live_plan": {
                "generated_at": plan["generated_at"],
                "planner": plan["planner"],
                "initial_soc_pct": plan["initial_soc_pct"],
                "initial_soc_observed_at": plan.get("initial_soc_observed_at"),
                "initial_soc_age_seconds": plan.get("initial_soc_age_seconds"),
                "first_interval_minutes": (plan.get("horizon_diagnostics") or {}).get("first_interval_minutes"),
                "intervals": inserted,
            },
        }
    )
    return {
        "ok": True,
        "status": "replanned",
        "reason": reason,
        "generated_at": plan["generated_at"],
        "planner": plan["planner"],
        "initial_soc_pct": plan["initial_soc_pct"],
        "first_interval_minutes": (plan.get("horizon_diagnostics") or {}).get("first_interval_minutes"),
        "comparison_eligible": False,
        "physical_writes_enabled": False,
    }


def _cooldown_elapsed(plan: dict[str, Any], now: datetime) -> tuple[bool, float]:
    o = core.cfg.get("optimizer") or {}
    minimum = max(0.0, float(o.get("soc_replan_min_interval_seconds", 60.0)))
    generated = plan.get("generated_at")
    if not generated:
        return True, minimum
    try:
        d = datetime.fromisoformat(str(generated).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        age = max(0.0, (now - d.astimezone(timezone.utc)).total_seconds())
    except Exception:
        return True, minimum
    return age >= minimum, max(0.0, minimum - age)


async def _soc_replanning_check(*, force: bool = False) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    plan = await asyncio.to_thread(latest_plan, 500)
    _replan_state["last_checked_at"] = now.isoformat()
    if plan.get("generated_at") is None or not plan.get("rows"):
        _replan_state["status"] = "no_plan"
        return {"status": "no_plan", "should_replan": False}

    snapshot = await asyncio.to_thread(replanning_snapshot, plan, core.cfg, now=now)
    cooldown_ok, cooldown_remaining = _cooldown_elapsed(plan, now)
    snapshot["cooldown_remaining_seconds"] = round(cooldown_remaining, 2)
    snapshot["cooldown_elapsed"] = cooldown_ok
    _replan_state["last_snapshot"] = snapshot

    if force:
        try:
            return await _run_live_replan("manual_force")
        except Exception as exc:
            _replan_state.update({"status": "failed", "last_error": repr(exc)})
            raise

    if not snapshot.get("should_replan"):
        _replan_state["status"] = str(snapshot.get("status") or "ok")
        return snapshot

    # Large deviations bypass the ordinary cooldown: waiting another minute when
    # the battery is already far from the modeled state defeats the safety goal.
    o = core.cfg.get("optimizer") or {}
    emergency = float(o.get("soc_replan_emergency_threshold_pct", 5.0))
    deviation = float(snapshot.get("absolute_deviation_pct_points") or 0.0)
    if not cooldown_ok and deviation < emergency:
        _replan_state["status"] = "cooldown"
        return {**snapshot, "status": "cooldown", "should_replan": True}

    reason = (
        f"soc_deviation:{snapshot.get('actual_soc_pct'):.2f}%_vs_"
        f"{snapshot.get('expected_soc_pct'):.2f}%_delta_"
        f"{snapshot.get('deviation_pct_points'):+.2f}pp"
    )
    try:
        return {**snapshot, **(await _run_live_replan(reason))}
    except Exception as exc:
        _replan_state.update({"status": "failed", "last_error": repr(exc)})
        return {**snapshot, "status": "failed", "error": repr(exc)}


async def _soc_replanning_loop() -> None:
    # Collector's first startup sample and the initial quarter plan are produced
    # by the inherited lifespan before this loop becomes relevant.
    await asyncio.sleep(15)
    while True:
        try:
            await _soc_replanning_check()
        except Exception as exc:
            _replan_state.update({"status": "failed", "last_error": repr(exc)})
        poll = max(15.0, float((core.cfg.get("collector") or {}).get("poll_seconds", 60)))
        await asyncio.sleep(poll)


async def _maintenance_loop_v186() -> None:
    await asyncio.gather(
        _previous_maintenance_loop(),
        _soc_replanning_loop(),
    )


core._forecast_maintenance_loop = _maintenance_loop_v186
install_ui_v186(app)


@app.get(
    "/optimizer/replanning/status",
    tags=["optimizer"],
    summary="Live SOC deviation monitor and intra-quarter replanning status",
)
async def optimizer_replanning_status():
    plan = await asyncio.to_thread(latest_plan, 500)
    snapshot = (
        await asyncio.to_thread(replanning_snapshot, plan, core.cfg)
        if plan.get("generated_at") is not None and plan.get("rows")
        else {"status": "no_plan", "should_replan": False}
    )
    return {
        "runtime_build": RUNTIME_BUILD,
        "normal_shared_vintage_planner": "deterministic_battery_dp_v3_5",
        "intra_quarter_live_planner": "deterministic_battery_dp_v3_6_live",
        "physical_writes_enabled": False,
        "comparison_policy": "live_variable-duration replans excluded from challenger vintage comparison",
        "configured": {
            "soc_replan_threshold_pct": float((core.cfg.get("optimizer") or {}).get("soc_replan_threshold_pct", 2.0)),
            "soc_replan_emergency_threshold_pct": float((core.cfg.get("optimizer") or {}).get("soc_replan_emergency_threshold_pct", 5.0)),
            "soc_replan_min_interval_seconds": float((core.cfg.get("optimizer") or {}).get("soc_replan_min_interval_seconds", 60.0)),
            "soc_observation_max_age_seconds": float((core.cfg.get("optimizer") or {}).get("soc_observation_max_age_seconds", 180.0)),
        },
        "monitor": dict(_replan_state),
        "current": snapshot,
    }


@app.post(
    "/optimizer/replanning/run",
    tags=["optimizer"],
    summary="Run the live SOC replanning check now",
)
async def optimizer_replanning_run(force: bool = Query(False)):
    return await _soc_replanning_check(force=force)


core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
app.version = RUNTIME_BUILD
app.openapi_schema = None
