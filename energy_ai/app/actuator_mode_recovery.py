from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import actuator_watchdog as wd
from . import deterministic_actuator as da

_INSTALLED = False
_ORIGINAL_ZERO_HANDSHAKE_AND_ARM = None
_ORIGINAL_FAIL_SAFE = None
_POLICY = "startup_mode_recovery_v1"
_RECOVERY_GRACE_SECONDS = 15 * 60
_MAX_RECOVERY_ATTEMPTS = 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _finite_zero(value: Any, tolerance: float) -> bool:
    try:
        return value is not None and abs(float(value)) <= tolerance
    except (TypeError, ValueError):
        return False


def _session_state(self) -> dict[str, Any]:
    state = getattr(self, "_energy_ai_mode_recovery", None)
    if not isinstance(state, dict) or state.get("policy") != _POLICY:
        state = {
            "policy": _POLICY,
            "armed_at": None,
            "recovery_attempts": 0,
            "last_recovery_at": None,
            "last_recovery_age_seconds": None,
            "last_recovery_readback": None,
        }
        setattr(self, "_energy_ai_mode_recovery", state)
    return state


async def zero_handshake_and_arm_with_recovery_window(self) -> dict[str, Any]:
    result = await _ORIGINAL_ZERO_HANDSHAKE_AND_ARM(self)
    if result.get("ok") and result.get("stage") == "armed":
        state = _session_state(self)
        state.update(
            {
                "armed_at": _now().isoformat(),
                "recovery_attempts": 0,
                "last_recovery_at": None,
                "last_recovery_age_seconds": None,
                "last_recovery_readback": None,
            }
        )
    return result


async def recover_startup_mode_drift_or_fail(
    self,
    reason: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(payload or {})
    if reason != "watchdog_working_mode_drift":
        return await _ORIGINAL_FAIL_SAFE(self, reason, payload)

    actuator_cfg = self.cfg.get("actuator") or {}
    tolerance = max(
        0.01,
        float(actuator_cfg.get("ack_tolerance_kw", 0.10)),
        float(actuator_cfg.get("zero_deadband_kw", 0.05)),
    )
    control_mode = str(actuator_cfg.get("control_working_mode") or "EMS BattCtrl")
    last_command = payload.get("last_command") or {}
    readback = payload.get("readback") or {}

    # Never recover a mode drift while a non-zero command is expected or observed.
    # In that case changing inverter modes could activate or suppress real power.
    if not _finite_zero(last_command.get("safe_action_kw"), tolerance):
        return await _ORIGINAL_FAIL_SAFE(self, reason, payload)
    if not _finite_zero(readback.get("battery_power_target_kw"), tolerance):
        return await _ORIGINAL_FAIL_SAFE(self, reason, payload)

    state = _session_state(self)
    armed_at = _utc(state.get("armed_at"))
    if armed_at is None:
        return await _ORIGINAL_FAIL_SAFE(self, reason, payload)
    age_seconds = (_now() - armed_at).total_seconds()
    if age_seconds < 0.0 or age_seconds > _RECOVERY_GRACE_SECONDS:
        return await _ORIGINAL_FAIL_SAFE(self, reason, payload)
    if int(state.get("recovery_attempts") or 0) >= _MAX_RECOVERY_ATTEMPTS:
        return await _ORIGINAL_FAIL_SAFE(self, reason, payload)

    # Count the attempt before touching hardware. A timeout or exception must not
    # permit an unbounded retry loop against an external controller or unstable
    # inverter. enter_control_mode_zero() always writes zero before changing mode.
    state["recovery_attempts"] = int(state.get("recovery_attempts") or 0) + 1
    state["last_recovery_age_seconds"] = round(age_seconds, 3)
    try:
        recovered = await self.adapter.enter_control_mode_zero()
    except Exception as exc:
        detail = {
            **payload,
            "startup_mode_recovery": {
                "attempt": state["recovery_attempts"],
                "age_seconds": round(age_seconds, 3),
                "error": repr(exc),
            },
        }
        return await _ORIGINAL_FAIL_SAFE(self, "watchdog_working_mode_recovery_failed", detail)

    mode_ok = str(recovered.get("working_mode")) == control_mode
    target_ok = _finite_zero(recovered.get("battery_power_target_kw"), tolerance)
    acknowledged = bool(recovered.get("acknowledged", True))
    if not (mode_ok and target_ok and acknowledged):
        detail = {
            **payload,
            "startup_mode_recovery": {
                "attempt": state["recovery_attempts"],
                "age_seconds": round(age_seconds, 3),
                "readback": recovered,
            },
        }
        return await _ORIGINAL_FAIL_SAFE(self, "watchdog_working_mode_recovery_failed", detail)

    now = _now().isoformat()
    state["last_recovery_at"] = now
    state["last_recovery_readback"] = recovered
    detail = {
        "attempt": state["recovery_attempts"],
        "age_seconds": round(age_seconds, 3),
        "previous_readback": readback,
        "recovered_readback": recovered,
        "last_command": last_command,
    }
    da._event("actuator_watchdog_mode_recovered", "startup_zero_mode_drift", detail)
    wd._record(self, "healthy_corrected", "startup_zero_mode_drift_recovered", detail)
    return {
        "status": "healthy_corrected",
        "reason": "startup_zero_mode_drift_recovered",
        "readback": recovered,
        "startup_mode_recovery": detail,
        "production": da.production_status(),
    }


def recovery_status(self) -> dict[str, Any]:
    state = dict(_session_state(self))
    armed_at = _utc(state.get("armed_at"))
    state["grace_seconds"] = _RECOVERY_GRACE_SECONDS
    state["max_recovery_attempts"] = _MAX_RECOVERY_ATTEMPTS
    state["age_seconds"] = None if armed_at is None else max(0.0, (_now() - armed_at).total_seconds())
    return state


def install_actuator_mode_recovery_patch() -> None:
    global _INSTALLED, _ORIGINAL_ZERO_HANDSHAKE_AND_ARM, _ORIGINAL_FAIL_SAFE
    if _INSTALLED:
        return
    _ORIGINAL_ZERO_HANDSHAKE_AND_ARM = da.DeterministicActuator.zero_handshake_and_arm
    _ORIGINAL_FAIL_SAFE = wd._fail_safe
    da.DeterministicActuator.zero_handshake_and_arm = zero_handshake_and_arm_with_recovery_window
    da.DeterministicActuator.mode_recovery_status = recovery_status
    wd._fail_safe = recover_startup_mode_drift_or_fail
    _INSTALLED = True
