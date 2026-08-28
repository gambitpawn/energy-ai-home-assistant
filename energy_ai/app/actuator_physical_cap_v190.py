from __future__ import annotations

from typing import Any, Callable

from . import deterministic_actuator as actuator_module

_INSTALLED = False
_ORIGINAL_SAFETY_FILTER: Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None


def apply_physical_command_cap(safety: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Clamp the already-safe actuator action to a final physical command cap.

    This is deliberately downstream of optimizer/model selection and downstream
    of the deterministic SOC/grid safety envelope. It therefore changes only the
    physical target sent to Solinteg, never the model request or comparison data.
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

    safe_interval = out.get("safe_interval_kw") or {}
    try:
        safe_lo = float(safe_interval.get("min"))
        safe_hi = float(safe_interval.get("max"))
        command_lo = max(safe_lo, -cap)
        command_hi = min(safe_hi, cap)
    except Exception:
        command_lo, command_hi = -cap, cap

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
            "physical_command_interval_kw": {
                "min": round(command_lo, 4),
                "max": round(command_hi, 4),
            },
        }
    )
    return out


def install_physical_command_cap_patch() -> None:
    global _INSTALLED, _ORIGINAL_SAFETY_FILTER
    if _INSTALLED:
        return
    _ORIGINAL_SAFETY_FILTER = actuator_module.safety_filter

    def capped_safety_filter(candidate: dict[str, Any], cfg: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
        if _ORIGINAL_SAFETY_FILTER is None:
            raise RuntimeError("physical command cap patch is not initialized")
        base = _ORIGINAL_SAFETY_FILTER(candidate, cfg, actual)
        return apply_physical_command_cap(base, cfg)

    actuator_module.safety_filter = capped_safety_filter
    _INSTALLED = True
