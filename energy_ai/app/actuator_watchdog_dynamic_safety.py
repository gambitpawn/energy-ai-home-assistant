from __future__ import annotations

from typing import Any

from . import deterministic_actuator as da
from .actuator_config import effective_actuator_config_report

_INSTALLED = False


async def watchdog_tick_with_dynamic_safety_correction(self) -> dict[str, Any]:
    """Keep ACTIVE through normal closed-loop corrections without weakening hard faults.

    Two conditions are handled without pausing production:
    1. A previously safe command moves just outside the current SOC/grid envelope;
       it is clamped and re-dispatched.
    2. Telemetry becomes stale while the inverter is already confirmed in control
       mode at an effectively zero target; the watchdog keeps the zero target and
       waits for fresh telemetry instead of turning a harmless observation gap into
       an operator-required pause.

    Stale telemetry with a non-zero target remains a fail-safe condition, as do
    mode drift, target drift, configuration drift and dispatch/readback failures.
    """
    prod = da.production_status()
    if not prod.get("physical_writes_enabled") or prod.get("operating_mode") != "active":
        return {"status": "inactive", "production": prod}

    effective_cfg = effective_actuator_config_report(self.cfg)
    if effective_cfg.get("restart_required"):
        return await self.fail_safe(
            "watchdog_actuator_runtime_config_changed_restart_required",
            {"effective_config": effective_cfg},
        )

    last = da._last_effective_command()
    if last is None:
        return await self.fail_safe("active_without_successful_command")

    candidate = {
        "source": last.get("source"),
        "source_id": last.get("source_id"),
        "engine_id": last.get("engine_id"),
        "decision_start": last.get("decision_start"),
        "valid_until": last.get("valid_until"),
        "requested_action_kw": last.get("safe_action_kw"),
    }
    valid, reason = da._candidate_valid(candidate, self.cfg)
    if not valid:
        return await self.fail_safe(f"watchdog_{reason}", {"last_command": last})

    try:
        readback = await self.adapter.readback()
    except Exception as exc:
        return await self.fail_safe("watchdog_readback_failed", {"error": repr(exc)})

    actuator_cfg = self.cfg.get("actuator") or {}
    control_mode = str(actuator_cfg.get("control_working_mode") or "EMS BattCtrl")
    tolerance = float(actuator_cfg.get("ack_tolerance_kw", 0.10))
    zero_deadband = max(0.0, float(actuator_cfg.get("zero_deadband_kw", 0.05)))
    actual_target = readback.get("battery_power_target_kw")
    expected = float(last.get("safe_action_kw") or 0.0)

    if str(readback.get("working_mode")) != control_mode:
        return await self.fail_safe(
            "watchdog_working_mode_drift",
            {"readback": readback, "last_command": last},
        )
    if actual_target is None or abs(float(actual_target) - expected) > tolerance:
        return await self.fail_safe(
            "watchdog_target_drift",
            {"readback": readback, "last_command": last},
        )

    actual = da._latest_actual()
    if actual is None:
        if abs(expected) <= zero_deadband and abs(float(actual_target)) <= max(zero_deadband, tolerance):
            da._event(
                "actuator_watchdog_telemetry_wait",
                "no_actual_state_zero_target_held",
                {"last_command": last, "readback": readback},
            )
            return {
                "status": "healthy_waiting_for_telemetry",
                "reason": "no_actual_state_zero_target_held",
                "last_command": last,
                "readback": readback,
                "production": prod,
            }
        return await self.fail_safe("watchdog_no_actual_state")

    max_age = float(actuator_cfg.get("state_max_age_seconds", 180.0))
    age = float(actual.get("age_seconds") or 1e9)
    if age > max_age:
        if abs(expected) <= zero_deadband and abs(float(actual_target)) <= max(zero_deadband, tolerance):
            da._event(
                "actuator_watchdog_telemetry_wait",
                "stale_actual_state_zero_target_held",
                {
                    "age_seconds": age,
                    "state_max_age_seconds": max_age,
                    "last_command": last,
                    "readback": readback,
                },
            )
            return {
                "status": "healthy_waiting_for_telemetry",
                "reason": "stale_actual_state_zero_target_held",
                "age_seconds": age,
                "state_max_age_seconds": max_age,
                "last_command": last,
                "readback": readback,
                "actual": actual,
                "production": prod,
            }
        return await self.fail_safe(
            "watchdog_actual_state_stale_nonzero_target",
            {
                "age_seconds": age,
                "state_max_age_seconds": max_age,
                "last_command": last,
                "readback": readback,
            },
        )

    try:
        safety = da.safety_filter(candidate, self.cfg, actual)
    except Exception as exc:
        return await self.fail_safe(
            "watchdog_safety_filter_failed",
            {"error": repr(exc), "last_command": last},
        )

    lo = float(safety["safe_interval_kw"]["min"])
    hi = float(safety["safe_interval_kw"]["max"])
    if expected < lo - 1e-6 or expected > hi + 1e-6:
        corrected = float(safety["safe_action_kw"])
        try:
            ack = await self.adapter.dispatch(corrected)
        except Exception as exc:
            return await self.fail_safe(
                "watchdog_safety_correction_dispatch_failed",
                {
                    "error": repr(exc),
                    "old_target_kw": expected,
                    "new_target_kw": corrected,
                    "safety": safety,
                    "last_command": last,
                },
            )

        payload = {
            "status": "acknowledged",
            "reason": "watchdog_dynamic_safety_correction",
            "requested_action_kw": expected,
            "safe_action_kw": corrected,
            "old_target_kw": expected,
            "new_target_kw": corrected,
            "safety": safety,
            "readback": ack,
            "physical_write_performed": True,
        }
        command_id = da._insert_command(
            candidate,
            safe_action_kw=corrected,
            physical_write=True,
            status="acknowledged",
            reason="watchdog_dynamic_safety_correction",
            payload=payload,
        )
        da._event(
            "actuator_watchdog_safety_correction",
            "dynamic_safety_envelope",
            {
                "command_id": command_id,
                "old_target_kw": expected,
                "new_target_kw": corrected,
                "safe_interval_kw": {"min": lo, "max": hi},
                "actual": actual,
                "source": last.get("source"),
                "engine_id": last.get("engine_id"),
            },
        )
        return {
            "status": "healthy_corrected",
            "reason": "dynamic_safety_envelope",
            "old_target_kw": expected,
            "new_target_kw": corrected,
            "command_id": command_id,
            "last_command": last,
            "readback": ack,
            "actual": actual,
            "safety": safety,
            "production": da.production_status(),
        }

    return {
        "status": "healthy",
        "last_command": last,
        "readback": readback,
        "actual": actual,
        "safety": safety,
    }


def install_dynamic_safety_watchdog_patch() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    da.DeterministicActuator.watchdog_tick = watchdog_tick_with_dynamic_safety_correction
    _INSTALLED = True
