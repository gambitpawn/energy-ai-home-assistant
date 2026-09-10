from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from .optimizer_store import latest_plan
from .production_state import status as production_status
from .pv_surplus_capture import PVSurplusCaptureController


def install_pv_surplus_capture(base) -> dict[str, Any]:
    """Add cheap residual capture ahead of the existing SOC replanning check.

    This deliberately does not run the optimizer on PV/load fluctuations. It
    modifies only the current physical battery target, using the optimizer's
    intended export as a floor, and lets the existing SOC deviation machinery
    trigger a full replan when accumulated captured energy becomes material.
    """
    existing = getattr(base, "_PV_SURPLUS_CAPTURE_CONTROLLER", None)
    if isinstance(existing, PVSurplusCaptureController):
        return {"installed": True, "already_installed": True, "policy": existing.status()["policy"]}

    controller = PVSurplusCaptureController()
    setattr(base, "_PV_SURPLUS_CAPTURE_CONTROLLER", controller)

    async def capture_check() -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        prod = production_status()
        if not prod.get("physical_writes_enabled") or prod.get("operating_mode") != "active":
            result = {"status": "inactive", "should_dispatch": False}
            base._REPLAN_STATE["pv_surplus_capture"] = result
            return result

        async with base._OPTIMIZER_REFRESH_LOCK:
            plan = await asyncio.to_thread(latest_plan, 500)
            if plan.get("generated_at") is None or not plan.get("rows"):
                result = {"status": "no_plan", "should_dispatch": False}
                base._REPLAN_STATE["pv_surplus_capture"] = result
                return result

            actual = base.ACTUATOR.current_actual()
            control = base.ACTUATOR.current_control_command()
            actuator_cfg = base.core.cfg.get("actuator") or {}
            deadband = max(
                float(actuator_cfg.get("zero_deadband_kw", 0.05)),
                float(actuator_cfg.get("min_action_change_kw", 0.10)),
            )
            decision = controller.evaluate(
                plan=plan,
                actual=actual,
                control=control,
                now=now,
                deadband_kw=deadband,
                max_state_age_seconds=float(actuator_cfg.get("state_max_age_seconds", 180.0)),
            )
            base._REPLAN_STATE["pv_surplus_capture"] = controller.status()
            if not decision.get("should_dispatch"):
                return decision

            candidate = controller.candidate(decision)
            try:
                actuation = await base.ACTUATOR.process_candidate(candidate)
            except Exception as exc:
                result = {**decision, "status": "failed", "error": repr(exc)}
                base._REPLAN_STATE["pv_surplus_capture"] = result
                return result

            result = {**decision, "actuator": actuation}
            base._REPLAN_STATE["pv_surplus_capture"] = result
            return result

    async def residual_aware_replanning_loop() -> None:
        await asyncio.sleep(15)
        while True:
            try:
                await capture_check()
            except Exception as exc:
                base._REPLAN_STATE["pv_surplus_capture"] = {
                    "status": "failed",
                    "should_dispatch": False,
                    "error": repr(exc),
                }
            try:
                await base.soc_replanning_check()
            except Exception as exc:
                base._REPLAN_STATE.update({"status": "failed", "last_error": repr(exc)})
            await asyncio.sleep(
                max(15.0, float((base.core.cfg.get("collector") or {}).get("poll_seconds", 60)))
            )

    base._soc_replanning_loop = residual_aware_replanning_loop
    base.pv_surplus_capture_check = capture_check
    return {
        "installed": True,
        "already_installed": False,
        "policy": "preserve_planned_export_capture_only_v1",
        "full_replan_trigger": "existing_soc_deviation_threshold",
        "optimizer_runs_per_residual_check": 0,
    }
