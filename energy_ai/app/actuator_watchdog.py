from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from . import deterministic_actuator as da
from .actuator_config import effective_actuator_config_report

_INSTALLED = False
_ORIGINAL_STATUS = None
_POLICY = "watchdog_v3_consolidated"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _runtime_state(self) -> dict[str, Any]:
    state = getattr(self, "_energy_ai_watchdog_runtime", None)
    if state is None:
        state = {
            "policy": _POLICY,
            "last_checked_at": None,
            "last_status": "not_checked",
            "last_reason": None,
            "last_detail": None,
            "correction_count": 0,
            "degraded_count": 0,
            "fail_safe_count": 0,
            "last_event_keys": {},
        }
        setattr(self, "_energy_ai_watchdog_runtime", state)
    return state


def _record(self, status: str, reason: str | None = None, detail: dict[str, Any] | None = None) -> None:
    state = _runtime_state(self)
    state["last_checked_at"] = _now_iso()
    state["last_status"] = status
    state["last_reason"] = reason
    state["last_detail"] = detail
    if status == "healthy_corrected":
        state["correction_count"] = int(state.get("correction_count") or 0) + 1
    elif status.startswith("healthy_waiting") or status == "healthy_within_safety_tolerance":
        state["degraded_count"] = int(state.get("degraded_count") or 0) + 1
    elif status == "fail_safe":
        state["fail_safe_count"] = int(state.get("fail_safe_count") or 0) + 1


def _emit_throttled(
    self,
    event_type: str,
    reason: str,
    payload: dict[str, Any],
    *,
    interval_seconds: float = 300.0,
) -> None:
    state = _runtime_state(self)
    keys = state.setdefault("last_event_keys", {})
    now = datetime.now(timezone.utc)
    key = f"{event_type}:{reason}"
    last_raw = keys.get(key)
    if last_raw:
        try:
            last = datetime.fromisoformat(str(last_raw).replace("Z", "+00:00"))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if (now - last.astimezone(timezone.utc)).total_seconds() < interval_seconds:
                return
        except Exception:
            pass
    keys[key] = now.isoformat()
    da._event(event_type, reason, payload)


def _target_zero(value: Any, tolerance: float) -> bool:
    try:
        return value is not None and abs(float(value)) <= tolerance
    except Exception:
        return False


def _envelope_tolerance_kw(actuator_cfg: dict[str, Any]) -> float:
    """Small tolerance for envelope noise, never a replacement for hard limits.

    The inverter target itself is acknowledged only within ack_tolerance_kw. A
    watchdog that rewrites for smaller deltas can chatter indefinitely on sensor
    noise/rounding. Cap the accepted envelope deviation at 0.10 kW even if the
    configured acknowledgement tolerance is larger.
    """
    ack = max(0.01, float(actuator_cfg.get("ack_tolerance_kw", 0.10)))
    return min(0.10, ack)


async def _readback_confirmed(
    self,
    *,
    expected_kw: float,
    control_mode: str,
    tolerance_kw: float,
    attempts: int = 2,
) -> tuple[dict[str, Any] | None, bool, bool, list[str]]:
    """Confirm mode/target across transient HA read failures or state lag."""
    last: dict[str, Any] | None = None
    errors: list[str] = []
    mode_ok = False
    target_ok = False
    for attempt in range(max(1, attempts)):
        try:
            last = await self.adapter.readback()
            mode_ok = str(last.get("working_mode")) == control_mode
            target = last.get("battery_power_target_kw")
            target_ok = target is not None and abs(float(target) - expected_kw) <= tolerance_kw
            if mode_ok and target_ok:
                return last, True, True, errors
        except Exception as exc:
            errors.append(repr(exc))
        if attempt + 1 < attempts:
            await asyncio.sleep(0.35)
    return last, mode_ok, target_ok, errors


async def _fail_safe(self, reason: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    _record(self, "fail_safe", reason, payload)
    return await self.fail_safe(reason, payload)


async def _wait_safe_zero(
    self,
    *,
    reason: str,
    last_command: dict[str, Any],
    readback: dict[str, Any],
    actual: dict[str, Any] | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "last_command": last_command,
        "readback": readback,
        **(detail or {}),
    }
    _emit_throttled(self, "actuator_watchdog_safe_wait", reason, payload)
    result = {
        "status": "healthy_waiting_safe_zero",
        "reason": reason,
        "last_command": last_command,
        "readback": readback,
        "production": da.production_status(),
    }
    if actual is not None:
        result["actual"] = actual
    if detail:
        result.update(detail)
    _record(self, result["status"], reason, detail)
    return result


async def _watchdog_tick_impl(self) -> dict[str, Any]:
    prod = da.production_status()
    if not prod.get("physical_writes_enabled") or prod.get("operating_mode") != "active":
        result = {"status": "inactive", "production": prod}
        _record(self, "inactive", "production_not_active")
        return result
    if not prod.get("actuator_ready"):
        return await _fail_safe(self, "watchdog_active_without_actuator_ready", {"production": prod})

    effective_cfg = effective_actuator_config_report(self.cfg)
    if effective_cfg.get("restart_required"):
        return await _fail_safe(
            self,
            "watchdog_actuator_runtime_config_changed_restart_required",
            {"effective_config": effective_cfg},
        )

    last = da._last_effective_command()
    if last is None:
        return await _fail_safe(self, "active_without_successful_command")

    expected = float(last.get("safe_action_kw") or 0.0)
    actuator_cfg = self.cfg.get("actuator") or {}
    control_mode = str(actuator_cfg.get("control_working_mode") or "EMS BattCtrl")
    ack_tolerance = max(0.01, float(actuator_cfg.get("ack_tolerance_kw", 0.10)))
    zero_tolerance = max(
        ack_tolerance,
        max(0.0, float(actuator_cfg.get("zero_deadband_kw", 0.05))),
    )

    readback, mode_ok, target_ok, read_errors = await _readback_confirmed(
        self,
        expected_kw=expected,
        control_mode=control_mode,
        tolerance_kw=ack_tolerance,
    )
    if readback is None:
        return await _fail_safe(
            self,
            "watchdog_readback_failed",
            {"errors": read_errors, "last_command": last},
        )
    if not mode_ok:
        return await _fail_safe(
            self,
            "watchdog_working_mode_drift",
            {"readback": readback, "read_errors": read_errors, "last_command": last},
        )
    if not target_ok:
        return await _fail_safe(
            self,
            "watchdog_target_drift",
            {"readback": readback, "read_errors": read_errors, "last_command": last},
        )

    actual_target = readback.get("battery_power_target_kw")
    zero_held = _target_zero(expected, zero_tolerance) and _target_zero(actual_target, zero_tolerance)

    candidate = {
        "source": last.get("source"),
        "source_id": last.get("source_id"),
        "engine_id": last.get("engine_id"),
        "decision_start": last.get("decision_start"),
        "valid_until": last.get("valid_until"),
        "requested_action_kw": expected,
    }
    valid, valid_reason = da._candidate_valid(candidate, self.cfg)
    if not valid:
        # Expiry of an already-confirmed zero command is a control-pipeline
        # degradation, not a physical safety fault. Staying in EMS at zero lets a
        # later fresh candidate recover automatically without operator re-arming.
        if valid_reason == "candidate_expired" and zero_held:
            return await _wait_safe_zero(
                self,
                reason="expired_candidate_zero_target_held",
                last_command=last,
                readback=readback,
                detail={"candidate_validity_reason": valid_reason},
            )
        return await _fail_safe(
            self,
            f"watchdog_{valid_reason}",
            {"last_command": last, "readback": readback},
        )

    actual = da._latest_actual()
    if actual is None:
        if zero_held:
            return await _wait_safe_zero(
                self,
                reason="no_actual_state_zero_target_held",
                last_command=last,
                readback=readback,
            )
        return await _fail_safe(self, "watchdog_no_actual_state", {"last_command": last, "readback": readback})

    max_age = float(actuator_cfg.get("state_max_age_seconds", 180.0))
    age_value = actual.get("age_seconds")
    age = 1e9 if age_value is None else float(age_value)
    missing_fields = [name for name in ("soc_pct", "load_kw", "pv_kw") if actual.get(name) is None]
    telemetry_problem = None
    if age > max_age:
        telemetry_problem = "stale_actual_state"
    elif missing_fields:
        telemetry_problem = "actual_state_missing_soc_load_or_pv"

    if telemetry_problem:
        if zero_held:
            return await _wait_safe_zero(
                self,
                reason=f"{telemetry_problem}_zero_target_held",
                last_command=last,
                readback=readback,
                actual=actual,
                detail={
                    "age_seconds": age,
                    "state_max_age_seconds": max_age,
                    "missing_fields": missing_fields,
                },
            )
        return await _fail_safe(
            self,
            f"watchdog_{telemetry_problem}_nonzero_target",
            {
                "age_seconds": age,
                "state_max_age_seconds": max_age,
                "missing_fields": missing_fields,
                "last_command": last,
                "readback": readback,
            },
        )

    try:
        safety = da.safety_filter(candidate, self.cfg, actual)
    except Exception as exc:
        # Unexpected safety calculation failures are faults. Telemetry freshness
        # and required-field problems were classified explicitly above.
        return await _fail_safe(
            self,
            "watchdog_safety_filter_failed",
            {"error": repr(exc), "last_command": last, "readback": readback, "actual": actual},
        )

    lo = float(safety["safe_interval_kw"]["min"])
    hi = float(safety["safe_interval_kw"]["max"])
    violation = max(lo - expected, expected - hi, 0.0)
    envelope_tolerance = _envelope_tolerance_kw(actuator_cfg)

    if violation <= 1e-9:
        result = {
            "status": "healthy",
            "last_command": last,
            "readback": readback,
            "actual": actual,
            "safety": safety,
        }
        _record(self, "healthy", "within_current_safety_envelope")
        return result

    if violation <= envelope_tolerance + 1e-9:
        detail = {
            "envelope_violation_kw": round(violation, 4),
            "envelope_tolerance_kw": envelope_tolerance,
            "safe_interval_kw": {"min": lo, "max": hi},
        }
        _emit_throttled(self, "actuator_watchdog_safety_tolerance", "minor_envelope_deviation", {
            **detail,
            "last_command": last,
            "actual": actual,
        })
        result = {
            "status": "healthy_within_safety_tolerance",
            "reason": "minor_envelope_deviation",
            **detail,
            "last_command": last,
            "readback": readback,
            "actual": actual,
            "safety": safety,
        }
        _record(self, result["status"], result["reason"], detail)
        return result

    corrected = float(safety["safe_action_kw"])
    try:
        ack = await self.adapter.dispatch(corrected)
    except Exception as exc:
        return await _fail_safe(
            self,
            "watchdog_safety_correction_dispatch_failed",
            {
                "error": repr(exc),
                "old_target_kw": expected,
                "new_target_kw": corrected,
                "envelope_violation_kw": violation,
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
        "envelope_violation_kw": violation,
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
            "envelope_violation_kw": round(violation, 4),
            "safe_interval_kw": {"min": lo, "max": hi},
            "actual": actual,
            "source": last.get("source"),
            "engine_id": last.get("engine_id"),
        },
    )
    result = {
        "status": "healthy_corrected",
        "reason": "dynamic_safety_envelope",
        "old_target_kw": expected,
        "new_target_kw": corrected,
        "envelope_violation_kw": violation,
        "command_id": command_id,
        "last_command": last,
        "readback": ack,
        "actual": actual,
        "safety": safety,
        "production": da.production_status(),
    }
    _record(self, result["status"], result["reason"], {
        "old_target_kw": expected,
        "new_target_kw": corrected,
        "envelope_violation_kw": violation,
    })
    return result


async def watchdog_tick(self) -> dict[str, Any]:
    """Canonical watchdog entry point.

    The watchdog distinguishes three classes of conditions:
    - hard control-integrity/safety faults -> fail-safe and pause;
    - safe degraded conditions while the verified physical target is zero -> keep
      ACTIVE so a fresh candidate/telemetry can recover automatically;
    - normal closed-loop envelope movement -> correct only when the deviation is
      larger than the target-resolution tolerance.

    It also contains a final exception barrier. An unexpected watchdog bug must
    never be silently swallowed while physical control remains active.
    """
    try:
        return await _watchdog_tick_impl(self)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        payload = {"error": repr(exc), "policy": _POLICY}
        try:
            return await _fail_safe(self, "watchdog_unhandled_exception", payload)
        except Exception as fail_exc:
            # Last-resort local state transition if even safe_release/fail_safe
            # raises. This cannot guarantee inverter release, but it prevents the
            # process from continuing to advertise writable ACTIVE control.
            try:
                da.mark_actuator_ready(False, detail="fault:watchdog_unhandled_exception")
            except Exception:
                pass
            try:
                da.set_mode("paused", reason="actuator_fault:watchdog_unhandled_exception")
            except Exception:
                pass
            try:
                da._event(
                    "actuator_fail_safe",
                    "watchdog_unhandled_exception",
                    {"detail": payload, "fail_safe_error": repr(fail_exc)},
                )
            except Exception:
                pass
            _record(self, "fail_safe", "watchdog_unhandled_exception", {
                **payload,
                "fail_safe_error": repr(fail_exc),
            })
            return {
                "ok": False,
                "status": "fail_safe",
                "reason": "watchdog_unhandled_exception",
                "error": repr(exc),
                "fail_safe_error": repr(fail_exc),
                "production": da.production_status(),
            }


async def _status_with_watchdog(self) -> dict[str, Any]:
    data = await _ORIGINAL_STATUS(self)
    state = dict(_runtime_state(self))
    state.pop("last_event_keys", None)
    data["watchdog_runtime"] = state
    data["watchdog_runtime"]["semantics"] = {
        "hard_mode_or_target_drift_pauses": True,
        "nonzero_stale_or_missing_telemetry_pauses": True,
        "zero_target_safe_wait_recovers_automatically": True,
        "expired_zero_target_waits_for_fresh_candidate": True,
        "dynamic_envelope_correction_without_pause": True,
        "minor_envelope_tolerance_kw_max": 0.10,
        "readback_confirmation_attempts": 2,
        "unexpected_exception_barrier": True,
    }
    return data


def install_actuator_watchdog_patch() -> None:
    global _INSTALLED, _ORIGINAL_STATUS
    if _INSTALLED:
        return
    # Called after diagnostics patch installation and before the decision-start
    # scheduler captures actuator methods. Capture that fully composed status
    # implementation, then install exactly one canonical watchdog implementation.
    _ORIGINAL_STATUS = da.DeterministicActuator.status
    da.DeterministicActuator.watchdog_tick = watchdog_tick
    da.DeterministicActuator.status = _status_with_watchdog
    _INSTALLED = True
