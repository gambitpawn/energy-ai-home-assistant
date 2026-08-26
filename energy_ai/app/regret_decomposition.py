from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

from .app_comparison import _active_tariffs, _actual_rows, resolve_window
from .historical_closed_loop import (
    _canonical,
    _information_vintages,
    _slice_horizon,
    _vintage_for_interval,
)
from .historical_closed_loop_v2 import compare_closed_loop
from .optimizer import DT_HOURS, PLANNER_NAME
from .optimizer_evaluation import _apply_action, _hindsight
from .optimizer_v35_replay import solve_v35_from_rows

ENGINE_NAME = "optimizer_regret_decomposition_v1"


def _actual_map(rows: list[dict[str, Any]]) -> dict[datetime, dict[str, Any]]:
    return {_canonical(str(r["start"])): r for r in rows}


def _inject_perfect_information(
    horizon: list[dict[str, Any]],
    actual_by_start: dict[datetime, dict[str, Any]],
    *,
    perfect_prices: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Replace load/PV with realized values for the complete stored v3.5 horizon.

    Perfect forecast means both point forecast error and load/PV uncertainty are
    removed. In the perfect-price variant, realized spot prices are also exposed
    to v3.5 and all horizon price intervals are marked known.
    """
    out: list[dict[str, Any]] = []
    missing: list[str] = []
    for raw in horizon:
        item = dict(raw)
        stamp = _canonical(str(item["start"]))
        actual = actual_by_start.get(stamp)
        if actual is None:
            missing.append(stamp.isoformat())
            continue
        item["start"] = stamp.isoformat()
        item["load_kw"] = float(actual["load_kw"])
        item["pv_kw"] = float(actual["pv_kw"])
        item["load_uncertainty_kw"] = 0.0
        item["pv_uncertainty_kw"] = 0.0
        if perfect_prices:
            item["price_known"] = True
            item["price_ore_kwh"] = float(actual["price_ore_kwh"])
        out.append(item)
    return out, missing


def _required_actual_end(
    eval_rows: list[dict[str, Any]],
    vintages: list[dict[str, Any]],
) -> tuple[datetime | None, int, int]:
    latest: datetime | None = None
    matched = 0
    horizon_intervals = 0
    for row in eval_rows:
        stamp = _canonical(str(row["start"]))
        vintage = _vintage_for_interval(stamp, vintages)
        if vintage is None:
            continue
        horizon = _slice_horizon(vintage["payload"], stamp)
        if not horizon:
            continue
        matched += 1
        horizon_intervals += len(horizon)
        end = _canonical(str(horizon[-1]["start"])) + timedelta(minutes=15)
        latest = end if latest is None or end > latest else latest
    return latest, matched, horizon_intervals


def _run_perfect_closed_loop(
    cfg: dict[str, Any],
    eval_rows: list[dict[str, Any]],
    vintages: list[dict[str, Any]],
    actual_by_start: dict[datetime, dict[str, Any]],
    initial_soc_pct: float,
    *,
    perfect_prices: bool,
    include_rows: bool,
) -> dict[str, Any]:
    battery = (cfg.get("policy") or {}).get("battery") or {}
    cap = float(battery.get("capacity_kwh", 19.6))
    hmin = float(battery.get("hard_min_soc_pct", 5.0))
    hmax = float(battery.get("hard_max_soc_pct", 100.0))
    soc0 = max(hmin, min(hmax, float(initial_soc_pct)))
    energy = cap * soc0 / 100.0

    cash = throughput = grid_import = grid_export = 0.0
    charge = discharge = 0.0
    peak_import = 0.0
    solve_failures = 0
    clamps = 0
    intervals = 0
    missing_horizon_intervals = 0
    replay_rows: list[dict[str, Any]] = []

    for row in eval_rows:
        stamp = _canonical(str(row["start"]))
        vintage = _vintage_for_interval(stamp, vintages)
        if vintage is None:
            solve_failures += 1
            break
        horizon = _slice_horizon(vintage["payload"], stamp)
        injected, missing = _inject_perfect_information(
            horizon,
            actual_by_start,
            perfect_prices=perfect_prices,
        )
        if missing or len(injected) != len(horizon):
            missing_horizon_intervals += len(missing)
            solve_failures += 1
            break
        try:
            current_soc = energy / cap * 100.0
            solved = solve_v35_from_rows(cfg, injected, current_soc)
            requested = float(solved["first_action_kw"])
            reserve_soc = float(solved["rows"][0]["reserve_soc_pct"])
        except Exception:
            solve_failures += 1
            break

        applied = _apply_action(row, requested, energy, cfg, reserve_soc)
        energy = float(applied["energy_end_kwh"])
        cash += float(applied["cash_cost_ore"])
        throughput += float(applied["throughput_kwh"])
        grid_import += float(applied["grid_import_kw"]) * DT_HOURS
        grid_export += float(applied["grid_export_kw"]) * DT_HOURS
        peak_import = max(peak_import, float(applied["grid_import_kw"]))
        action = float(applied["applied_action_kw"])
        if action < 0:
            charge += -action * DT_HOURS
        else:
            discharge += action * DT_HOURS
        clamps += int(bool(applied["clamped"]))
        intervals += 1

        if include_rows:
            replay_rows.append({
                "start": row["start"],
                "information_generated_at": vintage["generated_at"].isoformat(),
                "counterfactual_soc_start_pct": round(current_soc, 2),
                "requested_action_kw": round(requested, 4),
                "applied_action_kw": round(action, 4),
                "soc_end_pct": round(float(applied["soc_end_pct"]), 2),
                "grid_import_kw": round(float(applied["grid_import_kw"]), 4),
                "grid_export_kw": round(float(applied["grid_export_kw"]), 4),
                "perfect_prices": perfect_prices,
                "clamped": bool(applied["clamped"]),
            })

    return {
        "status": "ok" if intervals == len(eval_rows) and solve_failures == 0 else "incomplete",
        "planner": PLANNER_NAME,
        "information_mode": (
            "perfect_load_pv_and_prices_within_stored_v35_horizon"
            if perfect_prices
            else "perfect_load_pv_same_historical_price_information"
        ),
        "initial_soc_pct": round(soc0, 2),
        "terminal_soc_pct": round(energy / cap * 100.0, 2),
        "cash_cost_ore": round(cash, 2),
        "battery_throughput_kwh": round(throughput, 3),
        "battery_charge_kwh": round(charge, 3),
        "battery_discharge_kwh": round(discharge, 3),
        "grid_import_kwh": round(grid_import, 3),
        "grid_export_kwh": round(grid_export, 3),
        "peak_grid_import_kw_15m": round(peak_import, 3),
        "realized_constraint_clamp_intervals": clamps,
        "closed_loop_intervals": intervals,
        "solve_failures": solve_failures,
        "missing_horizon_intervals": missing_horizon_intervals,
        "rows": replay_rows if include_rows else None,
    }


def regret_decomposition(
    cfg: dict[str, Any],
    *,
    start: str | None = None,
    end: str | None = None,
    hours: int | None = None,
    days: int | None = None,
    include_rows: bool = False,
) -> dict[str, Any]:
    a, b = resolve_window(start=start, end=end, hours=hours, days=days)
    if _active_tariffs(cfg):
        return {
            "engine": ENGINE_NAME,
            "status": "unsupported_active_tariffs",
            "valid_decomposition": False,
            "window": {"start": a.isoformat(), "end": b.isoformat()},
        }

    realtime = compare_closed_loop(
        cfg,
        start=a.isoformat(),
        end=b.isoformat(),
        min_information_coverage=0.90,
        min_actual_coverage=0.90,
        include_rows=False,
    )
    base: dict[str, Any] = {
        "engine": ENGINE_NAME,
        "planner": PLANNER_NAME,
        "window": realtime.get("window") or {"start": a.isoformat(), "end": b.isoformat()},
        "valid_decomposition": False,
        "realtime_head_to_head": realtime,
    }
    if not realtime.get("valid_comparison"):
        return {**base, "status": "realtime_head_to_head_not_valid"}

    eval_rows, eval_data = _actual_rows(a, b)
    if not eval_rows:
        return {**base, "status": "no_actual_data"}
    first_soc = next((r.get("battery_soc_start_pct") for r in eval_rows if r.get("battery_soc_start_pct") is not None), None)
    if first_soc is None:
        return {**base, "status": "missing_initial_soc"}

    vintages = _information_vintages(a, b)
    required_end, matched_vintages, horizon_intervals = _required_actual_end(eval_rows, vintages)
    if required_end is None:
        return {**base, "status": "missing_information_vintages"}

    future_rows, future_data = _actual_rows(a, required_end)
    future_map = _actual_map(future_rows)

    missing_required: set[str] = set()
    required_unique: set[str] = set()
    for row in eval_rows:
        stamp = _canonical(str(row["start"]))
        vintage = _vintage_for_interval(stamp, vintages)
        if vintage is None:
            continue
        horizon = _slice_horizon(vintage["payload"], stamp)
        for h in horizon:
            hs = _canonical(str(h["start"]))
            key = hs.isoformat()
            required_unique.add(key)
            if hs not in future_map:
                missing_required.add(key)

    maturity = {
        "evaluation_intervals": len(eval_rows),
        "matched_information_vintages": matched_vintages,
        "summed_horizon_intervals": horizon_intervals,
        "unique_required_actual_intervals": len(required_unique),
        "available_required_actual_intervals": len(required_unique) - len(missing_required),
        "required_actual_until": required_end.isoformat(),
        "latest_available_actual_start": future_data.get("last"),
        "missing_required_actual_intervals": len(missing_required),
        "perfect_information_horizon_coverage_fraction": round(
            (len(required_unique) - len(missing_required)) / max(1, len(required_unique)), 4
        ),
    }
    if missing_required:
        return {
            **base,
            "status": "insufficient_future_actual_coverage",
            "maturity": maturity,
            "note": "A valid perfect-forecast decomposition requires realized load/PV for every interval in every historical v3.5 planning horizon used by the evaluation window.",
        }

    perfect_forecast = _run_perfect_closed_loop(
        cfg,
        eval_rows,
        vintages,
        future_map,
        float(first_soc),
        perfect_prices=False,
        include_rows=include_rows,
    )
    perfect_information = _run_perfect_closed_loop(
        cfg,
        eval_rows,
        vintages,
        future_map,
        float(first_soc),
        perfect_prices=True,
        include_rows=include_rows,
    )
    if perfect_forecast.get("status") != "ok" or perfect_information.get("status") != "ok":
        return {
            **base,
            "status": "perfect_information_closed_loop_failed",
            "maturity": maturity,
            "perfect_forecast_v35": perfect_forecast,
            "perfect_information_v35": perfect_information,
        }

    battery = (cfg.get("policy") or {}).get("battery") or {}
    econ = (cfg.get("policy") or {}).get("economics") or {}
    cap = float(battery.get("capacity_kwh", 19.6))
    hmin = float(battery.get("hard_min_soc_pct", 5.0))
    hmax = float(battery.get("hard_max_soc_pct", 100.0))
    initial_soc = max(hmin, min(hmax, float(first_soc)))
    initial_energy = cap * initial_soc / 100.0
    reference_price = median([
        float(r["price_ore_kwh"]) + float(econ.get("import_overhead_ore_kwh", 0.0))
        for r in eval_rows
    ])

    def add_economic(path: dict[str, Any]) -> float:
        terminal_energy = cap * float(path["terminal_soc_pct"]) / 100.0
        asset = (terminal_energy - initial_energy) * reference_price
        economic = float(path["cash_cost_ore"]) - asset
        path["terminal_asset_adjustment_ore"] = round(asset, 2)
        path["economic_cost_ore"] = round(economic, 2)
        path["economic_cost_sek"] = round(economic / 100.0, 2)
        return economic

    realtime_econ = float((realtime.get("shadow_planner_closed_loop") or {})["economic_cost_ore"])
    perfect_forecast_econ = add_economic(perfect_forecast)
    perfect_information_econ = add_economic(perfect_information)

    hindsight = _hindsight(
        eval_rows,
        cfg,
        initial_soc,
        float(perfect_information["terminal_soc_pct"]),
    )
    if hindsight.get("status") not in {"optimal", "feasible"}:
        return {
            **base,
            "status": "hindsight_failed",
            "maturity": maturity,
            "perfect_forecast_v35": perfect_forecast,
            "perfect_information_v35": perfect_information,
            "perfect_hindsight": hindsight,
        }

    perfect_info_terminal_energy = cap * float(perfect_information["terminal_soc_pct"]) / 100.0
    perfect_info_terminal_asset = (perfect_info_terminal_energy - initial_energy) * reference_price
    hindsight_econ = float(hindsight["cash_cost_ore"]) - perfect_info_terminal_asset
    hindsight["terminal_asset_adjustment_ore"] = round(perfect_info_terminal_asset, 2)
    hindsight["economic_cost_ore"] = round(hindsight_econ, 2)
    hindsight["economic_cost_sek"] = round(hindsight_econ / 100.0, 2)

    forecast_regret = realtime_econ - perfect_forecast_econ
    price_information_regret = perfect_forecast_econ - perfect_information_econ
    planner_horizon_policy_residual = perfect_information_econ - hindsight_econ
    total_gap = realtime_econ - hindsight_econ
    reconciliation = total_gap - (
        forecast_regret + price_information_regret + planner_horizon_policy_residual
    )

    actual_econ = float((realtime.get("actual_app") or {})["economic_cost_ore"])
    app_to_realtime_gain = actual_econ - realtime_econ

    return {
        **base,
        "status": "valid",
        "valid_decomposition": True,
        "maturity": maturity,
        "actual_app": realtime.get("actual_app"),
        "realtime_v35": realtime.get("shadow_planner_closed_loop"),
        "perfect_forecast_v35": perfect_forecast,
        "perfect_information_v35": perfect_information,
        "perfect_hindsight": hindsight,
        "decomposition": {
            "app_to_realtime_v35_gain_ore": round(app_to_realtime_gain, 2),
            "app_to_realtime_v35_gain_sek": round(app_to_realtime_gain / 100.0, 2),
            "forecast_regret_ore": round(forecast_regret, 2),
            "forecast_regret_sek": round(forecast_regret / 100.0, 2),
            "price_information_regret_ore": round(price_information_regret, 2),
            "price_information_regret_sek": round(price_information_regret / 100.0, 2),
            "planner_horizon_policy_residual_ore": round(planner_horizon_policy_residual, 2),
            "planner_horizon_policy_residual_sek": round(planner_horizon_policy_residual / 100.0, 2),
            "realtime_to_hindsight_total_gap_ore": round(total_gap, 2),
            "realtime_to_hindsight_total_gap_sek": round(total_gap / 100.0, 2),
            "reconciliation_error_ore": round(reconciliation, 6),
        },
        "definitions": {
            "forecast_regret": "realtime v3.5 economic cost minus v3.5 with realized load/PV and zero load/PV uncertainty, while preserving the historical price-known/unknown pattern; positive means forecast information hurt performance",
            "price_information_regret": "perfect-load/PV v3.5 economic cost minus v3.5 also given realized prices across the same stored planning horizons; positive means unavailable future prices account for part of the gap",
            "planner_horizon_policy_residual": "perfect-load/PV-and-price v3.5 economic cost minus full-period hindsight economic cost at the same terminal SOC; this residual includes finite-horizon, state-grid, objective/policy-shaping and receding-horizon effects and is not claimed as pure planner regret",
        },
        "valuation": {
            "reference_price_ore_kwh": round(reference_price, 3),
            "terminal_energy_method": "all paths are terminal-energy adjusted against their own terminal SOC using the same reference price; hindsight is additionally fixed to perfect-information-v3.5 terminal SOC",
        },
        "limitations": [
            "perfect forecast requires matured realized load/PV for every interval of every stored v3.5 horizon used in the evaluation window",
            "perfect forecast sets load/PV uncertainty to zero, so forecast_regret combines point-forecast error and uncertainty/reserve effects",
            "planner_horizon_policy_residual is not pure planner regret; it also reflects finite-horizon and policy/objective structure",
            "monthly demand/effect tariffs are excluded and active tariffs prevent decomposition",
        ],
    }
