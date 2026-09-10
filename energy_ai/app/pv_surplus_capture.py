from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Any


def _utc(value: str) -> datetime:
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _row_end(row: dict[str, Any]) -> datetime:
    start = _utc(str(row["start"]))
    if row.get("end"):
        return _utc(str(row["end"]))
    if row.get("duration_hours") is not None:
        return start + timedelta(hours=float(row["duration_hours"]))
    if row.get("duration_minutes") is not None:
        return start + timedelta(minutes=float(row["duration_minutes"]))
    return start + timedelta(minutes=15)


def current_plan_row(plan: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    now = now.astimezone(timezone.utc)
    for row in plan.get("rows") or []:
        try:
            start = _utc(str(row["start"]))
            if start <= now < _row_end(row):
                return row
        except Exception:
            continue
    return None


def forecast_net_kw(row: dict[str, Any]) -> float:
    """Recover forecast load-minus-PV from one stored optimizer row.

    Prefer explicit load/PV when present. The optimizer store also persists the
    signed grid split plus the baseline battery action, which is sufficient to
    reconstruct the same net forecast for legacy rows.
    """
    if row.get("load_kw") is not None and row.get("pv_kw") is not None:
        return float(row["load_kw"]) - float(row["pv_kw"])
    return (
        float(row.get("grid_import_kw") or 0.0)
        - float(row.get("grid_export_kw") or 0.0)
        + float(row.get("battery_action_kw") or 0.0)
    )


@dataclass(frozen=True)
class CaptureDecision:
    should_dispatch: bool
    reason: str
    base_action_kw: float
    target_action_kw: float
    forecast_net_kw: float
    actual_net_kw: float
    planned_grid_kw: float
    actual_grid_on_base_kw: float
    planned_export_kw: float
    actual_export_on_base_kw: float
    extra_export_kw: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def calculate_capture(
    *,
    row: dict[str, Any],
    base_action_kw: float,
    actual_load_kw: float,
    actual_pv_kw: float,
    current_action_kw: float,
    deadband_kw: float = 0.10,
) -> CaptureDecision:
    """Capture only export above the optimizer's intended export level.

    Battery action uses the project convention: positive = discharge,
    negative = charge. The controller never tries to recreate planned grid
    import. It only absorbs *additional export* caused by a favorable net-load
    residual, so already-planned export remains untouched.
    """
    base = float(base_action_kw)
    current = float(current_action_kw)
    forecast_net = forecast_net_kw(row)
    actual_net = max(0.0, float(actual_load_kw)) - max(0.0, float(actual_pv_kw))

    planned_grid = forecast_net - base
    actual_grid_on_base = actual_net - base
    planned_export = max(0.0, -planned_grid)
    actual_export_on_base = max(0.0, -actual_grid_on_base)
    extra_export = max(0.0, actual_export_on_base - planned_export)

    target = base - extra_export
    deadband = max(0.0, float(deadband_kw))
    if extra_export <= deadband:
        target = base
        reason = "no_unplanned_export"
    else:
        reason = "capture_unplanned_export"

    should_dispatch = abs(target - current) >= deadband and abs(target - current) > 1e-9
    if not should_dispatch and reason == "capture_unplanned_export":
        reason = "capture_target_already_held"
    elif not should_dispatch and reason == "no_unplanned_export" and abs(current - base) > 1e-9:
        reason = "return_to_base_within_deadband"

    return CaptureDecision(
        should_dispatch=should_dispatch,
        reason=reason,
        base_action_kw=round(base, 6),
        target_action_kw=round(target, 6),
        forecast_net_kw=round(forecast_net, 6),
        actual_net_kw=round(actual_net, 6),
        planned_grid_kw=round(planned_grid, 6),
        actual_grid_on_base_kw=round(actual_grid_on_base, 6),
        planned_export_kw=round(planned_export, 6),
        actual_export_on_base_kw=round(actual_export_on_base, 6),
        extra_export_kw=round(extra_export, 6),
    )


class PVSurplusCaptureController:
    """Keep optimizer intent separate from temporary residual corrections."""

    SOURCE = "pv_surplus_capture"

    def __init__(self) -> None:
        self._baseline: dict[str, Any] | None = None
        self._last: dict[str, Any] = {"status": "not_checked"}

    def _observe_control(self, control: dict[str, Any] | None) -> dict[str, Any] | None:
        if not control:
            return self._baseline
        if str(control.get("source") or "") != self.SOURCE:
            action = control.get("safe_action_kw")
            if action is None:
                action = control.get("requested_action_kw")
            if action is not None:
                self._baseline = {
                    "source": control.get("source"),
                    "source_id": control.get("source_id"),
                    "engine_id": control.get("engine_id"),
                    "decision_start": control.get("decision_start"),
                    "valid_until": control.get("valid_until"),
                    "base_action_kw": float(action),
                }
        return self._baseline

    def evaluate(
        self,
        *,
        plan: dict[str, Any],
        actual: dict[str, Any] | None,
        control: dict[str, Any] | None,
        now: datetime,
        deadband_kw: float,
        max_state_age_seconds: float,
    ) -> dict[str, Any]:
        baseline = self._observe_control(control)
        now = now.astimezone(timezone.utc)
        if baseline is None:
            return self._store({"status": "no_baseline", "should_dispatch": False})
        if actual is None:
            return self._store({"status": "no_actual_state", "should_dispatch": False})
        if float(actual.get("age_seconds") or 1e9) > max(0.0, float(max_state_age_seconds)):
            return self._store({"status": "stale_actual_state", "should_dispatch": False})
        if actual.get("load_kw") is None or actual.get("pv_kw") is None or actual.get("soc_pct") is None:
            return self._store({"status": "incomplete_actual_state", "should_dispatch": False})

        valid_until = baseline.get("valid_until")
        if not valid_until:
            return self._store({"status": "baseline_missing_valid_until", "should_dispatch": False})
        try:
            if now >= _utc(str(valid_until)):
                return self._store({"status": "baseline_expired", "should_dispatch": False})
        except Exception:
            return self._store({"status": "baseline_invalid_valid_until", "should_dispatch": False})

        row = current_plan_row(plan, now)
        if row is None:
            return self._store({"status": "no_current_plan_row", "should_dispatch": False})

        current_action = control.get("safe_action_kw") if control else None
        if current_action is None and control:
            current_action = control.get("requested_action_kw")
        if current_action is None:
            current_action = baseline["base_action_kw"]

        decision = calculate_capture(
            row=row,
            base_action_kw=float(baseline["base_action_kw"]),
            actual_load_kw=float(actual["load_kw"]),
            actual_pv_kw=float(actual["pv_kw"]),
            current_action_kw=float(current_action),
            deadband_kw=deadband_kw,
        ).as_dict()
        decision.update({
            "status": "dispatch" if decision["should_dispatch"] else "steady",
            "baseline": dict(baseline),
            "plan_generated_at": plan.get("generated_at"),
            "plan_row_start": row.get("start"),
            "actual_soc_pct": float(actual["soc_pct"]),
        })
        return self._store(decision)

    def candidate(self, decision: dict[str, Any]) -> dict[str, Any]:
        baseline = decision["baseline"]
        return {
            "source": self.SOURCE,
            "source_id": baseline.get("source_id"),
            "engine_id": baseline.get("engine_id"),
            "decision_start": baseline.get("decision_start"),
            "valid_until": baseline.get("valid_until"),
            "requested_action_kw": float(decision["target_action_kw"]),
            "base_action_kw": float(decision["base_action_kw"]),
            "planned_export_kw": float(decision["planned_export_kw"]),
            "extra_export_kw": float(decision["extra_export_kw"]),
        }

    def _store(self, value: dict[str, Any]) -> dict[str, Any]:
        self._last = dict(value)
        return dict(value)

    def status(self) -> dict[str, Any]:
        return {"policy": "preserve_planned_export_capture_only_v1", **self._last}
