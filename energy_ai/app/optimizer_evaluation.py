from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from statistics import mean, median
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from .db import DB_PATH
from .optimizer import DT_HOURS, _reserve_policy_penalty_ore
from .tariff_scenarios import _LP

LOCAL_TZ = ZoneInfo("Europe/Stockholm")
ENGINE_NAME = "optimizer_realized_hindsight_v1"
DECISION_GRACE_SECONDS = 180
MAX_PLAN_AGE_MINUTES = 30


def _dt(value: str) -> datetime:
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _day_bounds(local_date: date) -> tuple[datetime, datetime]:
    start = datetime.combine(local_date, time.min, tzinfo=LOCAL_TZ)
    end = datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=LOCAL_TZ)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _expected_intervals(local_date: date) -> int:
    start, end = _day_bounds(local_date)
    return int(round((end - start).total_seconds() / 900.0))


def _init_tables() -> None:
    with sqlite3.connect(DB_PATH) as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS optimizer_day_eval(
            local_date TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            engine TEXT NOT NULL,
            planner TEXT,
            payload_json TEXT NOT NULL
        );
        ''')


def _num(value: Any) -> float | None:
    if value in (None, "", "null", "None"):
        return None
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _actual_rows(local_date: date) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    start, end = _day_bounds(local_date)
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
    for stamp_raw, payload_raw in states:
        try:
            stamp = _dt(stamp_raw).replace(second=0, microsecond=0)
            payload = json.loads(payload_raw)
        except Exception:
            continue
        means = payload.get("mean") or {}
        load = _num(means.get("house_load_kw"))
        pv = _num(means.get("pv_power_kw"))
        price = _num(means.get("spot_price_ore_kwh"))
        if price is None:
            price = price_map.get(stamp)
        if load is None or pv is None or price is None:
            continue
        rows.append({
            "start": stamp.isoformat(),
            "load_kw": max(0.0, load),
            "pv_kw": max(0.0, pv),
            "price_ore_kwh": float(price),
            "battery_soc_start_pct": _num(payload.get("battery_soc_start_pct")),
            "battery_soc_end_pct": _num(payload.get("battery_soc_end_pct")),
            "completeness": _num(payload.get("completeness")),
        })

    expected = _expected_intervals(local_date)
    return rows, {
        "local_date": local_date.isoformat(),
        "expected_intervals": expected,
        "usable_actual_intervals": len(rows),
        "actual_coverage_fraction": round(len(rows) / max(1, expected), 4),
        "first": rows[0]["start"] if rows else None,
        "last": rows[-1]["start"] if rows else None,
    }


def _plan_actions(local_date: date) -> dict[datetime, dict[str, Any]]:
    start, end = _day_bounds(local_date)
    query_start = start - timedelta(minutes=MAX_PLAN_AGE_MINUTES)
    query_end = end + timedelta(seconds=DECISION_GRACE_SECONDS)
    try:
        with sqlite3.connect(DB_PATH) as c:
            rows = c.execute(
                "SELECT generated_at,start_utc,planner,payload_json FROM optimizer_plan WHERE generated_at>=? AND generated_at<? ORDER BY generated_at",
                (query_start.isoformat(), query_end.isoformat()),
            ).fetchall()
    except sqlite3.OperationalError:
        return {}

    candidates: dict[datetime, list[dict[str, Any]]] = {}
    for generated_raw, start_raw, planner, payload_raw in rows:
        try:
            generated = _dt(generated_raw)
            interval_start = _dt(start_raw).replace(second=0, microsecond=0)
            if not (start <= interval_start < end):
                continue
            lag = (generated - interval_start).total_seconds()
            if lag > DECISION_GRACE_SECONDS or lag < -MAX_PLAN_AGE_MINUTES * 60:
                continue
            payload = json.loads(payload_raw)
        except Exception:
            continue
        candidates.setdefault(interval_start, []).append({
            "generated_at": generated.isoformat(),
            "planner": planner,
            "lag_seconds": lag,
            "battery_action_kw": _num(payload.get("battery_action_kw")) or 0.0,
            "forecast_load_kw": _num(payload.get("load_kw")),
            "forecast_pv_kw": _num(payload.get("pv_kw")),
            "forecast_price_ore_kwh": _num(payload.get("price_ore_kwh")),
            "forecast_soc_end_pct": _num(payload.get("expected_soc_pct")),
            "reserve_soc_pct": _num(payload.get("reserve_soc_pct")),
            "reason": payload.get("reason"),
        })

    chosen: dict[datetime, dict[str, Any]] = {}
    for interval_start, items in candidates.items():
        # The deployed maintenance loop refreshes shortly after a quarter boundary.
        # Prefer the freshest decision available within the small grace window.
        chosen[interval_start] = max(items, key=lambda x: _dt(x["generated_at"]))
    return chosen


def _baseline_interval(row: dict[str, Any], cfg: dict[str, Any]) -> dict[str, float]:
    econ = (cfg.get("policy") or {}).get("economics") or {}
    opt = cfg.get("optimizer") or {}
    net = float(row["load_kw"]) - float(row["pv_kw"])
    imp = max(0.0, net)
    raw_export = max(0.0, -net)
    export_limit = float(opt.get("grid_export_limit_kw", 10.0))
    exp = min(raw_export, export_limit)
    price = float(row["price_ore_kwh"])
    buy = price + float(econ.get("import_overhead_ore_kwh", 0.0))
    sell = max(0.0, price - float(econ.get("export_overhead_ore_kwh", 0.0)))
    return {
        "grid_import_kw": imp,
        "grid_export_kw": exp,
        "curtailed_kw": max(0.0, raw_export - export_limit),
        "cash_cost_ore": (imp * buy - exp * sell) * DT_HOURS,
    }


def _apply_action(
    row: dict[str, Any],
    requested_action_kw: float,
    energy_kwh: float,
    cfg: dict[str, Any],
    reserve_soc_pct: float | None,
) -> dict[str, float | bool]:
    battery = (cfg.get("policy") or {}).get("battery") or {}
    econ = (cfg.get("policy") or {}).get("economics") or {}
    opt = cfg.get("optimizer") or {}
    cap = float(battery.get("capacity_kwh", 19.6))
    hmin = float(battery.get("hard_min_soc_pct", 5.0))
    hmax = float(battery.get("hard_max_soc_pct", 100.0))
    pmin = float(battery.get("preferred_min_soc_pct", 15.0))
    pmax = float(battery.get("preferred_max_soc_pct", 90.0))
    ec = float(opt.get("battery_charge_efficiency", 0.95))
    ed = float(opt.get("battery_discharge_efficiency", 0.95))
    cmax = float(opt.get("battery_max_charge_kw", 8.0))
    dmax = float(opt.get("battery_max_discharge_kw", 8.0))
    ilim = float(opt.get("physical_grid_import_limit_kw", 13.8))
    elim = float(opt.get("grid_export_limit_kw", 10.0))
    min_e = cap * hmin / 100.0
    max_e = cap * hmax / 100.0
    net = float(row["load_kw"]) - float(row["pv_kw"])
    requested = float(requested_action_kw)

    if requested >= 0.0:
        by_soc = max(0.0, energy_kwh - min_e) * ed / DT_HOURS
        by_export = max(0.0, net + elim)
        action = min(requested, dmax, by_soc, by_export)
    else:
        charge_requested = -requested
        by_soc = max(0.0, max_e - energy_kwh) / max(1e-9, ec * DT_HOURS)
        by_import = max(0.0, ilim - net)
        charge = min(charge_requested, cmax, by_soc, by_import)
        action = -charge

    clamped = abs(action - requested) > 1e-6
    if action >= 0.0:
        end_e = energy_kwh - action * DT_HOURS / max(1e-9, ed)
    else:
        end_e = energy_kwh + (-action) * ec * DT_HOURS
    end_e = min(max_e, max(min_e, end_e))

    grid = net - action
    imp = max(0.0, grid)
    raw_export = max(0.0, -grid)
    exp = min(raw_export, elim)
    curtailed = max(0.0, raw_export - elim)
    price = float(row["price_ore_kwh"])
    buy = price + float(econ.get("import_overhead_ore_kwh", 0.0))
    sell = max(0.0, price - float(econ.get("export_overhead_ore_kwh", 0.0)))
    degradation = abs(action) * DT_HOURS * float(opt.get("battery_degradation_ore_kwh", 5.0))
    energy_cost = (imp * buy - exp * sell) * DT_HOURS
    required = max(0.0, net - ilim)
    discretionary = max(0.0, action - required) if action > 0 else 0.0
    hurdle = discretionary * DT_HOURS * float(econ.get("minimum_arbitrage_margin_ore_kwh", 20.0))
    target_pct = float(reserve_soc_pct) if reserve_soc_pct is not None else float(battery.get("normal_reserve_soc_pct", 20.0))
    reserve_kwh = cap * max(hmin, min(hmax, target_pct)) / 100.0
    reserve_penalty = _reserve_policy_penalty_ore(end_e, reserve_kwh, cfg, cap, hmin, pmin)
    upper_penalty = max(0.0, end_e - cap * pmax / 100.0) * float(opt.get("preferred_max_excess_penalty_ore_per_kwh_hour", 2.0)) * DT_HOURS
    cash = energy_cost + degradation
    return {
        "requested_action_kw": requested,
        "applied_action_kw": action,
        "clamped": clamped,
        "energy_end_kwh": end_e,
        "soc_end_pct": end_e / cap * 100.0,
        "grid_import_kw": imp,
        "grid_export_kw": exp,
        "curtailed_kw": curtailed,
        "import_limit_exceedance_kw": max(0.0, imp - ilim),
        "energy_cost_ore": energy_cost,
        "degradation_cost_ore": degradation,
        "cash_cost_ore": cash,
        "hurdle_cost_ore": hurdle,
        "reserve_policy_penalty_ore": reserve_penalty,
        "preferred_max_excess_penalty_ore": upper_penalty,
        "policy_objective_cost_ore": cash + hurdle + reserve_penalty + upper_penalty,
        "throughput_kwh": abs(action) * DT_HOURS,
    }


def _hindsight(
    rows: list[dict[str, Any]],
    cfg: dict[str, Any],
    initial_soc_pct: float,
    terminal_soc_pct: float,
) -> dict[str, Any]:
    if not rows:
        return {"status": "no_rows"}
    battery = (cfg.get("policy") or {}).get("battery") or {}
    econ = (cfg.get("policy") or {}).get("economics") or {}
    opt = cfg.get("optimizer") or {}
    cap = float(battery.get("capacity_kwh", 19.6))
    hmin = float(battery.get("hard_min_soc_pct", 5.0))
    hmax = float(battery.get("hard_max_soc_pct", 100.0))
    pmin = float(battery.get("preferred_min_soc_pct", 15.0))
    pmax = float(battery.get("preferred_max_soc_pct", 90.0))
    normal = float(battery.get("normal_reserve_soc_pct", 20.0))
    critical = max(hmin, min(pmin, float(opt.get("reserve_critical_soc_pct", 10.0))))
    initial = max(hmin, min(hmax, float(initial_soc_pct)))
    terminal = max(hmin, min(hmax, float(terminal_soc_pct)))
    e0 = cap * initial / 100.0
    eterm = cap * terminal / 100.0
    ec = float(opt.get("battery_charge_efficiency", 0.95))
    ed = float(opt.get("battery_discharge_efficiency", 0.95))
    cmax = float(opt.get("battery_max_charge_kw", 8.0))
    dmax = float(opt.get("battery_max_discharge_kw", 8.0))
    ilim = float(opt.get("physical_grid_import_limit_kw", 13.8))
    elim = float(opt.get("grid_export_limit_kw", 10.0))
    deg = float(opt.get("battery_degradation_ore_kwh", 5.0))
    margin = float(econ.get("minimum_arbitrage_margin_ore_kwh", 20.0))
    ioh = float(econ.get("import_overhead_ore_kwh", 0.0))
    eoh = float(econ.get("export_overhead_ore_kwh", 0.0))
    critical_rate = max(0.0, float(opt.get("reserve_critical_penalty_ore_per_kwh_hour", 300.0)))
    preferred_rate = max(0.0, min(critical_rate, float(opt.get("reserve_preferred_penalty_ore_per_kwh_hour", 100.0))))
    target_rate = max(0.0, min(preferred_rate, float(opt.get("reserve_target_penalty_ore_per_kwh_hour", 10.0))))
    upper_rate = max(0.0, float(opt.get("preferred_max_excess_penalty_ore_per_kwh_hour", 2.0)))

    n = len(rows)
    lp = _LP()
    charge = lp.add_vars("charge", n, 0, cmax)
    discharge = lp.add_vars("discharge", n, 0, dmax)
    imp = lp.add_vars("import", n, 0, ilim)
    exp = lp.add_vars("export", n, 0, elim)
    soc = lp.add_vars("soc", n, cap * hmin / 100.0, cap * hmax / 100.0)
    ybat = lp.add_vars("ybat", n, 0, 1, integral=True)
    ygrid = lp.add_vars("ygrid", n, 0, 1, integral=True)
    discretionary = lp.add_vars("discretionary", n, 0, dmax)
    ztarget = lp.add_vars("ztarget", n, 0, cap)
    zpreferred = lp.add_vars("zpreferred", n, 0, cap)
    zcritical = lp.add_vars("zcritical", n, 0, cap)
    zupper = lp.add_vars("zupper", n, 0, cap)
    target_kwh = cap * normal / 100.0
    preferred_kwh = cap * pmin / 100.0
    critical_kwh = cap * critical / 100.0
    upper_kwh = cap * pmax / 100.0

    for t, row in enumerate(rows):
        net = float(row["load_kw"]) - float(row["pv_kw"])
        lp.constraint({int(imp[t]): 1, int(exp[t]): -1, int(discharge[t]): 1, int(charge[t]): -1}, lb=net, ub=net)
        coeff = {int(soc[t]): 1, int(charge[t]): -ec * DT_HOURS, int(discharge[t]): DT_HOURS / ed}
        if t:
            coeff[int(soc[t - 1])] = -1
            lp.constraint(coeff, lb=0, ub=0)
        else:
            lp.constraint(coeff, lb=e0, ub=e0)
        lp.constraint({int(charge[t]): 1, int(ybat[t]): -cmax}, ub=0)
        lp.constraint({int(discharge[t]): 1, int(ybat[t]): dmax}, ub=dmax)
        lp.constraint({int(imp[t]): 1, int(ygrid[t]): -ilim}, ub=0)
        lp.constraint({int(exp[t]): 1, int(ygrid[t]): elim}, ub=elim)
        required = max(0.0, net - ilim)
        lp.constraint({int(discretionary[t]): 1, int(discharge[t]): -1}, lb=-required)
        price = float(row["price_ore_kwh"])
        buy = price + ioh
        sell = max(0.0, price - eoh)
        lp.set_obj(imp[t], buy * DT_HOURS)
        lp.set_obj(exp[t], -sell * DT_HOURS)
        lp.set_obj(charge[t], deg * DT_HOURS)
        lp.set_obj(discharge[t], deg * DT_HOURS)
        lp.set_obj(discretionary[t], margin * DT_HOURS)
        lp.constraint({int(ztarget[t]): 1, int(soc[t]): 1}, lb=target_kwh)
        lp.constraint({int(zpreferred[t]): 1, int(soc[t]): 1}, lb=preferred_kwh)
        lp.constraint({int(zcritical[t]): 1, int(soc[t]): 1}, lb=critical_kwh)
        lp.constraint({int(zupper[t]): 1, int(soc[t]): -1}, lb=-upper_kwh)
        lp.set_obj(ztarget[t], target_rate * DT_HOURS)
        lp.set_obj(zpreferred[t], (preferred_rate - target_rate) * DT_HOURS)
        lp.set_obj(zcritical[t], (critical_rate - preferred_rate) * DT_HOURS)
        lp.set_obj(zupper[t], upper_rate * DT_HOURS)

    lp.constraint({int(soc[-1]): 1}, lb=eterm, ub=eterm)
    result = lp.solve(time_limit=30)
    if not result.success or result.x is None:
        return {"status": "infeasible_or_timeout", "solver_status": int(result.status), "solver_message": str(result.message)}

    x = result.x
    energy = degradation = hurdle = reserve = upper = throughput = 0.0
    actions = []
    for t, row in enumerate(rows):
        c = max(0.0, float(x[charge[t]]))
        d = max(0.0, float(x[discharge[t]]))
        gi = max(0.0, float(x[imp[t]]))
        ge = max(0.0, float(x[exp[t]]))
        dd = max(0.0, float(x[discretionary[t]]))
        price = float(row["price_ore_kwh"])
        buy = price + ioh
        sell = max(0.0, price - eoh)
        energy += (gi * buy - ge * sell) * DT_HOURS
        degradation += (c + d) * deg * DT_HOURS
        hurdle += dd * margin * DT_HOURS
        reserve += (
            float(x[ztarget[t]]) * target_rate
            + float(x[zpreferred[t]]) * (preferred_rate - target_rate)
            + float(x[zcritical[t]]) * (critical_rate - preferred_rate)
        ) * DT_HOURS
        upper += float(x[zupper[t]]) * upper_rate * DT_HOURS
        throughput += (c + d) * DT_HOURS
        actions.append({
            "start": row["start"],
            "action_kw": round(d - c, 4),
            "soc_end_pct": round(float(x[soc[t]]) / cap * 100.0, 2),
            "grid_import_kw": round(gi, 4),
            "grid_export_kw": round(ge, 4),
        })
    cash = energy + degradation
    return {
        "status": "optimal" if result.status == 0 else "feasible",
        "solver_status": int(result.status),
        "solver_message": str(result.message),
        "initial_soc_pct": round(initial, 3),
        "terminal_soc_pct": round(terminal, 3),
        "cash_cost_ore": round(cash, 2),
        "policy_objective_cost_ore": round(cash + hurdle + reserve + upper, 2),
        "battery_throughput_kwh": round(throughput, 3),
        "actions": actions,
    }


def evaluate_day(cfg: dict[str, Any], local_date: str | date) -> dict[str, Any]:
    day = date.fromisoformat(local_date) if isinstance(local_date, str) else local_date
    rows, data = _actual_rows(day)
    expected = data["expected_intervals"]
    if data["actual_coverage_fraction"] < 0.90:
        return {"engine": ENGINE_NAME, "local_date": day.isoformat(), "status": "insufficient_actual_coverage", "data": data}
    actions = _plan_actions(day)
    matched = 0
    planners: dict[str, int] = {}
    lags = []
    forecast_load_errors = []
    forecast_pv_errors = []
    forecast_net_errors = []
    first_soc = next((r["battery_soc_start_pct"] for r in rows if r["battery_soc_start_pct"] is not None), None)
    if first_soc is None:
        return {"engine": ENGINE_NAME, "local_date": day.isoformat(), "status": "missing_initial_soc", "data": data}

    battery = (cfg.get("policy") or {}).get("battery") or {}
    cap = float(battery.get("capacity_kwh", 19.6))
    hmin = float(battery.get("hard_min_soc_pct", 5.0))
    hmax = float(battery.get("hard_max_soc_pct", 100.0))
    virtual_soc = max(hmin, min(hmax, float(first_soc)))
    energy = cap * virtual_soc / 100.0
    baseline_cash = realtime_cash = realtime_objective = throughput = 0.0
    clamped_intervals = import_proxy_exceedances = 0
    replay_rows = []

    for row in rows:
        stamp = _dt(row["start"]).replace(second=0, microsecond=0)
        decision = actions.get(stamp)
        requested = 0.0
        reserve_soc = None
        if decision is not None:
            matched += 1
            requested = float(decision["battery_action_kw"])
            reserve_soc = decision.get("reserve_soc_pct")
            planners[decision.get("planner") or "unknown"] = planners.get(decision.get("planner") or "unknown", 0) + 1
            lags.append(float(decision["lag_seconds"]))
            fl, fp = decision.get("forecast_load_kw"), decision.get("forecast_pv_kw")
            if fl is not None:
                forecast_load_errors.append(float(row["load_kw"]) - float(fl))
            if fp is not None:
                forecast_pv_errors.append(float(row["pv_kw"]) - float(fp))
            if fl is not None and fp is not None:
                forecast_net_errors.append((float(row["load_kw"]) - float(row["pv_kw"])) - (float(fl) - float(fp)))

        baseline = _baseline_interval(row, cfg)
        applied = _apply_action(row, requested, energy, cfg, reserve_soc)
        energy = float(applied["energy_end_kwh"])
        baseline_cash += float(baseline["cash_cost_ore"])
        realtime_cash += float(applied["cash_cost_ore"])
        realtime_objective += float(applied["policy_objective_cost_ore"])
        throughput += float(applied["throughput_kwh"])
        clamped_intervals += int(bool(applied["clamped"]))
        import_proxy_exceedances += int(float(applied["import_limit_exceedance_kw"]) > 1e-6)
        replay_rows.append({
            "start": row["start"],
            "actual_load_kw": round(float(row["load_kw"]), 4),
            "actual_pv_kw": round(float(row["pv_kw"]), 4),
            "price_ore_kwh": round(float(row["price_ore_kwh"]), 4),
            "plan_available": decision is not None,
            "requested_action_kw": round(requested, 4),
            "applied_action_kw": round(float(applied["applied_action_kw"]), 4),
            "virtual_soc_end_pct": round(float(applied["soc_end_pct"]), 2),
            "grid_import_kw": round(float(applied["grid_import_kw"]), 4),
            "grid_export_kw": round(float(applied["grid_export_kw"]), 4),
            "clamped": bool(applied["clamped"]),
        })

    action_coverage = matched / max(1, len(rows))
    terminal_soc = energy / cap * 100.0
    reference_price = median([float(r["price_ore_kwh"]) + float(((cfg.get("policy") or {}).get("economics") or {}).get("import_overhead_ore_kwh", 0.0)) for r in rows])
    terminal_delta_kwh = energy - cap * virtual_soc * 0.0  # overwritten below for clarity
    initial_energy = cap * max(hmin, min(hmax, float(first_soc))) / 100.0
    terminal_delta_kwh = energy - initial_energy
    terminal_asset_adjustment = terminal_delta_kwh * reference_price
    realtime_economic_cost = realtime_cash - terminal_asset_adjustment
    baseline_economic_cost = baseline_cash

    active_tariffs = [name for name, item in ((cfg.get("tariffs") or {}).items()) if isinstance(item, dict) and item.get("enabled")]
    hindsight = {"status": "skipped_active_tariffs", "active_tariffs": active_tariffs} if active_tariffs else _hindsight(rows, cfg, float(first_soc), terminal_soc)
    if hindsight.get("status") in {"optimal", "feasible"}:
        hindsight_cash = float(hindsight["cash_cost_ore"])
        hindsight_economic_cost = hindsight_cash - terminal_asset_adjustment
        perfect_info_gap = realtime_economic_cost - hindsight_economic_cost
        policy_gap = realtime_objective - float(hindsight["policy_objective_cost_ore"])
    else:
        hindsight_economic_cost = perfect_info_gap = policy_gap = None

    def _err(values: list[float]) -> dict[str, Any]:
        if not values:
            return {"n": 0, "mae_kw": None, "bias_kw": None}
        return {"n": len(values), "mae_kw": round(mean(abs(x) for x in values), 4), "bias_kw": round(mean(values), 4)}

    status = "ok" if action_coverage >= 0.90 else "partial_plan_coverage"
    return {
        "engine": ENGINE_NAME,
        "local_date": day.isoformat(),
        "status": status,
        "data": {**data, "matched_plan_actions": matched, "plan_action_coverage_fraction": round(action_coverage, 4)},
        "decision_timing": {
            "grace_seconds_after_interval_start": DECISION_GRACE_SECONDS,
            "max_plan_age_minutes": MAX_PLAN_AGE_MINUTES,
            "mean_generation_lag_seconds": round(mean(lags), 2) if lags else None,
            "planner_counts": planners,
        },
        "forecast_error_on_executed_intervals": {
            "load": _err(forecast_load_errors),
            "pv": _err(forecast_pv_errors),
            "net_load": _err(forecast_net_errors),
        },
        "realtime_counterfactual": {
            "initial_soc_pct": round(float(first_soc), 2),
            "terminal_soc_pct": round(terminal_soc, 2),
            "terminal_soc_delta_pct": round(terminal_soc - float(first_soc), 2),
            "battery_throughput_kwh": round(throughput, 3),
            "clamped_action_intervals": clamped_intervals,
            "import_proxy_exceedance_intervals": import_proxy_exceedances,
            "cash_cost_ore": round(realtime_cash, 2),
            "terminal_asset_adjustment_ore": round(terminal_asset_adjustment, 2),
            "economic_cost_ore": round(realtime_economic_cost, 2),
            "policy_objective_cost_ore": round(realtime_objective, 2),
        },
        "zero_battery_baseline": {
            "definition": "actual house load minus actual PV with battery action fixed to zero",
            "cash_cost_ore": round(baseline_cash, 2),
            "economic_cost_ore": round(baseline_economic_cost, 2),
        },
        "perfect_hindsight": hindsight,
        "comparison": {
            "realtime_economic_saving_vs_zero_battery_ore": round(baseline_economic_cost - realtime_economic_cost, 2),
            "realtime_economic_saving_vs_zero_battery_sek": round((baseline_economic_cost - realtime_economic_cost) / 100.0, 2),
            "perfect_information_gap_ore": None if perfect_info_gap is None else round(perfect_info_gap, 2),
            "perfect_information_gap_sek": None if perfect_info_gap is None else round(perfect_info_gap / 100.0, 2),
            "policy_objective_gap_ore": None if policy_gap is None else round(policy_gap, 2),
            "regret_definition": "realtime counterfactual economic cost minus perfect-hindsight economic cost at the same terminal SOC; positive means perfect information would have been better",
            "forecast_vs_planner_regret_decomposition": "not_claimed_in_v1; requires a perfect-forecast run through the same realtime planner before separating forecast regret from planner regret",
        },
        "reference_price_ore_kwh_for_terminal_energy": round(reference_price, 3),
        "rows": replay_rows,
    }


def evaluate_matured_optimizer_days(cfg: dict[str, Any], lookback_days: int = 7) -> dict[str, Any]:
    _init_tables()
    today = datetime.now(LOCAL_TZ).date()
    created_at = datetime.now(timezone.utc).isoformat()
    results = []
    stored = 0
    for offset in range(1, max(1, int(lookback_days)) + 1):
        day = today - timedelta(days=offset)
        result = evaluate_day(cfg, day)
        results.append({"local_date": day.isoformat(), "status": result.get("status")})
        if result.get("status") not in {"ok", "partial_plan_coverage"}:
            continue
        planner_counts = ((result.get("decision_timing") or {}).get("planner_counts") or {})
        planner = max(planner_counts, key=planner_counts.get) if planner_counts else None
        with sqlite3.connect(DB_PATH) as c:
            c.execute(
                "INSERT OR REPLACE INTO optimizer_day_eval(local_date,created_at,engine,planner,payload_json) VALUES (?,?,?,?,?)",
                (day.isoformat(), created_at, ENGINE_NAME, planner, json.dumps(result, ensure_ascii=False)),
            )
        stored += 1
    return {"ok": True, "engine": ENGINE_NAME, "lookback_days": lookback_days, "days_stored": stored, "days": results}


def evaluation_report(days: int = 30) -> dict[str, Any]:
    _init_tables()
    cutoff = (datetime.now(LOCAL_TZ).date() - timedelta(days=max(1, int(days)))).isoformat()
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            "SELECT local_date,payload_json FROM optimizer_day_eval WHERE local_date>=? ORDER BY local_date",
            (cutoff,),
        ).fetchall()
    payloads = []
    for _, raw in rows:
        try:
            payloads.append(json.loads(raw))
        except Exception:
            pass
    good = [p for p in payloads if p.get("status") == "ok"]
    partial = [p for p in payloads if p.get("status") == "partial_plan_coverage"]
    savings = [float((p.get("comparison") or {}).get("realtime_economic_saving_vs_zero_battery_ore")) for p in good if (p.get("comparison") or {}).get("realtime_economic_saving_vs_zero_battery_ore") is not None]
    regrets = [float((p.get("comparison") or {}).get("perfect_information_gap_ore")) for p in good if (p.get("comparison") or {}).get("perfect_information_gap_ore") is not None]
    coverages = [float((p.get("data") or {}).get("plan_action_coverage_fraction") or 0.0) for p in payloads]
    clamped = [int((p.get("realtime_counterfactual") or {}).get("clamped_action_intervals") or 0) for p in payloads]
    return {
        "engine": ENGINE_NAME,
        "window_days": days,
        "stored_days": len(payloads),
        "complete_days": len(good),
        "partial_days": len(partial),
        "first_day": payloads[0]["local_date"] if payloads else None,
        "last_day": payloads[-1]["local_date"] if payloads else None,
        "mean_plan_action_coverage_fraction": round(mean(coverages), 4) if coverages else None,
        "total_realtime_economic_saving_vs_zero_battery_sek": round(sum(savings) / 100.0, 2) if savings else None,
        "mean_daily_realtime_economic_saving_sek": round(mean(savings) / 100.0, 2) if savings else None,
        "total_perfect_information_gap_sek": round(sum(regrets) / 100.0, 2) if regrets else None,
        "mean_daily_perfect_information_gap_sek": round(mean(regrets) / 100.0, 2) if regrets else None,
        "total_clamped_action_intervals": sum(clamped),
        "interpretation": {
            "saving": "positive means the recorded receding-horizon shadow actions would have beaten a reconstructed zero-battery baseline after valuing terminal battery energy",
            "perfect_information_gap": "positive means a same-terminal-SOC perfect-hindsight optimizer could have done better on the realized load/PV/prices",
            "causality_limit": "v1 does not label the entire perfect-information gap as forecast error; planner-vs-forecast regret decomposition is deferred",
        },
        "promotion_policy": "evaluation_only; never auto-change or auto-promote planner parameters",
    }
