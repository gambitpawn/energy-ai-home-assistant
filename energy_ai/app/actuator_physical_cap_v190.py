from __future__ import annotations

from typing import Any, Callable

from . import deterministic_actuator as actuator_module

_INSTALLED = False
_ORIGINAL_SAFETY_FILTER: Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None
_ORIGINAL_PROCESS_CANDIDATE = None
_ORIGINAL_STATUS = None


def apply_physical_command_cap(safety: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Clamp the already-safe actuator action to a final physical command cap.

    This is deliberately downstream of optimizer/model selection and downstream
    of the deterministic SOC/grid safety envelope. It therefore changes only the
    physical target sent to Solinteg, never the model request or comparison data.

    `safe_interval_kw` is narrowed to the final physical interval as well. This is
    intentional: established min-change and watchdog logic already trust that
    field, so they must not retain an older command outside the commissioning cap.
    The uncapped deterministic interval is preserved separately for diagnostics.
    """
    out = dict(safety)
    actuator = cfg.get("actuator") or {}
    cap = max(0.0, float(actuator.get("max_physical_command_kw", 2.0)))
    pre_cap = float(out.get("safe_action_kw") or 0.0)
    target = max(-cap, min(cap, pre_cap))

    reasons = list(out.get("reasons") or [])
    cap_applied = abs(target - pre_cap) > 1e-9
    if cap_applied and "physical_command_cap" not in reasons:
        reasons.append("physical_command_cap")

    actual = out.get("actual") or {}
    try:
        net = max(0.0, float(actual.get("load_kw"))) - max(0.0, float(actual.get("pv_kw")))
        predicted_grid = net - target
    except Exception:
        predicted_grid = out.get("predicted_grid_kw")

    source_interval = out.get("safe_interval_kw") or {}
    deterministic_interval: dict[str, float] | None = None
    try:
        safe_lo = float(source_interval.get("min"))
        safe_hi = float(source_interval.get("max"))
        deterministic_interval = {"min": round(safe_lo, 4), "max": round(safe_hi, 4)}
        command_lo = max(safe_lo, -cap)
        command_hi = min(safe_hi, cap)
    except Exception:
        command_lo, command_hi = -cap, cap

    physical_interval = {"min": round(command_lo, 4), "max": round(command_hi, 4)}
    out.update(
        {
            "pre_cap_safe_action_kw": round(pre_cap, 4),
            "physical_target_kw": round(target, 4),
            "physical_command_cap_kw": round(cap, 4),
            # Preserve the established field as the actual command that may be
            # dispatched. Existing persistence/watchdog code therefore remains
            # authoritative without a second command-value channel.
            "safe_action_kw": round(target, 4),
            "clamped": bool(out.get("clamped")) or cap_applied,
            "cap_applied": cap_applied,
            "reasons": reasons,
            "predicted_grid_kw": None if predicted_grid is None else round(float(predicted_grid), 4),
            "deterministic_safe_interval_kw": deterministic_interval,
            "physical_command_interval_kw": physical_interval,
            # Existing actuator hold/watchdog logic reads this key. Narrowing it
            # is what makes the cap a real downstream safety boundary.
            "safe_interval_kw": physical_interval,
        }
    )
    return out


def _promote_cap_diagnostics(result: dict[str, Any]) -> dict[str, Any]:
    safety = result.get("safety")
    if not isinstance(safety, dict):
        return result
    out = dict(result)
    if safety.get("pre_cap_safe_action_kw") is not None:
        out["pre_cap_safe_action_kw"] = safety.get("pre_cap_safe_action_kw")
    # For held_existing, result.safe_action_kw is the command actually retained;
    # otherwise it is the target that was/would be dispatched.
    if out.get("safe_action_kw") is not None:
        out["physical_target_kw"] = float(out["safe_action_kw"])
    elif safety.get("physical_target_kw") is not None:
        out["physical_target_kw"] = safety.get("physical_target_kw")
    out["physical_command_cap_kw"] = safety.get("physical_command_cap_kw")
    out["cap_applied"] = bool(safety.get("cap_applied"))
    return out


def install_physical_command_cap_patch() -> None:
    global _INSTALLED, _ORIGINAL_SAFETY_FILTER, _ORIGINAL_PROCESS_CANDIDATE, _ORIGINAL_STATUS
    if _INSTALLED:
        return
    _ORIGINAL_SAFETY_FILTER = actuator_module.safety_filter
    _ORIGINAL_PROCESS_CANDIDATE = actuator_module.DeterministicActuator.process_candidate
    _ORIGINAL_STATUS = actuator_module.DeterministicActuator.status

    def capped_safety_filter(candidate: dict[str, Any], cfg: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
        if _ORIGINAL_SAFETY_FILTER is None:
            raise RuntimeError("physical command cap patch is not initialized")
        base = _ORIGINAL_SAFETY_FILTER(candidate, cfg, actual)
        return apply_physical_command_cap(base, cfg)

    async def capped_process_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        if _ORIGINAL_PROCESS_CANDIDATE is None:
            raise RuntimeError("physical command cap process patch is not initialized")
        result = await _ORIGINAL_PROCESS_CANDIDATE(self, candidate)
        return _promote_cap_diagnostics(result)

    async def capped_status(self) -> dict[str, Any]:
        if _ORIGINAL_STATUS is None:
            raise RuntimeError("physical command cap status patch is not initialized")
        result = await _ORIGINAL_STATUS(self)
        semantics = dict(result.get("safety_semantics") or {})
        semantics["downstream_physical_command_cap"] = True
        result["safety_semantics"] = semantics
        configuration = dict(result.get("configuration_safety") or {})
        configuration["physical_command_cap_change_requires_restart"] = True
        result["configuration_safety"] = configuration
        return result

    actuator_module.safety_filter = capped_safety_filter
    actuator_module.DeterministicActuator.process_candidate = capped_process_candidate
    actuator_module.DeterministicActuator.status = capped_status
    _INSTALLED = True
