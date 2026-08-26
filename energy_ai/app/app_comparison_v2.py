from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .app_comparison import compare_app_vs_planner as _compare_v1, resolve_window

ENGINE_NAME = "app_vs_shadow_planner_v2"
WINNER_EPSILON_ORE = 1.0


def _aligned_quarter(d: datetime) -> bool:
    return d.minute % 15 == 0 and d.second == 0 and d.microsecond == 0


def compare_app_vs_planner(
    cfg: dict[str, Any],
    *,
    start: str | None = None,
    end: str | None = None,
    hours: int | None = None,
    days: int | None = None,
    min_plan_coverage: float = 0.90,
    min_actual_coverage: float = 0.90,
    include_rows: bool = True,
) -> dict[str, Any]:
    a, b = resolve_window(start=start, end=end, hours=hours, days=days)
    if not _aligned_quarter(a) or not _aligned_quarter(b):
        raise ValueError("comparison start and end must align to 15-minute boundaries")
    completed_boundary = datetime.now(timezone.utc).replace(
        minute=(datetime.now(timezone.utc).minute // 15) * 15,
        second=0,
        microsecond=0,
    )
    if b > completed_boundary:
        raise ValueError("comparison end must not extend beyond the latest completed 15-minute interval")

    result = _compare_v1(
        cfg,
        start=a.isoformat(),
        end=b.isoformat(),
        min_plan_coverage=min_plan_coverage,
        min_actual_coverage=min_actual_coverage,
        include_rows=include_rows,
    )
    result["engine"] = ENGINE_NAME

    data = result.setdefault("data", {})
    expected_first = a.isoformat()
    expected_last = (b - timedelta(minutes=15)).isoformat()
    boundary_complete = data.get("first") == expected_first and data.get("last") == expected_last
    data["start_boundary_complete"] = data.get("first") == expected_first
    data["end_boundary_complete"] = data.get("last") == expected_last
    data["boundary_complete"] = boundary_complete

    if result.get("status") == "valid" and not boundary_complete:
        result["status"] = "missing_boundary_data"
        result["valid_comparison"] = False
        result["winner"] = None

    if result.get("valid_comparison"):
        advantage = float((result.get("comparison") or {}).get("planner_advantage_ore") or 0.0)
        if advantage > WINNER_EPSILON_ORE:
            result["winner"] = "shadow_planner"
        elif advantage < -WINNER_EPSILON_ORE:
            result["winner"] = "actual_app"
        else:
            result["winner"] = "tie"
        result.setdefault("comparison", {})["winner_epsilon_ore"] = WINNER_EPSILON_ORE

    result.setdefault("limitations", []).append(
        "a valid head-to-head result also requires the first and last requested 15-minute buckets to be present so start/terminal SOC refer to the requested window"
    )
    return result
