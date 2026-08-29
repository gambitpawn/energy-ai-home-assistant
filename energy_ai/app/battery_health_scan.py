from __future__ import annotations

import copy
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .battery_health_cost import DEFAULT_BATTERY_HEALTH_PARAMETERS, BatteryHealthParameters
from .battery_health_hindsight import (
    DEFAULT_GRID_STEP_KWH,
    HINDSIGHT_VERSION,
    compare_profiles_for_rows,
    solve_hindsight_rows,
)
from .optimizer_evaluation import _actual_rows

LOCAL_TZ = ZoneInfo("Europe/Stockholm")
SCAN_VERSION = "battery_health_high_soc_scan_v1"
DEFAULT_SCAN_GRID_STEP_KWH = 0.2
DEFAULT_SCAN_DAYS = 30
DEFAULT_SCAN_LIMIT = 10

OFF_PARAMETERS = replace(
    DEFAULT_BATTERY_HEALTH_PARAMETERS,
    high_soc_enabled=False,
    high_soc_zone_1_cost_ore_per_kwh_hour=0.0,
    high_soc_zone_2_cost_ore_per_kwh_hour=0.0,
    high_soc_zone_3_cost_ore_per_kwh_hour=0.0,
)


def off_profile_parameters() -> BatteryHealthParameters:
    return OFF_PARAMETERS.validated()


def _day_inputs(day: date) -> dict[str, Any]:
    rows, data = _actual_rows(day)
    expected = int(data.get("expected_intervals") or 0)
    coverage = float(data.get("actual_coverage_fraction") or 0.0)
    if expected <= 0 or coverage < 0.98 or len(rows) != expected:
        return {
            "status": "insufficient_actual_coverage",
            "rows": rows,
            "data": data,
            "initial_soc_pct": None,
            "terminal_soc_pct": None,
        }
    first_soc = next(
        (r.get("battery_soc_start_pct") for r in rows if r.get("battery_soc_start_pct") is not None),
        None,
    )
    terminal_soc = next(
        (r.get("battery_soc_end_pct") for r in reversed(rows) if r.get("battery_soc_end_pct") is not None),
        None,
    )
    if first_soc is None:
        status = "missing_initial_soc"
    elif terminal_soc is None:
        status = "missing_terminal_soc"
    else:
        status = "ok"
    return {
        "status": status,
        "rows": rows,
        "data": data,
        "initial_soc_pct": float(first_soc) if first_soc is not None else None,
        "terminal_soc_pct": float(terminal_soc) if terminal_soc is not None else None,
    }


def compare_profiles_with_off_for_rows(
    cfg: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    initial_soc_pct: float,
    terminal_soc_pct: float,
    grid_step_kwh: float = DEFAULT_GRID_STEP_KWH,
    include_actions: bool = False,
) -> dict[str, Any]:
    comparison = compare_profiles_for_rows(
        cfg,
        rows,
        initial_soc_pct=initial_soc_pct,
        terminal_soc_pct=terminal_soc_pct,
        grid_step_kwh=grid_step_kwh,
        include_actions=include_actions,
    )
    comparison["profiles"] = {
        "off": solve_hindsight_rows(
            cfg,
            rows,
            initial_soc_pct=initial_soc_pct,
            terminal_soc_pct=terminal_soc_pct,
            parameters=OFF_PARAMETERS,
            grid_step_kwh=grid_step_kwh,
            include_actions=include_actions,
        ),
        **comparison["profiles"],
    }
    return comparison


def compare_profiles_with_off_for_day(
    cfg: dict[str, Any],
    local_date: str | date,
    *,
    grid_step_kwh: float = DEFAULT_GRID_STEP_KWH,
    include_actions: bool = False,
) -> dict[str, Any]:
    day = date.fromisoformat(local_date) if isinstance(local_date, str) else local_date
    inputs = _day_inputs(day)
    if inputs["status"] != "ok":
        return {
            "diagnostic_only": True,
            "planner_integration_enabled": False,
            "selector_integration_enabled": False,
            "physical_write_performed": False,
            "hindsight_version": HINDSIGHT_VERSION,
            "local_date": day.isoformat(),
            "status": inputs["status"],
            "data": inputs["data"],
        }
    initial = float(inputs["initial_soc_pct"])
    terminal = float(inputs["terminal_soc_pct"])
    comparison = compare_profiles_with_off_for_rows(
        cfg,
        inputs["rows"],
        initial_soc_pct=initial,
        terminal_soc_pct=terminal,
        grid_step_kwh=grid_step_kwh,
        include_actions=include_actions,
    )
    comparison.update(
        {
            "local_date": day.isoformat(),
            "status": "ok",
            "data": inputs["data"],
            "terminal_semantics": "fixed_to_observed_day_terminal_soc_for_profile_comparability",
            "observed_initial_soc_pct": round(initial, 4),
            "observed_terminal_soc_pct": round(terminal, 4),
        }
    )
    return comparison


def economic_value_above_soc_threshold_for_rows(
    cfg: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    initial_soc_pct: float,
    terminal_soc_pct: float,
    threshold_pct: float = 90.0,
    grid_step_kwh: float = DEFAULT_SCAN_GRID_STEP_KWH,
    unrestricted_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Measure the objective value of capacity above a SOC threshold.

    The comparison is valid only when both boundary SOC values are at or below
    the threshold. Both solves use the OFF health profile, so the difference is
    the economic/control-policy value of allowing storage above the threshold,
    not a battery-health price.
    """
    threshold = float(threshold_pct)
    if not (0.0 < threshold <= 100.0):
        raise ValueError("threshold_pct must be in (0, 100]")
    initial = float(initial_soc_pct)
    terminal = float(terminal_soc_pct)
    unrestricted = unrestricted_result or solve_hindsight_rows(
        cfg,
        rows,
        initial_soc_pct=initial,
        terminal_soc_pct=terminal,
        parameters=OFF_PARAMETERS,
        grid_step_kwh=grid_step_kwh,
        include_actions=False,
    )
    if unrestricted.get("status") != "optimal":
        return {
            "status": "unrestricted_not_optimal",
            "economic_value_ore": None,
            "economic_value_sek": None,
        }
    if initial > threshold + 1e-9 or terminal > threshold + 1e-9:
        return {
            "status": "boundary_soc_above_threshold",
            "economic_value_ore": None,
            "economic_value_sek": None,
        }

    capped_cfg = copy.deepcopy(cfg)
    battery = capped_cfg.setdefault("policy", {}).setdefault("battery", {})
    original_hard_max = float(battery.get("hard_max_soc_pct", 100.0))
    battery["hard_max_soc_pct"] = min(original_hard_max, threshold)
    capped = solve_hindsight_rows(
        capped_cfg,
        rows,
        initial_soc_pct=initial,
        terminal_soc_pct=terminal,
        parameters=OFF_PARAMETERS,
        grid_step_kwh=grid_step_kwh,
        include_actions=False,
    )
    if capped.get("status") != "optimal":
        return {
            "status": "capped_not_optimal",
            "economic_value_ore": None,
            "economic_value_sek": None,
            "capped_status": capped.get("status"),
        }
    value = float(capped["canonical_objective_cost_ore"]) - float(unrestricted["canonical_objective_cost_ore"])
    return {
        "status": "ok",
        "threshold_pct": threshold,
        "economic_value_ore": round(value, 4),
        "economic_value_sek": round(value / 100.0, 4),
        "unrestricted_objective_ore": float(unrestricted["canonical_objective_cost_ore"]),
        "capped_objective_ore": float(capped["canonical_objective_cost_ore"]),
        "capped_max_soc_pct": float(capped["max_soc_pct"]),
    }


def _candidate_summary(
    day: date,
    off: dict[str, Any],
    value: dict[str, Any],
    data: dict[str, Any],
) -> dict[str, Any]:
    return {
        "local_date": day.isoformat(),
        "max_soc_pct": float(off["max_soc_pct"]),
        "hours_above_90_soc": float(off["hours_above_90_soc"]),
        "hours_above_95_soc": float(off["hours_above_95_soc"]),
        "hours_above_98_soc": float(off["hours_above_98_soc"]),
        "initial_soc_pct": float(off["initial_soc_pct"]),
        "terminal_soc_pct": float(off["terminal_soc_pct"]),
        "energy_cost_sek": float(off["energy_cost_sek"]),
        "battery_throughput_kwh": float(off["battery_throughput_kwh"]),
        "canonical_objective_cost_sek": float(off["canonical_objective_cost_sek"]),
        "economic_value_above_90_status": value.get("status"),
        "economic_value_above_90_ore": value.get("economic_value_ore"),
        "economic_value_above_90_sek": value.get("economic_value_sek"),
        "actual_coverage_fraction": float(data.get("actual_coverage_fraction") or 0.0),
    }


def scan_high_soc_days(
    cfg: dict[str, Any],
    *,
    days_back: int = DEFAULT_SCAN_DAYS,
    end_date: str | date | None = None,
    limit: int = DEFAULT_SCAN_LIMIT,
    grid_step_kwh: float = DEFAULT_SCAN_GRID_STEP_KWH,
) -> dict[str, Any]:
    """Scan recent complete days using an OFF-profile perfect-information oracle."""
    days = int(days_back)
    top_n = int(limit)
    if not (1 <= days <= 180):
        raise ValueError("days_back must be between 1 and 180")
    if not (1 <= top_n <= 25):
        raise ValueError("limit must be between 1 and 25")
    if not (0.05 <= float(grid_step_kwh) <= 1.0):
        raise ValueError("grid_step_kwh must be between 0.05 and 1.0")

    if end_date is None:
        last_day = datetime.now(LOCAL_TZ).date() - timedelta(days=1)
    else:
        last_day = date.fromisoformat(end_date) if isinstance(end_date, str) else end_date
    first_day = last_day - timedelta(days=days - 1)

    usable: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}
    for offset in range(days):
        day = first_day + timedelta(days=offset)
        inputs = _day_inputs(day)
        if inputs["status"] != "ok":
            reason = str(inputs["status"])
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        initial = float(inputs["initial_soc_pct"])
        terminal = float(inputs["terminal_soc_pct"])
        off = solve_hindsight_rows(
            cfg,
            inputs["rows"],
            initial_soc_pct=initial,
            terminal_soc_pct=terminal,
            parameters=OFF_PARAMETERS,
            grid_step_kwh=float(grid_step_kwh),
            include_actions=False,
        )
        if off.get("status") != "optimal":
            reason = f"oracle_{off.get('status') or 'unknown'}"
            skipped[reason] = skipped.get(reason, 0) + 1
            continue

        if float(off["max_soc_pct"]) > 90.0 + 1e-9:
            value = economic_value_above_soc_threshold_for_rows(
                cfg,
                inputs["rows"],
                initial_soc_pct=initial,
                terminal_soc_pct=terminal,
                threshold_pct=90.0,
                grid_step_kwh=float(grid_step_kwh),
                unrestricted_result=off,
            )
        else:
            value = {
                "status": "not_used",
                "economic_value_ore": 0.0,
                "economic_value_sek": 0.0,
            }
        usable.append(_candidate_summary(day, off, value, inputs["data"]))

    def rank_key(item: dict[str, Any]) -> tuple[float, float, float, float, float]:
        value = item.get("economic_value_above_90_ore")
        economic = float(value) if value is not None else -1.0
        return (
            float(item["max_soc_pct"]),
            float(item["hours_above_98_soc"]),
            float(item["hours_above_95_soc"]),
            float(item["hours_above_90_soc"]),
            economic,
        )

    ranked = sorted(usable, key=rank_key, reverse=True)
    candidates = [item for item in ranked if float(item["max_soc_pct"]) > 90.0 + 1e-9]
    return {
        "diagnostic_only": True,
        "planner_integration_enabled": False,
        "selector_integration_enabled": False,
        "physical_write_performed": False,
        "scan_version": SCAN_VERSION,
        "hindsight_version": HINDSIGHT_VERSION,
        "profile": "off",
        "profile_semantics": "cycle wear retained; high-SOC occupancy cost disabled",
        "period": {
            "first_local_date": first_day.isoformat(),
            "last_local_date": last_day.isoformat(),
            "days_requested": days,
        },
        "grid_step_kwh": float(grid_step_kwh),
        "rank_basis": [
            "max_soc_pct desc",
            "hours_above_98_soc desc",
            "hours_above_95_soc desc",
            "hours_above_90_soc desc",
            "economic_value_above_90_ore desc when comparable",
        ],
        "days_usable": len(usable),
        "days_with_off_optimum_above_90_soc": len(candidates),
        "skipped": skipped,
        "top_candidates": candidates[:top_n],
        "highest_soc_days": ranked[:top_n],
    }
