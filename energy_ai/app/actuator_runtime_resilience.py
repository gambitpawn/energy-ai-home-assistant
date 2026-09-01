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


def _candidate_safety_horizon_hours(
    candidate: dict[str, Any],
    cfg: dict[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[float, float]:
    """Return the remaining time for which the current command can remain active.

    The old actuator safety filter always projected the *current* command across a
    fresh 15-minute interval. During an already-running interval this repeatedly
    moved the SOC envelope inward, even though the command had only a few minutes
    left to run. Near a SOC guard this made the watchdog chase the boundary with a
    new write every minute.

    Candidate validity already defines the real control horizon. Include the same
    grace period used by _candidate_valid because the previous command can legally
    remain active while the next decision is being dispatched.
    """
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


def safety_filter_time_aware(
    candidate: dict[str, Any],
    cfg: dict[str, Any],
    actual: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply hard actuator limits over the command's *remaining* validity horizon."""
    requested = float(candidate["requested_action_kw"])
    actuator = cfg.get("actuator") or {}
    optimizer = cfg.get("optimizer") or {}
    battery = (cfg.get("policy") or {}).get("battery") or {}

    stale = float(actuator.get("state_max_age_seconds", 180.0))
    age_value = actual.get("age_seconds")
    age = 1e9 if age_value is None else float(age_value)
    if age > stale:
        raise RuntimeError(f"actual_state_stale:{age}s>{stale}s")

    soc = actual.get("soc_pct")
    load = actual.get("load_kw")
    pv = actual.get("pv_kw")
    if soc is None or load is None or pv is None:
        raise RuntimeError("actual_state_missing_soc_load_or_pv")

    soc = float(soc)
    load = max(0.0, float(load))
    pv = max(0.0, float(pv))
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

    net = load - pv
    # grid = net - battery_action. Positive grid means import.
    lower = max(-cmax, net - import_limit, -charge_by_soc)
    upper = min(dmax, net + export_limit, discharge_by_soc)
    if lower > upper + 1e-9:
        raise RuntimeError(f"no_safe_action_interval:lower={lower:.3f},upper={upper:.3f}")

    safe = min(upper, max(lower, requested))
    if soc <= hmin + guard + 1e-9 and safe > 0.0:
        safe = 0.0
    if soc >= hmax - guard - 1e-9 and safe < 0.0:
        safe = 0.0

    zero_deadband = max(0.0, float(actuator.get("zero_deadband_kw", 0.05)))
    if abs(safe) < zero_deadband:
        safe = 0.0

    predicted_grid = net - safe
    clamped = abs(safe - requested) > 1e-6
    reasons: list[str] = ["safety_clamped"] if clamped else []
    return {
        "requested_action_kw": requested,
        "safe_action_kw": round(safe, 4),
        "clamped": clamped,
        "reasons": reasons,
        "actual": actual,
        "predicted_grid_kw": round(predicted_grid, 4),
        "safe_interval_kw": {"min": round(lower, 4), "max": round(upper, 4)},
        "soc_guard": {"hard_min_pct": hmin, "hard_max_pct": hmax, "margin_pct": guard},
        "safety_horizon_seconds": round(horizon_seconds, 3),
        "safety_horizon_source": "candidate_valid_until_plus_grace",
    }


async def _resolve_entities_cached(self: SolintegCommandAdapter):
    """Cache the resolved Solinteg entity pair for the lifetime of the add-on.

    With blank explicit entity options, the original implementation called
    Home Assistant all_states() on every watchdog readback and every dispatch.
    Runtime configuration changes already require restart, so repeating discovery
    every 30 seconds adds load and failure surface without providing freshness.
    """
    cached = getattr(self, "_energy_ai_resolved_entities", None)
    if cached is not None:
        return cached
    resolved = await _ORIGINAL_RESOLVE_ENTITIES(self)
    self._energy_ai_resolved_entities = resolved
    return resolved


async def _dispatch_with_reconciliation(self: SolintegCommandAdapter, target_kw: float) -> dict[str, Any]:
    """Reconcile an ambiguous HTTP timeout before declaring command failure.

    A Home Assistant service POST can time out after the write has already been
    accepted. In that case an immediate fail-safe is a false negative. The write
    is idempotent, but we do not blindly retry it here: first verify the actual
    inverter mode/target. If they already match, treat the command as acknowledged;
    otherwise preserve the original exception so the actuator can fail safe.
    """
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
                target_ok = actual_target is not None and abs(float(actual_target) - float(target_kw)) <= self.ack_tolerance_kw
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
