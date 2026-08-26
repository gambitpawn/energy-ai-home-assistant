from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from statistics import mean, median
from typing import Any

from .app_comparison import _active_tariffs, _actual_interval, _actual_rows, resolve_window
from .db import DB_PATH
from .optimizer import DT_HOURS, PLANNER_NAME
from .optimizer_evaluation import DECISION_GRACE_SECONDS, MAX_PLAN_AGE_MINUTES, _apply_action, _dt
from .optimizer_v35_replay import solve_v35_from_rows

ENGINE_NAME = "historical_closed_loop_v35_v1"
WINNER_EPSILON_ORE = 1.0


def _canonical(value: str) -> datetime:
    return _dt(value).replace(second=0, microsecond=0)


def _information_vintages(start: datetime, end: datetime) -> list[dict[str, Any]]:
    query_start = start - timedelta(minutes=MAX_PLAN_AGE_MINUTES)
    query_end = end + timedelta(seconds=DECISION_GRACE_SECONDS)
    try:
        with sqlite3.connect(DB_PATH) as c:
            rows = c.execute(
                "SELECT generated_at,planner,payload_json FROM optimizer_plan_summary "
                "WHERE generated_at>=? AND generated_at<? ORDER BY generated_at",
                (query_start.isoformat(), query_end.isoformat()),
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for generated_raw, planner, raw in rows:
        try:
            generated = _dt(generated_raw)
            payload = json.loads(raw or "{}")
            horizon = payload.get("rows") or []
            if not horizon:
                continue
        except Exception:
            continue
        out.append({
            "generated_at": generated,
            "source_planner": str(planner or payload.get("planner") or "unknown"),
            "payload": payload,
        })
    return out


def _vintage_for_interval(
    interval_start: datetime,
    vintages: list[dict[str, Any]],
) -> dict[str, Any] | None:
    candidates = []
    for item in vintages:
        lag = (item["generated_at"] - interval_start).total_seconds()
        if lag > DECISION_GRACE_SECONDS or lag < -MAX_PLAN_AGE_MINUTES * 60:
            continue
        rows = item["payload"].get("rows") or []
        if any(_canonical(str(r.get("start"))) == interval_start for r in rows if r.get("start")):
            candidates.append(item)
    if not candidates:
        return None
    return max(candidates, key=lambda x: x["generated_at"])


def _slice_horizon(payload: dict[str, Any], interval_start: datetime) -> list[dict[str, Any]]:
    rows = payload.get("rows") or []
    sliced = []
    started = False
    for raw in rows:
        if not isinstance(raw, dict) or not raw.get("start"):
            continue
        stamp = _canonical(str(raw["start"]))
        if stamp == interval_start:
            started = True
        if not started:
            continue
        if stamp < interval_start:
            continue
        item = dict(raw)
        item["start"] = stamp.isoformat()
        item["load_kw"] = float(item.get("load_kw") or 0.0)
        item["pv_kw"] = float(item.get("pv_kw") or 0.0)
        item["load_uncertainty_kw"] = float(item.get("load_uncertainty_kw") or 0.0)
        item["pv_uncertainty_kw"] = float(item.get("pv_uncertainty_kw") or 0.0)
        item["price_known"] = bool(item.get("price_known", item.get("price_ore_kwh") is not None))
        item["price_ore_kwh"] = None if item.get("price_ore_kwh") is None else float(item["price_ore_kwh"])
        sliced.append(item)
    return sliced


def replay_regression(cfg: dict[str, Any], samples: int = 12) -> dict[str, Any]:
    samples = max(1, min(int(samples), 100))
    try:
        with sqlite3.connect(DB_PATH) as c:
            rows = c.execute(
                "SELECT generated_at,planner,payload_json FROM optimizer_plan_summary "
                "WHERE planner=? ORDER BY generated_at DESC LIMIT ?",
                (PLANNER_NAME, samples),
            ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    checks = []
    for generated_at, planner, raw in rows:
        try:
            payload = json.loads(raw or "{}")
            horizon = payload.get("rows") or []
            initial_soc = float(payload["initial_soc_pct"])
            expected = float(horizon[0]["battery_action_kw"])
            solved = solve_v35_from_rows(cfg, [dict(r) for r in horizon], initial_soc)
            actual = float(solved["first_action_kw"])
            error = actual - expected
            checks.append({
                "generated_at": generated_at,
                "planner": planner,
                "initial_soc_pct": initial_soc,
                "stored_first_action_kw": round(expected, 6),
                "replayed_first_action_kw": round(actual, 6),
                "difference_kw": round(error, 6),
                "pass": abs(error) <= 0.00011,
            })
        except Exception as exc:
            checks.append({"generated_at": generated_at, "planner": planner, "pass": False, "error": repr(exc)})
    passed = sum(1 for r in checks if r.get("pass"))
    return {
        "engine": "pure_v35_replay_regression_v1",
        "reference_planner": PLANNER_NAME,
        "samples_requested": samples,
        "samples_checked": len(checks),
        "samples_passed": passed,
        "pass": bool(checks) and passed == len(checks),
        "tolerance_kw": 0.00011,
        "checks": checks,
    }


def compare_closed_loop(
    cfg: dict[str, Any],
    *,
    start: str | None = None,
    end: str | None = None,
    hours: int | None = None,
    days: int | None = None,
    min_information_coverage: float = 0.90,
    min_actual_coverage: float = 0.90,
    include_rows: bool = True,
) -> dict[str, Any]:
    a, b = resolve_window(start=start, end=end, hours=hours, days=days)
    if a.minute % 15 or a.second or a.microsecond or b.minute % 15 or b.second or b.microsecond:
        raise ValueError("comparison start and end must align to 15-minute boundaries")
    completed = datetime.now(timezone.utc).replace(
        minute=(datetime.now(timezone.utc).minute // 15) * 15,
        second=0,
        microsecond=0,
    )
    if b > completed:
        raise ValueError("comparison end must not extend beyond the latest completed 15-minute interval")
    active_tariffs = _active_tariffs(cfg)
    rows, data = _actual_rows(a, b)
    expected = int(data.get("expected_intervals") or 0)
    expected_first = a.isoformat()
    expected_last = (b - timedelta(minutes=15)).isoformat()
    boundary_complete = bool(rows) and data.get("first") == expected_first and data.get("last") == expected_last
    data["start_boundary_complete"] = bool(rows) and data.get("first") == expected_first
    data["end_boundary_complete"] = bool(rows) and data.get("last") == expected_last
    data["boundary_complete"] = boundary_complete
    data["minimum_actual_coverage_fraction"] = float(min_actual_coverage)
    data["minimum_information_coverage_fraction"] = float(min_information_coverage)

    base = {
        "engine": ENGINE_NAME,
        "planner": PLANNER_NAME,
        "window": {"start": a.isoformat(), "end": b.isoformat(), "hours": round((b-a).total_seconds()/3600.0, 2)},
        "data": data,
        "valid_comparison": False,
        "winner": None,
    }
    if active_tariffs:
        return {**base, "status": "unsupported_active_tariffs", "active_tariffs": active_tariffs}
    if not rows:
        return {**base, "status": "no_actual_data"}
    if float(data.get("actual_coverage_fraction") or 0.0) < float(min_actual_coverage) or not boundary_complete:
        return {**base, "status": "insufficient_actual_coverage"}

    first_soc = next((r.get("battery_soc_start_pct") for r in rows if r.get("battery_soc_start_pct") is not None), None)
    actual_terminal_soc = next((r.get("battery_soc_end_pct") for r in reversed(rows) if r.get("battery_soc_end_pct") is not None), None)
    if first_soc is None or actual_terminal_soc is None:
        return {**base, "status": "missing_soc"}

    vintages = _information_vintages(a, b)
    vintage_map: dict[datetime, dict[str, Any]] = {}
    source_counts: dict[str, int] = {}
    for row in rows:
        stamp = _canonical(row["start"])
        item = _vintage_for_interval(stamp, vintages)
        if item is not None:
            vintage_map[stamp] = item
            source = str(item["source_planner"])
            source_counts[source] = source_counts.get(source, 0) + 1
    information_coverage = len(vintage_map) / max(1, expected)
    data["matched_information_vintages"] = len(vintage_map)
    data["information_vintage_coverage_fraction"] = round(information_coverage, 4)

    battery_cfg = (cfg.get("policy") or {}).get("battery") or {}
    econ = (cfg.get("policy") or {}).get("economics") or {}
    cap = float(battery_cfg.get("capacity_kwh", 19.6))
    hmin = float(battery_cfg.get("hard_min_soc_pct", 5.0))
    hmax = float(battery_cfg.get("hard_max_soc_pct", 100.0))
    initial_soc = max(hmin, min(hmax, float(first_soc)))
    initial_energy = cap * initial_soc / 100.0
    shadow_energy = initial_energy

    actual_cash = actual_throughput = actual_import = actual_export = 0.0
    shadow_cash = shadow_throughput = shadow_import = shadow_export = 0.0
    actual_charge = actual_discharge = shadow_charge = shadow_discharge = 0.0
    actual_peak = shadow_peak = 0.0
    realized_clamps = 0
    solve_failures = 0
    solve_count = 0
    generation_lags = []
    action_abs_errors = []
    direction_matches = direction_n = 0
    replay_rows = []
    contiguous = True

    def direction(v: float) -> int:
        return 1 if v > 0.25 else -1 if v < -0.25 else 0

    for row in rows:
        stamp = _canonical(row["start"])
        actual = _actual_interval(row, cfg)
        actual_cash += float(actual["cash_cost_ore"])
        actual_throughput += float(actual["throughput_kwh"])
        actual_import += float(actual["grid_import_kw"]) * DT_HOURS
        actual_export += float(actual["grid_export_kw"]) * DT_HOURS
        actual_charge += float(actual["charge_kwh"])
        actual_discharge += float(actual["discharge_kwh"])
        actual_peak = max(actual_peak, float(actual["grid_import_kw"]))

        vintage = vintage_map.get(stamp)
        if vintage is None or not contiguous:
            contiguous = False
            if include_rows:
                replay_rows.append({
                    "start": row["start"],
                    "information_vintage_available": False,
                    "actual_battery_action_kw": round(float(row["battery_power_kw"]), 4),
                })
            continue

        horizon = _slice_horizon(vintage["payload"], stamp)
        if not horizon:
            solve_failures += 1
            contiguous = False
            continue
        try:
            current_soc = shadow_energy / cap * 100.0
            solved = solve_v35_from_rows(cfg, horizon, current_soc)
            requested = float(solved["first_action_kw"])
            solve_count += 1
        except Exception:
            solve_failures += 1
            contiguous = False
            continue

        applied = _apply_action(row, requested, shadow_energy, cfg, None)
        shadow_energy = float(applied["energy_end_kwh"])
        shadow_cash += float(applied["cash_cost_ore"])
        shadow_throughput += float(applied["throughput_kwh"])
        shadow_import += float(applied["grid_import_kw"]) * DT_HOURS
        shadow_export += float(applied["grid_export_kw"]) * DT_HOURS
        if float(applied["applied_action_kw"]) < 0:
            shadow_charge += -float(applied["applied_action_kw"]) * DT_HOURS
        else:
            shadow_discharge += float(applied["applied_action_kw"]) * DT_HOURS
        shadow_peak = max(shadow_peak, float(applied["grid_import_kw"]))
        realized_clamps += int(bool(applied["clamped"]))
        lag = (vintage["generated_at"] - stamp).total_seconds()
        generation_lags.append(lag)
        actual_action = float(row["battery_power_kw"])
        shadow_action = float(applied["applied_action_kw"])
        action_abs_errors.append(abs(actual_action - shadow_action))
        direction_matches += int(direction(actual_action) == direction(shadow_action))
        direction_n += 1

        if include_rows:
            replay_rows.append({
                "start": row["start"],
                "information_vintage_available": True,
                "information_generated_at": vintage["generated_at"].isoformat(),
                "information_source_planner": vintage["source_planner"],
                "counterfactual_soc_start_pct": round(current_soc, 2),
                "planner_requested_action_kw": round(requested, 4),
                "planner_applied_action_kw": round(shadow_action, 4),
                "planner_virtual_soc_end_pct": round(float(applied["soc_end_pct"]), 2),
                "actual_battery_action_kw": round(actual_action, 4),
                "actual_soc_end_pct": None if row.get("battery_soc_end_pct") is None else round(float(row["battery_soc_end_pct"]), 2),
                "actual_grid_import_kw": round(float(actual["grid_import_kw"]), 4),
                "actual_grid_export_kw": round(float(actual["grid_export_kw"]), 4),
                "planner_grid_import_kw": round(float(applied["grid_import_kw"]), 4),
                "planner_grid_export_kw": round(float(applied["grid_export_kw"]), 4),
                "realized_constraint_clamp": bool(applied["clamped"]),
            })

    replay_coverage = solve_count / max(1, expected)
    data["closed_loop_intervals"] = solve_count
    data["closed_loop_coverage_fraction"] = round(replay_coverage, 4)
    data["solve_failures"] = solve_failures
    full_valid = (
        information_coverage >= float(min_information_coverage)
        and replay_coverage >= float(min_information_coverage)
        and solve_count == len(rows)
        and solve_failures == 0
    )

    shadow_terminal_soc = shadow_energy / cap * 100.0
    reference_price = median([
        float(r["price_ore_kwh"]) + float(econ.get("import_overhead_ore_kwh", 0.0)) for r in rows
    ])
    actual_terminal_energy = cap * float(actual_terminal_soc) / 100.0
    actual_terminal_asset = (actual_terminal_energy - initial_energy) * reference_price
    shadow_terminal_asset = (shadow_energy - initial_energy) * reference_price
    actual_economic = actual_cash - actual_terminal_asset
    shadow_economic = shadow_cash - shadow_terminal_asset
    advantage = actual_economic - shadow_economic

    if full_valid:
        winner = "shadow_planner" if advantage > WINNER_EPSILON_ORE else "actual_app" if advantage < -WINNER_EPSILON_ORE else "tie"
        status = "valid"
    else:
        winner = None
        status = "insufficient_information_vintage_coverage" if information_coverage < float(min_information_coverage) else "incomplete_closed_loop"

    return {
        **base,
        "status": status,
        "valid_comparison": full_valid,
        "winner": winner,
        "data": data,
        "information_vintages": {
            "source": "stored optimizer_plan_summary payload_json",
            "source_planner_counts": source_counts,
            "mean_generation_lag_seconds": round(mean(generation_lags), 2) if generation_lags else None,
            "note": "source planner identifies which stored plan carried the forecast/price snapshot; every counterfactual decision is re-solved with deterministic_battery_dp_v3_5",
        },
        "actual_app": {
            "initial_soc_pct": round(initial_soc, 2),
            "terminal_soc_pct": round(float(actual_terminal_soc), 2),
            "cash_cost_ore": round(actual_cash, 2),
            "cash_cost_sek": round(actual_cash / 100.0, 2),
            "terminal_asset_adjustment_ore": round(actual_terminal_asset, 2),
            "economic_cost_ore": round(actual_economic, 2),
            "economic_cost_sek": round(actual_economic / 100.0, 2),
            "battery_throughput_kwh": round(actual_throughput, 3),
            "battery_charge_kwh": round(actual_charge, 3),
            "battery_discharge_kwh": round(actual_discharge, 3),
            "grid_import_kwh": round(actual_import, 3),
            "grid_export_kwh": round(actual_export, 3),
            "peak_grid_import_kw_15m": round(actual_peak, 3),
        },
        "shadow_planner_closed_loop": {
            "planner": PLANNER_NAME,
            "initial_soc_pct": round(initial_soc, 2),
            "terminal_soc_pct": round(shadow_terminal_soc, 2),
            "cash_cost_ore": round(shadow_cash, 2),
            "cash_cost_sek": round(shadow_cash / 100.0, 2),
            "terminal_asset_adjustment_ore": round(shadow_terminal_asset, 2),
            "economic_cost_ore": round(shadow_economic, 2),
            "economic_cost_sek": round(shadow_economic / 100.0, 2),
            "battery_throughput_kwh": round(shadow_throughput, 3),
            "battery_charge_kwh": round(shadow_charge, 3),
            "battery_discharge_kwh": round(shadow_discharge, 3),
            "grid_import_kwh": round(shadow_import, 3),
            "grid_export_kwh": round(shadow_export, 3),
            "peak_grid_import_kw_15m": round(shadow_peak, 3),
            "realized_constraint_clamp_intervals": realized_clamps,
        },
        "comparison": {
            "planner_advantage_ore": round(advantage, 2),
            "planner_advantage_sek": round(advantage / 100.0, 2),
            "cash_cost_difference_ore": round(actual_cash - shadow_cash, 2),
            "terminal_soc_difference_pct": round(shadow_terminal_soc - float(actual_terminal_soc), 2),
            "battery_throughput_difference_kwh": round(shadow_throughput - actual_throughput, 3),
            "grid_import_difference_kwh": round(shadow_import - actual_import, 3),
            "grid_export_difference_kwh": round(shadow_export - actual_export, 3),
            "peak_grid_import_difference_kw_15m": round(shadow_peak - actual_peak, 3),
            "action_mae_kw": round(mean(action_abs_errors), 4) if action_abs_errors else None,
            "action_direction_agreement_fraction": round(direction_matches / direction_n, 4) if direction_n else None,
            "winner_epsilon_ore": WINNER_EPSILON_ORE,
        },
        "valuation": {
            "reference_price_ore_kwh": round(reference_price, 3),
            "terminal_energy_method": "each path receives the same reference-price credit/debit for terminal battery energy relative to the common initial SOC",
            "economic_cost_definition": "spot import cost minus spot export revenue plus battery degradation minus terminal battery asset adjustment",
            "planner_advantage_definition": "actual_app economic cost minus closed-loop shadow planner economic cost; positive means the planner would have been cheaper",
        },
        "limitations": [
            "counterfactual assumes realized house load and PV are unchanged by battery control",
            "each 15-minute decision uses the freshest stored information vintage available within the same timing window as the live evaluator",
            "the first planner action is applied to the realized 15-minute average load/PV; sub-interval dynamics are not reconstructed",
            "realized_constraint_clamp means forecast error made the planned action infeasible against realized load/PV or physical limits; it is a real counterfactual effect, not a replay-SOC artifact",
            "monthly demand/effect tariffs are excluded in v1 and active tariffs prevent a winner declaration",
        ],
        "rows": replay_rows if include_rows else None,
    }
