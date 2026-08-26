from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from .db import DB_PATH
from .optimizer import DT_HOURS
from .optimizer_evaluation import DECISION_GRACE_SECONDS, MAX_PLAN_AGE_MINUTES, _apply_action, _dt, _num

LOCAL_TZ = ZoneInfo("Europe/Stockholm")
ENGINE_NAME = "app_vs_shadow_planner_v1"
DIRECTION_THRESHOLD_KW = 0.25


def _parse_user_dt(value: str) -> datetime:
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=LOCAL_TZ)
    return d.astimezone(timezone.utc)


def _floor_quarter(d: datetime) -> datetime:
    d = d.astimezone(timezone.utc)
    return d.replace(minute=(d.minute // 15) * 15, second=0, microsecond=0)


def resolve_window(
    start: str | None = None,
    end: str | None = None,
    hours: int | None = None,
    days: int | None = None,
) -> tuple[datetime, datetime]:
    if bool(start) != bool(end):
        raise ValueError("start and end must be supplied together")
    if start and end:
        if hours is not None or days is not None:
            raise ValueError("use either start/end or hours/days, not both")
        a = _parse_user_dt(start)
        b = _parse_user_dt(end)
    else:
        if hours is not None and days is not None:
            raise ValueError("use either hours or days, not both")
        duration = timedelta(days=int(days)) if days is not None else timedelta(hours=int(hours or 24))
        b = _floor_quarter(datetime.now(timezone.utc))
        a = b - duration
    if b <= a:
        raise ValueError("end must be after start")
    if b - a > timedelta(days=31):
        raise ValueError("comparison window may not exceed 31 days")
    return a, b


def _expected_intervals(start: datetime, end: datetime) -> int:
    return max(1, int(round((end - start).total_seconds() / 900.0)))


def _actual_rows(start: datetime, end: datetime) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as c:
        states = c.execute(
            "SELECT bucket_start,payload_json FROM state_15m WHERE bucket_start>=? AND bucket_start<? ORDER BY bucket_start",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        prices = c.execute(
            "SELECT start_utc,price_ore_kwh FROM price_15m WHERE start_utc>=? AND start_utc<? ORDER BY start_utc",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    price_map = {_dt(s).replace(second=0, microsecond=0): float(p) for s, p in prices}
    rows: list[dict[str, Any]] = []
    rejected = 0
    for stamp_raw, payload_raw in states:
        try:
            stamp = _dt(stamp_raw).replace(second=0, microsecond=0)
            payload = json.loads(payload_raw)
        except Exception:
            rejected += 1
            continue
        means = payload.get("mean") or {}
        load = _num(means.get("house_load_kw"))
        pv = _num(means.get("pv_power_kw"))
        grid = _num(means.get("grid_power_kw"))
        battery = _num(means.get("battery_power_kw"))
        price = _num(means.get("spot_price_ore_kwh"))
        if price is None:
            price = price_map.get(stamp)
        if None in (load, pv, grid, battery, price):
            rejected += 1
            continue
        rows.append({
            "start": stamp.isoformat(),
            "load_kw": max(0.0, float(load)),
            "pv_kw": max(0.0, float(pv)),
            "grid_power_kw": float(grid),
            "battery_power_kw": float(battery),
            "price_ore_kwh": float(price),
            "battery_soc_start_pct": _num(payload.get("battery_soc_start_pct")),
            "battery_soc_end_pct": _num(payload.get("battery_soc_end_pct")),
            "completeness": _num(payload.get("completeness")),
        })
    expected = _expected_intervals(start, end)
    return rows, {
        "expected_intervals": expected,
        "usable_actual_intervals": len(rows),
        "rejected_actual_intervals": rejected,
        "actual_coverage_fraction": round(len(rows) / expected, 4),
        "first": rows[0]["start"] if rows else None,
        "last": rows[-1]["start"] if rows else None,
    }


def _plan_actions(start: datetime, end: datetime) -> dict[datetime, dict[str, Any]]:
    query_start = start - timedelta(minutes=MAX_PLAN_AGE_MINUTES)
    query_end = end + timedelta(seconds=DECISION_GRACE_SECONDS)
    try:
        with sqlite3.connect(DB_PATH) as c:
            rows = c.execute(
                "SELECT generated_at,start_utc,planner,battery_action_kw,expected_soc_pct,payload_json "
                "FROM optimizer_plan WHERE generated_at>=? AND generated_at<? ORDER BY generated_at",
                (query_start.isoformat(), query_end.isoformat()),
            ).fetchall()
    except sqlite3.OperationalError:
        return {}
    candidates: dict[datetime, list[dict[str, Any]]] = {}
    for generated_raw, start_raw, planner, action_kw, expected_soc, payload_raw in rows:
        try:
            generated = _dt(generated_raw)
            interval_start = _dt(start_raw).replace(second=0, microsecond=0)
            if not (start <= interval_start < end):
                continue
            lag = (generated - interval_start).total_seconds()
            if lag > DECISION_GRACE_SECONDS or lag < -MAX_PLAN_AGE_MINUTES * 60:
                continue
            payload = json.loads(payload_raw or "{}")
        except Exception:
            continue
        candidates.setdefault(interval_start, []).append({
            "generated_at": generated.isoformat(),
            "planner": str(planner or "unknown"),
            "lag_seconds": lag,
            "battery_action_kw": float(action_kw),
            "expected_soc_pct": _num(expected_soc),
            "reserve_soc_pct": _num(payload.get("reserve_soc_pct")),
            "reason": payload.get("reason"),
        })
    return {
        stamp: max(items, key=lambda x: _dt(x["generated_at"]))
        for stamp, items in candidates.items()
    }


def _actual_interval(row: dict[str, Any], cfg: dict[str, Any]) -> dict[str, float]:
    econ = (cfg.get("policy") or {}).get("economics") or {}
    opt = cfg.get("optimizer") or {}
    grid = float(row["grid_power_kw"])
    batt = float(row["battery_power_kw"])
    imp = max(0.0, grid)
    exp = max(0.0, -grid)
    buy = float(row["price_ore_kwh"]) + float(econ.get("import_overhead_ore_kwh", 0.0))
    sell = max(0.0, float(row["price_ore_kwh"]) - float(econ.get("export_overhead_ore_kwh", 0.0)))
    energy_cost = (imp * buy - exp * sell) * DT_HOURS
    degradation = abs(batt) * DT_HOURS * float(opt.get("battery_degradation_ore_kwh", 5.0))
    return {
        "grid_import_kw": imp,
        "grid_export_kw": exp,
        "energy_cost_ore": energy_cost,
        "degradation_cost_ore": degradation,
        "cash_cost_ore": energy_cost + degradation,
        "throughput_kwh": abs(batt) * DT_HOURS,
        "charge_kwh": max(0.0, -batt) * DT_HOURS,
        "discharge_kwh": max(0.0, batt) * DT_HOURS,
    }


def _direction(value: float) -> int:
    if value > DIRECTION_THRESHOLD_KW:
        return 1
    if value < -DIRECTION_THRESHOLD_KW:
        return -1
    return 0


def _active_tariffs(cfg: dict[str, Any]) -> list[str]:
    tariffs = cfg.get("tariffs") or {}
    if not bool(tariffs.get("enabled", False)):
        return []
    return [
        name for name, item in tariffs.items()
        if name != "enabled" and isinstance(item, dict) and bool(item.get("enabled", False))
    ]


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
    active_tariffs = _active_tariffs(cfg)
    rows, data = _actual_rows(a, b)
    actions = _plan_actions(a, b)
    expected = int(data["expected_intervals"])
    matched = sum(1 for row in rows if _dt(row["start"]).replace(second=0, microsecond=0) in actions)
    plan_coverage = matched / max(1, expected)
    data.update({
        "matched_plan_actions": matched,
        "plan_action_coverage_fraction": round(plan_coverage, 4),
        "minimum_plan_coverage_fraction": round(float(min_plan_coverage), 4),
        "minimum_actual_coverage_fraction": round(float(min_actual_coverage), 4),
    })
    if active_tariffs:
        return {
            "engine": ENGINE_NAME,
            "status": "unsupported_active_tariffs",
            "window": {"start": a.isoformat(), "end": b.isoformat()},
            "active_tariffs": active_tariffs,
            "data": data,
            "winner": None,
            "notes": "v1 compares spot-energy economics plus battery degradation and terminal energy value; active monthly demand tariffs require a tariff-aware comparison before a winner can be declared",
        }
    if not rows:
        return {
            "engine": ENGINE_NAME,
            "status": "no_actual_data",
            "window": {"start": a.isoformat(), "end": b.isoformat()},
            "data": data,
            "winner": None,
        }

    first_soc = next((r["battery_soc_start_pct"] for r in rows if r["battery_soc_start_pct"] is not None), None)
    actual_terminal_soc = next((r["battery_soc_end_pct"] for r in reversed(rows) if r["battery_soc_end_pct"] is not None), None)
    if first_soc is None or actual_terminal_soc is None:
        return {
            "engine": ENGINE_NAME,
            "status": "missing_soc",
            "window": {"start": a.isoformat(), "end": b.isoformat()},
            "data": data,
            "winner": None,
        }

    battery_cfg = (cfg.get("policy") or {}).get("battery") or {}
    econ = (cfg.get("policy") or {}).get("economics") or {}
    cap = float(battery_cfg.get("capacity_kwh", 19.6))
    hmin = float(battery_cfg.get("hard_min_soc_pct", 5.0))
    hmax = float(battery_cfg.get("hard_max_soc_pct", 100.0))
    initial_soc = max(hmin, min(hmax, float(first_soc)))
    energy = cap * initial_soc / 100.0
    initial_energy = energy

    actual_cash = actual_throughput = actual_charge = actual_discharge = 0.0
    actual_import = actual_export = 0.0
    planner_cash = planner_throughput = planner_charge = planner_discharge = 0.0
    planner_import = planner_export = 0.0
    actual_peak_import = planner_peak_import = 0.0
    clamped = 0
    action_abs_errors: list[float] = []
    direction_matches = 0
    direction_n = 0
    planner_counts: dict[str, int] = {}
    lags: list[float] = []
    output_rows: list[dict[str, Any]] = []

    for row in rows:
        stamp = _dt(row["start"]).replace(second=0, microsecond=0)
        decision = actions.get(stamp)
        actual = _actual_interval(row, cfg)
        actual_cash += actual["cash_cost_ore"]
        actual_throughput += actual["throughput_kwh"]
        actual_charge += actual["charge_kwh"]
        actual_discharge += actual["discharge_kwh"]
        actual_import += actual["grid_import_kw"] * DT_HOURS
        actual_export += actual["grid_export_kw"] * DT_HOURS
        actual_peak_import = max(actual_peak_import, actual["grid_import_kw"])

        requested = 0.0 if decision is None else float(decision["battery_action_kw"])
        reserve_soc = None if decision is None else decision.get("reserve_soc_pct")
        applied = _apply_action(row, requested, energy, cfg, reserve_soc)
        energy = float(applied["energy_end_kwh"])
        planner_cash += float(applied["cash_cost_ore"])
        planner_throughput += float(applied["throughput_kwh"])
        if float(applied["applied_action_kw"]) < 0:
            planner_charge += -float(applied["applied_action_kw"]) * DT_HOURS
        else:
            planner_discharge += float(applied["applied_action_kw"]) * DT_HOURS
        planner_import += float(applied["grid_import_kw"]) * DT_HOURS
        planner_export += float(applied["grid_export_kw"]) * DT_HOURS
        planner_peak_import = max(planner_peak_import, float(applied["grid_import_kw"]))
        clamped += int(bool(applied["clamped"]))

        if decision is not None:
            planner = str(decision.get("planner") or "unknown")
            planner_counts[planner] = planner_counts.get(planner, 0) + 1
            lags.append(float(decision.get("lag_seconds") or 0.0))
            actual_action = float(row["battery_power_kw"])
            planner_action = float(applied["applied_action_kw"])
            action_abs_errors.append(abs(actual_action - planner_action))
            direction_matches += int(_direction(actual_action) == _direction(planner_action))
            direction_n += 1

        if include_rows:
            output_rows.append({
                "start": row["start"],
                "price_ore_kwh": round(float(row["price_ore_kwh"]), 4),
                "load_kw": round(float(row["load_kw"]), 4),
                "pv_kw": round(float(row["pv_kw"]), 4),
                "plan_available": decision is not None,
                "planner": None if decision is None else decision.get("planner"),
                "actual_battery_action_kw": round(float(row["battery_power_kw"]), 4),
                "planner_requested_action_kw": round(requested, 4),
                "planner_applied_action_kw": round(float(applied["applied_action_kw"]), 4),
                "actual_soc_end_pct": None if row["battery_soc_end_pct"] is None else round(float(row["battery_soc_end_pct"]), 2),
                "planner_virtual_soc_end_pct": round(float(applied["soc_end_pct"]), 2),
                "actual_grid_import_kw": round(float(actual["grid_import_kw"]), 4),
                "actual_grid_export_kw": round(float(actual["grid_export_kw"]), 4),
                "planner_grid_import_kw": round(float(applied["grid_import_kw"]), 4),
                "planner_grid_export_kw": round(float(applied["grid_export_kw"]), 4),
                "planner_action_clamped": bool(applied["clamped"]),
            })

    planner_terminal_soc = energy / cap * 100.0
    reference_price = median([
        float(r["price_ore_kwh"]) + float(econ.get("import_overhead_ore_kwh", 0.0))
        for r in rows
    ])
    actual_terminal_energy = cap * max(hmin, min(hmax, float(actual_terminal_soc))) / 100.0
    planner_terminal_energy = energy
    actual_terminal_adjustment = (actual_terminal_energy - initial_energy) * reference_price
    planner_terminal_adjustment = (planner_terminal_energy - initial_energy) * reference_price
    actual_economic_cost = actual_cash - actual_terminal_adjustment
    planner_economic_cost = planner_cash - planner_terminal_adjustment
    advantage = actual_economic_cost - planner_economic_cost

    actual_coverage_ok = float(data["actual_coverage_fraction"]) >= float(min_actual_coverage)
    plan_coverage_ok = plan_coverage >= float(min_plan_coverage)
    single_planner = len(planner_counts) == 1
    valid = actual_coverage_ok and plan_coverage_ok and single_planner
    if not actual_coverage_ok:
        status = "insufficient_actual_coverage"
    elif not plan_coverage_ok:
        status = "insufficient_plan_coverage"
    elif not single_planner:
        status = "mixed_planner_versions"
    else:
        status = "valid"

    if valid:
        if advantage > 0.005:
            winner = "shadow_planner"
        elif advantage < -0.005:
            winner = "actual_app"
        else:
            winner = "tie"
    else:
        winner = None

    result = {
        "engine": ENGINE_NAME,
        "status": status,
        "valid_comparison": valid,
        "winner": winner,
        "window": {
            "start": a.isoformat(),
            "end": b.isoformat(),
            "hours": round((b - a).total_seconds() / 3600.0, 3),
        },
        "data": data,
        "planner": {
            "counts": planner_counts,
            "single_planner": single_planner,
            "mean_generation_lag_seconds": round(sum(lags) / len(lags), 2) if lags else None,
            "clamped_action_intervals": clamped,
        },
        "actual_app": {
            "initial_soc_pct": round(initial_soc, 2),
            "terminal_soc_pct": round(float(actual_terminal_soc), 2),
            "cash_cost_ore": round(actual_cash, 2),
            "cash_cost_sek": round(actual_cash / 100.0, 2),
            "terminal_asset_adjustment_ore": round(actual_terminal_adjustment, 2),
            "economic_cost_ore": round(actual_economic_cost, 2),
            "economic_cost_sek": round(actual_economic_cost / 100.0, 2),
            "battery_throughput_kwh": round(actual_throughput, 3),
            "battery_charge_kwh": round(actual_charge, 3),
            "battery_discharge_kwh": round(actual_discharge, 3),
            "grid_import_kwh": round(actual_import, 3),
            "grid_export_kwh": round(actual_export, 3),
            "peak_grid_import_kw_15m": round(actual_peak_import, 3),
        },
        "shadow_planner": {
            "initial_soc_pct": round(initial_soc, 2),
            "terminal_soc_pct": round(planner_terminal_soc, 2),
            "cash_cost_ore": round(planner_cash, 2),
            "cash_cost_sek": round(planner_cash / 100.0, 2),
            "terminal_asset_adjustment_ore": round(planner_terminal_adjustment, 2),
            "economic_cost_ore": round(planner_economic_cost, 2),
            "economic_cost_sek": round(planner_economic_cost / 100.0, 2),
            "battery_throughput_kwh": round(planner_throughput, 3),
            "battery_charge_kwh": round(planner_charge, 3),
            "battery_discharge_kwh": round(planner_discharge, 3),
            "grid_import_kwh": round(planner_import, 3),
            "grid_export_kwh": round(planner_export, 3),
            "peak_grid_import_kw_15m": round(planner_peak_import, 3),
        },
        "comparison": {
            "planner_advantage_ore": round(advantage, 2),
            "planner_advantage_sek": round(advantage / 100.0, 2),
            "cash_cost_difference_ore": round(actual_cash - planner_cash, 2),
            "terminal_soc_difference_pct": round(planner_terminal_soc - float(actual_terminal_soc), 2),
            "battery_throughput_difference_kwh": round(planner_throughput - actual_throughput, 3),
            "grid_import_difference_kwh": round(planner_import - actual_import, 3),
            "grid_export_difference_kwh": round(planner_export - actual_export, 3),
            "peak_grid_import_difference_kw_15m": round(planner_peak_import - actual_peak_import, 3),
            "action_mae_kw_on_matched_intervals": round(sum(action_abs_errors) / len(action_abs_errors), 4) if action_abs_errors else None,
            "action_direction_agreement_fraction": round(direction_matches / direction_n, 4) if direction_n else None,
        },
        "valuation": {
            "reference_price_ore_kwh": round(reference_price, 3),
            "terminal_energy_method": "each path receives the same reference-price credit/debit for terminal battery energy relative to the common initial SOC",
            "economic_cost_definition": "spot import cost minus spot export revenue plus battery degradation minus terminal battery asset adjustment",
            "planner_advantage_definition": "actual_app economic cost minus shadow_planner economic cost; positive means the planner would have been cheaper",
        },
        "limitations": [
            "counterfactual assumes realized house load and PV are unchanged by battery control",
            "actual app cost uses measured grid power and measured battery power; planner cost replays stored shadow actions through the deterministic battery model",
            "v1 excludes monthly demand/effect tariffs and refuses to declare a winner when such tariffs are active",
            "missing planner actions are replayed as zero only for diagnostics; no winner is declared unless minimum plan coverage is met",
            "peak_grid_import_kw_15m is a 15-minute average diagnostic, not a tariff billing metric",
        ],
    }
    if include_rows:
        result["rows"] = output_rows
    return result
