from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from . import deterministic_actuator as da
from .solinteg_command import SolintegCommandAdapter

_INSTALLED = False
_ORIGINAL_RESOLVE_ENTITIES = SolintegCommandAdapter.resolve_entities
_ORIGINAL_DISPATCH = SolintegCommandAdapter.dispatch


def _utc(value: str) -> datetime:
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _candidate_safety_horizon_hours(
    candidate: dict[str, Any],
    cfg: dict[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[float, float]:
    """Return remaining legal command lifetime, including configured grace."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    valid_until = candidate.get("valid_until")
    grace = max(0.0, float((cfg.get("actuator") or {}).get("candidate_grace_seconds", 120.0)))
    if not valid_until:
        return 0.25, 900.0
    try:
        expiry = _utc(str(valid_until)) + timedelta(seconds=grace)
    except Exception:
        return 0.25, 900.0
    remaining_seconds = max(1.0, (expiry - now).total_seconds())
    return remaining_seconds / 3600.0, remaining_seconds


def _net_power(actual: dict[str, Any]) -> tuple[float, str]:
    """Resolve net load from the best available independent telemetry path.

    Preferred source is load-PV. ``net_kw`` is accepted when a caller has built
    it from another independently verified path (the watchdog uses measured grid
    plus Solinteg target readback). For generic process_candidate safety, measured
    grid+battery power provides a third redundant path.
    """
    if _finite(actual.get("net_kw")):
        return float(actual["net_kw"]), "provided_net_kw"
    if _finite(actual.get("load_kw")) and _finite(actual.get("pv_kw")):
        return max(0.0, float(actual["load_kw"])) - max(0.0, float(actual["pv_kw"])), "load_minus_pv"
    if _finite(actual.get("grid_kw")) and _finite(actual.get("battery_kw")):
        return float(actual["grid_kw"]) + float(actual["battery_kw"]), "grid_plus_battery"
    raise RuntimeError("actual_state_missing_net_inputs")


def safety_filter_time_aware(
    candidate: dict[str, Any],
    cfg: dict[str, Any],
    actual: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply hard limits using remaining horizon and redundant power telemetry."""
    requested_raw = candidate.get("requested_action_kw")
    if not _finite(requested_raw):
        raise RuntimeError("candidate_requested_action_non_finite")
    requested = float(requested_raw)

    actuator = cfg.get("actuator") or {}
    optimizer = cfg.get("optimizer") or {}
    battery = (cfg.get("policy") or {}).get("battery") or {}

    stale = float(actuator.get("state_max_age_seconds", 180.0))
    age_value = actual.get("age_seconds")
    age = 1e9 if not _finite(age_value) else float(age_value)
    if age > stale:
        raise RuntimeError(f"actual_state_stale:{age}s>{stale}s")

    if not _finite(actual.get("soc_pct")):
        raise RuntimeError("actual_state_missing_soc")
    soc = float(actual["soc_pct"])
    net, net_source = _net_power(actual)
    if not math.isfinite(net):
        raise RuntimeError("actual_state_net_non_finite")

    cap = float(battery.get("capacity_kwh", 19.6))
    hmin = float(battery.get("hard_min_soc_pct", 5.0))
    hmax = float(battery.get("hard_max_soc_pct", 100.0))
    guard = max(0.0, float(actuator.get("soc_guard_margin_pct", 1.0)))
    cmax = max(0.0, float(optimizer.get("battery_max_charge_kw", 8.0)))
    dmax = max(0.0, float(optimizer.get("battery_max_discharge_kw", 8.0)))
    ec = max(0.01, float(optimizer.get("battery_charge_efficiency", 0.95)))
    ed = max(0.01, float(optimizer.get("battery_discharge_efficiency", 0.95)))
    import_limit = max(0.0, float(optimizer.get("physical_grid_import_limit_kw", 13.8)))
    export_limit = max(0.0, float(optimizer.get("grid_export_limit_kw", 10.0)))

    dt_hours, horizon_seconds = _candidate_safety_horizon_hours(candidate, cfg, now=now)
    energy = cap * max(hmin, min(hmax, soc)) / 100.0
    min_energy = cap * min(hmax, hmin + guard) / 100.0
    max_energy = cap * max(hmin, hmax - guard) / 100.0
    discharge_by_soc = max(0.0, energy - min_energy) * ed / dt_hours
    charge_by_soc = max(0.0, max_energy - energy) / ec / dt_hours

    # grid = net - battery_action. Positive grid means import.
    lower = max(-cmax, net - import_limit, -charge_by_soc)
    upper = min(dmax, net + export_limit, discharge_by_soc)
    if lower > upper + 1e-9:
        raise RuntimeError(f"no_safe_action_interval:lower={lower:.3f},upper={upper:.3f}")

    safe = min(upper, max(lower, requested))
    zero_is_safe = lower <= 0.0 <= upper
    if soc <= hmin + guard + 1e-9 and safe > 0.0 and zero_is_safe:
        safe = 0.0
    if soc >= hmax - guard - 1e-9 and safe < 0.0 and zero_is_safe:
        safe = 0.0

    zero_deadband = max(0.0, float(actuator.get("zero_deadband_kw", 0.05)))
    zero_deadband_applied = False
    if abs(safe) < zero_deadband and zero_is_safe:
        safe = 0.0
        zero_deadband_applied = True

    predicted_grid = net - safe
    clamped = abs(safe - requested) > 1e-6
    reasons: list[str] = ["safety_clamped"] if clamped else []
    return {
        "requested_action_kw": requested,
        "safe_action_kw": round(safe, 4),
        "clamped": clamped,
        "reasons": reasons,
        "actual": actual,
        "net_kw": round(net, 4),
        "net_input_source": net_source,
        "predicted_grid_kw": round(predicted_grid, 4),
        "safe_interval_kw": {"min": round(lower, 4), "max": round(upper, 4)},
        "soc_guard": {"hard_min_pct": hmin, "hard_max_pct": hmax, "margin_pct": guard},
        "safety_horizon_seconds": round(horizon_seconds, 3),
        "safety_horizon_source": "candidate_valid_until_plus_grace",
        "zero_deadband_applied": zero_deadband_applied,
    }


async def _resolve_entities_cached(self: SolintegCommandAdapter):
    cached = getattr(self, "_energy_ai_resolved_entities", None)
    if cached is not None:
        return cached
    resolved = await _ORIGINAL_RESOLVE_ENTITIES(self)
    self._energy_ai_resolved_entities = resolved
    return resolved


async def _dispatch_with_reconciliation(self: SolintegCommandAdapter, target_kw: float) -> dict[str, Any]:
    """Reconcile ambiguous service-call failures against actual inverter state."""
    try:
        return await _ORIGINAL_DISPATCH(self, target_kw)
    except Exception as original_exc:
        control_mode = str((self.cfg.get("actuator") or {}).get("control_working_mode") or "EMS BattCtrl")
        last_error: Exception | None = None
        last_readback: dict[str, Any] | None = None
        for _ in range(3):
            try:
                entities = await self.resolve_entities()
                last_readback = await self.readback(entities)
                actual_target = last_readback.get("battery_power_target_kw")
                mode_ok = str(last_readback.get("working_mode")) == control_mode
                target_ok = (
                    _finite(actual_target)
                    and abs(float(actual_target) - float(target_kw)) <= self.ack_tolerance_kw
                )
                if mode_ok and target_ok:
                    return {
                        **last_readback,
                        "acknowledged": True,
                        "reconciled_after_dispatch_error": True,
                        "dispatch_error": repr(original_exc),
                    }
            except Exception as exc:
                last_error = exc
            await asyncio.sleep(0.25)
        if last_error is not None:
            try:
                original_exc.add_note(f"post-timeout readback also failed: {last_error!r}")
            except Exception:
                pass
        elif last_readback is not None:
            try:
                original_exc.add_note(f"post-timeout readback did not match target: {last_readback!r}")
            except Exception:
                pass
        raise original_exc


def install_actuator_runtime_resilience_patch() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    da.safety_filter = safety_filter_time_aware
    SolintegCommandAdapter.resolve_entities = _resolve_entities_cached
    SolintegCommandAdapter.dispatch = _dispatch_with_reconciliation
    _INSTALLED = True
