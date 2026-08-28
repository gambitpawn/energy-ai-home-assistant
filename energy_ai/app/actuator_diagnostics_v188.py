from __future__ import annotations

from typing import Any

from .actuator_config import actuator_preflight_config_gate, effective_actuator_config_report
from .deterministic_actuator import DeterministicActuator
from .production_state import status as production_status

_INSTALLED = False
_ORIGINAL_PREFLIGHT = DeterministicActuator.preflight
_ORIGINAL_STATUS = DeterministicActuator.status
_ORIGINAL_PROCESS_CANDIDATE = DeterministicActuator.process_candidate
_ORIGINAL_WATCHDOG_TICK = DeterministicActuator.watchdog_tick


def _effective(self: DeterministicActuator) -> dict[str, Any]:
    return effective_actuator_config_report(self.cfg)


async def _preflight_v188(self: DeterministicActuator) -> dict[str, Any]:
    base = await _ORIGINAL_PREFLIGHT(self)
    effective = _effective(self)
    current_mode = None
    if isinstance(base.get("readback"), dict):
        current_mode = base["readback"].get("working_mode")
    gate = actuator_preflight_config_gate(effective, current_mode)

    base["effective_config"] = effective
    base["config_gate"] = gate
    warnings = list(base.get("warnings") or [])
    warnings.extend(gate.get("warnings") or [])
    base["warnings"] = warnings

    if not gate.get("ok"):
        base["ok"] = False
        base["error"] = gate.get("error") or "actuator_configuration_gate_failed"
    return base


async def _status_v188(self: DeterministicActuator) -> dict[str, Any]:
    result = await _ORIGINAL_STATUS(self)
    result["effective_config"] = _effective(self)
    result["configuration_safety"] = {
        "ui_changes_require_restart": True,
        "stale_runtime_blocks_arm": True,
        "stale_runtime_blocks_active": True,
        "stale_runtime_during_active_triggers_fail_safe": True,
        "safe_release_mode_must_match_current_normal_mode_before_arm": True,
    }
    return result


async def _process_candidate_v188(self: DeterministicActuator, candidate: dict[str, Any]) -> dict[str, Any]:
    prod = production_status()
    if prod.get("physical_writes_enabled") or prod.get("operating_mode") == "active":
        effective = _effective(self)
        if effective.get("restart_required"):
            return await self.fail_safe(
                "actuator_runtime_config_changed_restart_required",
                {"effective_config": effective, "candidate": candidate},
            )
    return await _ORIGINAL_PROCESS_CANDIDATE(self, candidate)


async def _watchdog_tick_v188(self: DeterministicActuator) -> dict[str, Any]:
    prod = production_status()
    if prod.get("physical_writes_enabled") or prod.get("operating_mode") == "active":
        effective = _effective(self)
        if effective.get("restart_required"):
            return await self.fail_safe(
                "watchdog_actuator_runtime_config_changed_restart_required",
                {"effective_config": effective},
            )
    return await _ORIGINAL_WATCHDOG_TICK(self)


def install_actuator_diagnostics_patch() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    DeterministicActuator.preflight = _preflight_v188
    DeterministicActuator.status = _status_v188
    DeterministicActuator.process_candidate = _process_candidate_v188
    DeterministicActuator.watchdog_tick = _watchdog_tick_v188
    _INSTALLED = True
