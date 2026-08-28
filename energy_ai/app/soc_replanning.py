from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .optimizer_v36_live import latest_soc_observation


def _parse_ts(value: str) -> datetime:
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def expected_soc_at(plan: dict[str, Any], at: datetime | None = None) -> float | None:
    """Interpolate the plan SOC at one instant.

    v3.6 rows carry explicit start/end SOC. For historical v3.5 rows we infer
    start SOC from plan.initial_soc_pct / the preceding row's end SOC.
    """
    at = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    rows = list(plan.get("rows") or [])
    if not rows:
        value = plan.get("initial_soc_pct")
        return None if value is None else float(value)

    previous_soc = float(plan.get("initial_soc_pct") or rows[0].get("soc_start_pct") or 0.0)
    for row in rows:
        start = _parse_ts(str(row["start"]))
        duration_hours = float(row.get("duration_hours") or 0.25)
        end = _parse_ts(str(row["end"])) if row.get("end") else start + timedelta(hours=duration_hours)
        start_soc = float(row.get("soc_start_pct") if row.get("soc_start_pct") is not None else previous_soc)
        end_soc = float(row.get("expected_soc_pct") if row.get("expected_soc_pct") is not None else start_soc)
        if at < start:
            return previous_soc
        if start <= at < end:
            span = max(1e-6, (end - start).total_seconds())
            fraction = max(0.0, min(1.0, (at - start).total_seconds() / span))
            return start_soc + (end_soc - start_soc) * fraction
        previous_soc = end_soc
    return previous_soc


def replanning_snapshot(
    plan: dict[str, Any],
    cfg: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    optimizer = cfg.get("optimizer") or {}
    threshold = float(optimizer.get("soc_replan_threshold_pct", 2.0))
    max_age = float(optimizer.get("soc_observation_max_age_seconds", 180.0))
    obs = latest_soc_observation(now)
    if obs is None:
        return {
            "status": "soc_unavailable",
            "should_replan": False,
            "threshold_pct": threshold,
            "soc_observation_max_age_seconds": max_age,
        }
    expected = expected_soc_at(plan, now)
    if expected is None:
        return {
            "status": "plan_soc_unavailable",
            "should_replan": False,
            "actual_soc_pct": obs["soc_pct"],
            "actual_soc_observed_at": obs["observed_at"],
            "actual_soc_age_seconds": obs["age_seconds"],
            "threshold_pct": threshold,
            "soc_observation_max_age_seconds": max_age,
        }
    deviation = float(obs["soc_pct"]) - float(expected)
    stale = float(obs["age_seconds"]) > max_age
    return {
        "status": "stale_soc" if stale else "ok",
        "should_replan": (not stale) and abs(deviation) >= threshold,
        "actual_soc_pct": float(obs["soc_pct"]),
        "expected_soc_pct": float(expected),
        "deviation_pct_points": deviation,
        "absolute_deviation_pct_points": abs(deviation),
        "actual_soc_observed_at": obs["observed_at"],
        "actual_soc_age_seconds": float(obs["age_seconds"]),
        "threshold_pct": threshold,
        "soc_observation_max_age_seconds": max_age,
        "plan_generated_at": plan.get("generated_at"),
        "planner": plan.get("planner"),
    }
