from __future__ import annotations

from typing import Any

from . import deterministic_actuator as da
from .actuator_config import effective_actuator_config_report

_INSTALLED = False


async def watchdog_tick_with_dynamic_safety_correction(self) -> dict[str, Any]:
    """Keep ACTIVE through normal SOC/grid envelope shrinkage.

    A command that was safe when dispatched can naturally become too large as SOC,
    load or PV changes. That is a normal closed-loop condition, not an actuator
    fault. Re-dispatch the nearest currently-safe target and keep ACTIVE. Genuine
    telemetry, mode, target, configuration or dispatch failures still fail safe.
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

    control_mode = str((self.cfg.get("actuator") or {}).get("control_working_mode") or "EMS BattCtrl")
    tolerance = float((self.cfg.get("actuator") or {}).get("ack_tolerance_kw", 0.10))
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
        return await self.fail_safe("watchdog_no_actual_state")
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
