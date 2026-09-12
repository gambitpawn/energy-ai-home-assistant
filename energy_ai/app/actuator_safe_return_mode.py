from __future__ import annotations

from typing import Any

from . import deterministic_actuator as da
from .solinteg_command import SolintegCommandAdapter

_INSTALLED = False
_ORIGINAL_PREFLIGHT = None
_ORIGINAL_ZERO_HANDSHAKE_AND_ARM = None
_ORIGINAL_SAFE_RELEASE = None


def _zero(value: Any, tolerance: float) -> bool:
    try:
        return value is not None and abs(float(value)) <= tolerance
    except (TypeError, ValueError):
        return False


async def preflight_with_dynamic_safe_return(self) -> dict[str, Any]:
    """Allow arming from a verified zero-power non-control mode.

    The old gate required the inverter's current mode to equal a static configured
    safe mode. That is too brittle after inverter reboot/firmware transitions where
    the normal Solinteg mode can legitimately be General, EMS General or ToU. The
    active session captures the actual pre-arm mode and restores it on release.
    """
    report = await _ORIGINAL_PREFLIGHT(self)
    if report.get("error") != "safe_release_mode_mismatch_current_working_mode":
        return report

    readback = report.get("readback") or {}
    current = str(readback.get("working_mode") or "")
    control_mode = str((self.cfg.get("actuator") or {}).get("control_working_mode") or "EMS BattCtrl")
    tolerance = max(
        0.01,
        float((self.cfg.get("actuator") or {}).get("ack_tolerance_kw", 0.10)),
        float((self.cfg.get("actuator") or {}).get("zero_deadband_kw", 0.05)),
    )
    if not current or current == control_mode:
        return report
    if not _zero(readback.get("battery_power_target_kw"), tolerance):
        return report

    config_gate = dict(report.get("config_gate") or {})
    config_gate.update(
        {
            "ok": True,
            "error": None,
            "session_safe_return_mode": current,
            "static_safe_working_mode_ignored_for_session": config_gate.get("configured_safe_working_mode"),
            "warnings": [
                "Current non-control inverter mode will be captured and restored for this control session."
            ],
        }
    )
    return {
        **report,
        "ok": True,
        "error": None,
        "config_gate": config_gate,
        "warnings": config_gate["warnings"],
    }


async def arm_with_captured_return_mode(self) -> dict[str, Any]:
    """Capture the inverter mode immediately before Energy AI takes control."""
    try:
        readback = await self.adapter.readback()
        current = str(readback.get("working_mode") or "")
        control_mode = str((self.cfg.get("actuator") or {}).get("control_working_mode") or "EMS BattCtrl")
        tolerance = max(
            0.01,
            float((self.cfg.get("actuator") or {}).get("ack_tolerance_kw", 0.10)),
            float((self.cfg.get("actuator") or {}).get("zero_deadband_kw", 0.05)),
        )
        if current and current != control_mode and _zero(readback.get("battery_power_target_kw"), tolerance):
            setattr(self.adapter, "_energy_ai_session_safe_return_mode", current)
            setattr(self.adapter, "_energy_ai_session_safe_return_source", "captured_pre_arm_readback")
    except Exception:
        # The canonical preflight below remains authoritative. Failure to capture
        # here must not bypass any existing authentication/readback safety gate.
        pass

    result = await _ORIGINAL_ZERO_HANDSHAKE_AND_ARM(self)
    if result.get("ok"):
        result["session_safe_return_mode"] = getattr(
            self.adapter, "_energy_ai_session_safe_return_mode", None
        )
    return result


async def safe_release_with_session_return(self: SolintegCommandAdapter) -> dict[str, Any]:
    """Zero the battery and restore the mode that existed before this session.

    If there is no session memory and the inverter is already outside EMS BattCtrl,
    leave that current non-control mode untouched. Only an inverter still in the
    Energy AI control mode falls back to the configured static safe mode.
    """
    entities = await self.resolve_entities()
    actuator_cfg = self.cfg.get("actuator") or {}
    control_mode = str(actuator_cfg.get("control_working_mode") or "EMS BattCtrl")
    configured_safe_mode = str(actuator_cfg.get("safe_working_mode") or "General")
    session_mode = str(getattr(self, "_energy_ai_session_safe_return_mode", "") or "")
    errors: list[str] = []

    try:
        before = await self.readback(entities)
    except Exception as exc:
        before = {}
        errors.append(f"initial_readback:{exc!r}")

    current_mode = str(before.get("working_mode") or "")
    if session_mode:
        return_mode = session_mode
        return_source = str(
            getattr(self, "_energy_ai_session_safe_return_source", "captured_pre_arm_readback")
        )
    elif current_mode and current_mode != control_mode:
        return_mode = current_mode
        return_source = "current_non_control_mode_preserved"
    else:
        return_mode = configured_safe_mode
        return_source = "configured_fallback"

    try:
        await self.set_power_target(0.0, entities)
        await self.wait_for_ack(expected_target_kw=0.0, entities=entities)
    except Exception as exc:
        errors.append(f"zero_target:{exc!r}")

    try:
        current = await self.readback(entities)
        if str(current.get("working_mode")) != return_mode:
            await self.set_working_mode(return_mode, entities)
        readback = await self.wait_for_ack(expected_mode=return_mode, expected_target_kw=0.0, entities=entities)
    except Exception as exc:
        errors.append(f"safe_mode:{exc!r}")
        readback = await self.readback(entities)

    released = not errors
    if released and session_mode:
        try:
            delattr(self, "_energy_ai_session_safe_return_mode")
        except AttributeError:
            pass
        try:
            delattr(self, "_energy_ai_session_safe_return_source")
        except AttributeError:
            pass

    return {
        "released": released,
        "errors": errors,
        "readback": readback,
        "return_mode": return_mode,
        "return_mode_source": return_source,
        "configured_safe_working_mode": configured_safe_mode,
    }


def install_actuator_safe_return_mode_patch() -> None:
    global _INSTALLED, _ORIGINAL_PREFLIGHT, _ORIGINAL_ZERO_HANDSHAKE_AND_ARM, _ORIGINAL_SAFE_RELEASE
    if _INSTALLED:
        return
    _ORIGINAL_PREFLIGHT = da.DeterministicActuator.preflight
    _ORIGINAL_ZERO_HANDSHAKE_AND_ARM = da.DeterministicActuator.zero_handshake_and_arm
    _ORIGINAL_SAFE_RELEASE = SolintegCommandAdapter.safe_release
    da.DeterministicActuator.preflight = preflight_with_dynamic_safe_return
    da.DeterministicActuator.zero_handshake_and_arm = arm_with_captured_return_mode
    SolintegCommandAdapter.safe_release = safe_release_with_session_return
    _INSTALLED = True
