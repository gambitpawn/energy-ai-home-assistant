from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from .db import DB_PATH
from .optimizer import (
    DT_HOURS,
    _build_horizon,
    _classify_action,
    _continuation_profile,
    _dynamic_reserve_kwh,
    _latest_soc_pct,
    build_plan as build_v35_plan,
    horizon_diagnostics,
)
from .tariff_scenarios import LOCAL_TZ, _LP, _calendar_active, _hour_groups, _tariff_metric_from_hourly

PLANNER_NAME = "tariff_aware_battery_milp_v1"
BASE_PLANNER_NAME = "deterministic_battery_dp_v3_5"
TARIFF_NAMES = ("consumption_demand", "production_demand")


def _tariff_config(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    return dict(((cfg.get("tariffs") or {}).get(name) or {}))


def active_tariff_names(cfg: dict[str, Any]) -> list[str]:
    tariffs = cfg.get("tariffs") or {}
    if not bool(tariffs.get("enabled", False)):
        return []
    return [name for name in TARIFF_NAMES if bool(_tariff_config(cfg, name).get("enabled", False))]


def _canonical_ts(value: str) -> datetime:
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _historical_tariff_state(cfg: dict[str, Any], name: str, now: datetime | None = None) -> dict[str, Any]:
    tariff = _tariff_config(cfg, name)
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now_local = now_utc.astimezone(LOCAL_TZ)
    month_start_local = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_start_utc = month_start_local.astimezone(timezone.utc)
    current_hour_local = now_local.replace(minute=0, second=0, microsecond=0)

    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            "SELECT bucket_start,payload_json FROM state_15m WHERE bucket_start>=? AND bucket_start<? ORDER BY bucket_start ASC",
            (month_start_utc.isoformat(), now_utc.isoformat()),
        ).fetchall()

    groups: dict[tuple[str, int], dict[int, float]] = defaultdict(dict)
    usable_quarters = 0
    for stamp_raw, payload_raw in rows:
        try:
            stamp = _canonical_ts(str(stamp_raw))
            local = stamp.astimezone(LOCAL_TZ)
            if local >= current_hour_local:
                continue
            if not _calendar_active(local, tariff, False):
                continue
            payload = json.loads(payload_raw)
            grid = ((payload.get("mean") or {}).get("grid_power_kw"))
            if grid is None:
                continue
            grid = float(grid)
            value = max(0.0, grid) if tariff.get("kind") == "import_top3_mean" else max(0.0, -grid)
            groups[(local.date().isoformat(), local.hour)][local.minute] = value
            usable_quarters += 1
        except Exception:
            continue

    hourly = []
    for (day, hour), quarters in sorted(groups.items()):
        if set(quarters) != {0, 15, 30, 45}:
            continue
        hourly.append({"date": day, "hour": hour, "kw": sum(quarters.values()) / 4.0})

    values = [float(x["kw"]) for x in hourly]
    if tariff.get("kind") == "import_top3_mean":
        carried = sorted(values, reverse=True)[:3]
    else:
        carried = [max(values)] if values else []
    metric = _tariff_metric_from_hourly([], tariff, carried)
    return {
        "month": f"{now_local.year:04d}-{now_local.month:02d}",
        "completed_active_hours": len(hourly),
        "usable_quarters": usable_quarters,
        "historical_values_kw": [round(x, 4) for x in carried],
        "historical_metric_kw": round(float(metric.get("metric_kw") or 0.0), 4),
        "top_hours": sorted(hourly, key=lambda x: x["kw"], reverse=True)[:10],
        "source": "state_15m.mean.grid_power_kw",
    }


def tariff_state(cfg: dict[str, Any]) -> dict[str, Any]:
    names = active_tariff_names(cfg)
    return {
        "enabled": bool(names),
        "active_tariffs": names,
        "tariffs": {name: _historical_tariff_state(cfg, name) for name in names},
    }


def _evaluate(rows: list[dict[str, Any]], tariff: dict[str, Any], historical: list[float]) -> dict[str, Any]:
    groups = _hour_groups(rows, tariff, False)
    key = "grid_import_kw" if tariff.get("kind") == "import_top3_mean" else "grid_export_kw"
    hourly, details = [], []
    for group in groups:
        value = sum(float(rows[i].get(key) or 0.0) for i in group["indices"]) / 4.0
        hourly.append(value)
        details.append({"date": group["date"], "hour": group["hour"], "kw": round(value, 4)})
    metric = _tariff_metric_from_hourly(hourly, tariff, historical)
    return {
        **metric,
        "historical_values_kw": [round(float(x), 4) for x in historical],
        "forecast_active_hours": len(groups),
        "max_forecast_hour_kw": round(max(hourly, default=0.0), 4),
        "top_forecast_hours": sorted(details, key=lambda x: x["kw"], reverse=True)[:10],
    }


def _baseline_known_cash(plan: dict[str, Any]) -> float:
    return sum(float(r.get("cash_cost_ore") or 0.0) for r in (plan.get("rows") or []) if r.get("price_known"))


def _known_rows(rows_full: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in rows_full:
        if not row.get("price_known"):
            break
        out.append(dict(row))
    return out


def _has_active_forecast_groups(rows: list[dict[str, Any]], cfg: dict[str, Any], names: list[str]) -> bool:
    return any(_hour_groups(rows, _tariff_config(cfg, name), False) for name in names)


def build_tariff_plan(cfg: dict[str, Any], names: list[str] | None = None) -> dict[str, Any]:
    names = list(names if names is not None else active_tariff_names(cfg))
    if not names:
        return build_v35_plan(cfg)

    rows_full = _build_horizon(cfg)
    rows = _known_rows(rows_full)
    if not rows or not _has_active_forecast_groups(rows, cfg, names):
        return build_v35_plan(cfg)

    base = build_v35_plan(cfg)
    opt = cfg.get("optimizer") or {}
    battery = (cfg.get("policy") or {}).get("battery") or {}
    econ = (cfg.get("policy") or {}).get("economics") or {}
    cap = float(battery.get("capacity_kwh", 19.6))
    hmin = float(battery.get("hard_min_soc_pct", 5.0))
    hmax = float(battery.get("hard_max_soc_pct", 100.0))
    pmin = float(battery.get("preferred_min_soc_pct", 15.0))
    pmax = float(battery.get("preferred_max_soc_pct", 90.0))
    critical = max(hmin, min(pmin, float(opt.get("reserve_critical_soc_pct", 10.0))))
    initial_measured = _latest_soc_pct()
    if initial_measured is None:
        raise RuntimeError("Current battery SOC is unavailable")
    initial = max(hmin, min(hmax, float(initial_measured)))
    initial_kwh = cap * initial / 100.0

    ec = float(opt.get("battery_charge_efficiency", 0.95))
    ed = float(opt.get("battery_discharge_efficiency", 0.95))
    cmax = float(opt.get("battery_max_charge_kw", 8.0))
    dmax = float(opt.get("battery_max_discharge_kw", 8.0))
    ilim = float(opt.get("physical_grid_import_limit_kw", 13.8))
    elim = float(opt.get("grid_export_limit_kw", 10.0))
    degradation_rate = float(opt.get("battery_degradation_ore_kwh", 5.0))
    margin = float(econ.get("minimum_arbitrage_margin_ore_kwh", 20.0))
    import_overhead = float(econ.get("import_overhead_ore_kwh", 0.0))
    export_overhead = float(econ.get("export_overhead_ore_kwh", 0.0))
    target_rate = max(0.0, float(opt.get("reserve_target_penalty_ore_per_kwh_hour", 10.0)))
    preferred_rate = max(target_rate, float(opt.get("reserve_preferred_penalty_ore_per_kwh_hour", 100.0)))
    critical_rate = max(preferred_rate, float(opt.get("reserve_critical_penalty_ore_per_kwh_hour", 300.0)))
    upper_rate = max(0.0, float(opt.get("preferred_max_excess_penalty_ore_per_kwh_hour", 2.0)))

    n = len(rows)
    lp = _LP()
    charge = lp.add_vars("charge", n, 0, cmax)
    discharge = lp.add_vars("discharge", n, 0, dmax)
    imp = lp.add_vars("import", n, 0, ilim)
    exp = lp.add_vars("export", n, 0, elim)
    soc = lp.add_vars("soc", n, cap * hmin / 100.0, cap * hmax / 100.0)
    ybat = lp.add_vars("battery_direction", n, 0, 1, integral=True)
    ygrid = lp.add_vars("grid_direction", n, 0, 1, integral=True)
    discretionary = lp.add_vars("discretionary_discharge", n, 0, dmax)
    ztarget = lp.add_vars("reserve_target_shortfall", n, 0, cap)
    zpreferred = lp.add_vars("reserve_preferred_shortfall", n, 0, cap)
    zcritical = lp.add_vars("reserve_critical_shortfall", n, 0, cap)
    zupper = lp.add_vars("preferred_max_excess", n, 0, cap)

    preferred_kwh = cap * pmin / 100.0
    critical_kwh = cap * critical / 100.0
    upper_kwh = cap * pmax / 100.0
    reserve_pct_by_row: list[float] = []

    for t, row in enumerate(rows):
        net = float(row["load_kw"]) - float(row["pv_kw"])
        lp.constraint({int(imp[t]): 1, int(exp[t]): -1, int(discharge[t]): 1, int(charge[t]): -1}, lb=net, ub=net)
        coeff = {int(soc[t]): 1, int(charge[t]): -ec * DT_HOURS, int(discharge[t]): DT_HOURS / ed}
        if t:
            coeff[int(soc[t - 1])] = -1
            lp.constraint(coeff, lb=0, ub=0)
        else:
            lp.constraint(coeff, lb=initial_kwh, ub=initial_kwh)

        lp.constraint({int(charge[t]): 1, int(ybat[t]): -cmax}, ub=0)
        lp.constraint({int(discharge[t]): 1, int(ybat[t]): dmax}, ub=dmax)
        lp.constraint({int(imp[t]): 1, int(ygrid[t]): -ilim}, ub=0)
        lp.constraint({int(exp[t]): 1, int(ygrid[t]): elim}, ub=elim)

        required = max(0.0, net - ilim)
        lp.constraint({int(discretionary[t]): 1, int(discharge[t]): -1}, lb=-required)

        buy = float(row["price_ore_kwh"]) + import_overhead
        sell = max(0.0, float(row["price_ore_kwh"]) - export_overhead)
        lp.set_obj(imp[t], buy * DT_HOURS)
        lp.set_obj(exp[t], -sell * DT_HOURS)
        lp.set_obj(charge[t], degradation_rate * DT_HOURS)
        lp.set_obj(discharge[t], degradation_rate * DT_HOURS)
        lp.set_obj(discretionary[t], margin * DT_HOURS)

        reserve_kwh, reserve_pct = _dynamic_reserve_kwh(row, cfg, cap)
        reserve_pct_by_row.append(reserve_pct)
        lp.constraint({int(ztarget[t]): 1, int(soc[t]): 1}, lb=reserve_kwh)
        lp.constraint({int(zpreferred[t]): 1, int(soc[t]): 1}, lb=preferred_kwh)
        lp.constraint({int(zcritical[t]): 1, int(soc[t]): 1}, lb=critical_kwh)
        lp.constraint({int(zupper[t]): 1, int(soc[t]): -1}, lb=-upper_kwh)
        lp.set_obj(ztarget[t], target_rate * DT_HOURS)
        lp.set_obj(zpreferred[t], (preferred_rate - target_rate) * DT_HOURS)
        lp.set_obj(zcritical[t], (critical_rate - preferred_rate) * DT_HOURS)
        lp.set_obj(zupper[t], upper_rate * DT_HOURS)

    continuation = _continuation_profile(rows_full, cfg, cap, upper_kwh, ed)
    if continuation.get("enabled"):
        target = float(continuation.get("target_kwh") or 0.0)
        ref = float(continuation.get("reference_price_ore_kwh") or 0.0)
        risk = float(continuation.get("risk_premium_ore_kwh") or 0.0)
        zcont = lp.add_vars("continuation_shortfall", 1, 0, cap)[0]
        lp.constraint({int(zcont): 1, int(soc[-1]): 1}, lb=target)
        lp.set_obj(soc[-1], -ref)
        lp.set_obj(zcont, risk)
    else:
        tolerance = cap * float(opt.get("terminal_soc_tolerance_pct", 3.0)) / 100.0
        lo = max(cap * hmin / 100.0, initial_kwh - tolerance)
        hi = min(cap * hmax / 100.0, initial_kwh + tolerance)
        lp.constraint({int(soc[-1]): 1}, lb=lo, ub=hi)
        zterm = lp.add_vars("terminal_soc_delta", 1, 0, cap)[0]
        lp.constraint({int(zterm): 1, int(soc[-1]): -1}, lb=-initial_kwh)
        lp.constraint({int(zterm): 1, int(soc[-1]): 1}, lb=initial_kwh)
        lp.set_obj(zterm, float(opt.get("terminal_soc_tiebreak_ore_per_kwh", 5.0)))

    state = {name: _historical_tariff_state(cfg, name) for name in names}
    templates = {name: _tariff_config(cfg, name) for name in names}
    group_counts: dict[str, int] = {}
    for name, tariff in templates.items():
        groups = _hour_groups(rows, tariff, False)
        group_counts[name] = len(groups)
        historical = [float(x) for x in state[name].get("historical_values_kw") or []]
        rate_ore = float(tariff.get("rate_sek_per_kw", 0.0)) * 100.0
        if tariff.get("kind") == "import_top3_mean":
            theta = lp.add_vars(f"{name}_theta", 1, 0, ilim)[0]
            z = lp.add_vars(f"{name}_excess", len(groups) + len(historical), 0, ilim)
            lp.set_obj(theta, rate_ore)
            if len(z):
                lp.set_obj(z, rate_ore / 3.0)
            for j, group in enumerate(groups):
                coeff = {int(z[j]): 1, int(theta): 1}
                for i in group["indices"]:
                    coeff[int(imp[i])] = coeff.get(int(imp[i]), 0.0) - 0.25
                lp.constraint(coeff, lb=0)
            for j, value in enumerate(historical, start=len(groups)):
                lp.constraint({int(z[j]): 1, int(theta): 1}, lb=value)
        elif tariff.get("kind") == "export_max_hour":
            peak = lp.add_vars(f"{name}_peak", 1, 0, elim)[0]
            lp.set_obj(peak, rate_ore)
            for group in groups:
                coeff = {int(peak): 1}
                for i in group["indices"]:
                    coeff[int(exp[i])] = coeff.get(int(exp[i]), 0.0) - 0.25
                lp.constraint(coeff, lb=0)
            for value in historical:
                lp.constraint({int(peak): 1}, lb=value)
        else:
            raise ValueError(f"Unsupported tariff kind for {name}: {tariff.get('kind')}")

    result = lp.solve(time_limit=30)
    if not result.success or result.x is None:
        raise RuntimeError(f"Tariff-aware planner failed: status={result.status} message={result.message}")

    x = result.x
    out = []
    energy_cost = degradation_cost = hurdle_cost = reserve_cost = upper_cost = 0.0
    battery_export_kwh = discretionary_kwh = 0.0
    for t, row in enumerate(rows):
        c = max(0.0, float(x[charge[t]]))
        d = max(0.0, float(x[discharge[t]]))
        gi = max(0.0, float(x[imp[t]]))
        ge = max(0.0, float(x[exp[t]]))
        dd = max(0.0, float(x[discretionary[t]]))
        action = d - c
        buy = float(row["price_ore_kwh"]) + import_overhead
        sell = max(0.0, float(row["price_ore_kwh"]) - export_overhead)
        energy = (gi * buy - ge * sell) * DT_HOURS
        degradation = (c + d) * degradation_rate * DT_HOURS
        hurdle = dd * margin * DT_HOURS
        reserve_penalty = (
            float(x[ztarget[t]]) * target_rate
            + float(x[zpreferred[t]]) * (preferred_rate - target_rate)
            + float(x[zcritical[t]]) * (critical_rate - preferred_rate)
        ) * DT_HOURS
        upper_penalty = float(x[zupper[t]]) * upper_rate * DT_HOURS
        energy_cost += energy
        degradation_cost += degradation
        hurdle_cost += hurdle
        reserve_cost += reserve_penalty
        upper_cost += upper_penalty

        net = float(row["load_kw"]) - float(row["pv_kw"])
        pv_surplus = max(0.0, float(row["pv_kw"]) - float(row["load_kw"]))
        pv_charge = min(c, pv_surplus)
        grid_charge = max(0.0, c - pv_charge)
        battery_export = min(ge, max(0.0, action - max(0.0, net))) if action > 0 and ge > 0 else 0.0
        required = max(0.0, net - ilim)
        battery_export_kwh += battery_export * DT_HOURS
        discretionary_kwh += dd * DT_HOURS
        res = {
            "pv_charge_kw": pv_charge,
            "grid_charge_kw": grid_charge,
            "grid_import_kw": gi,
            "grid_export_kw": ge,
            "battery_export_kw": battery_export,
        }
        reason, flow = _classify_action(row, action, res)
        out.append({
            **row,
            "battery_action_kw": round(action, 4),
            "expected_soc_pct": round(float(x[soc[t]]) / cap * 100.0, 2),
            "reserve_soc_pct": round(reserve_pct_by_row[t], 2),
            "grid_import_kw": round(gi, 4),
            "grid_export_kw": round(ge, 4),
            "battery_export_kw": round(battery_export, 4),
            "required_physical_discharge_kw": round(required, 4),
            "discretionary_discharge_kw": round(dd, 4),
            "curtailed_kw": 0.0,
            "energy_cost_ore": round(energy, 4),
            "degradation_cost_ore": round(degradation, 4),
            "cash_cost_ore": round(energy + degradation, 4),
            "discretionary_shift_hurdle_cost_ore": round(hurdle, 4),
            "arbitrage_hurdle_cost_ore": round(hurdle, 4),
            "reserve_policy_penalty_ore": round(reserve_penalty, 4),
            "preferred_max_excess_penalty_ore": round(upper_penalty, 4),
            "continuation_policy_adjustment_ore": 0.0,
            "policy_adjustment_ore": round(hurdle + reserve_penalty + upper_penalty, 4),
            "objective_cost_ore": round(energy + degradation + hurdle + reserve_penalty + upper_penalty, 4),
            "reason": reason,
            "flow_breakdown_kw": {k: round(v, 4) for k, v in flow.items()},
        })

    tariff_eval = {
        name: _evaluate(out, templates[name], [float(x) for x in state[name].get("historical_values_kw") or []])
        for name in names
    }
    tariff_cost_ore = sum(float(v.get("cost_sek") or 0.0) * 100.0 for v in tariff_eval.values())
    baseline_tariffs = {
        name: _evaluate(
            [r for r in (base.get("rows") or []) if r.get("price_known")][:n],
            templates[name],
            [float(x) for x in state[name].get("historical_values_kw") or []],
        )
        for name in names
    }
    baseline_tariff_cost_ore = sum(float(v.get("cost_sek") or 0.0) * 100.0 for v in baseline_tariffs.values())

    known_cash = energy_cost + degradation_cost + tariff_cost_ore
    baseline_known_cash = _baseline_known_cash(base) + baseline_tariff_cost_ore
    terminal_kwh = float(x[soc[-1]])
    optimized_asset = baseline_asset = 0.0
    if continuation.get("enabled"):
        ref = float(continuation.get("reference_price_ore_kwh") or 0.0)
        risk = float(continuation.get("risk_premium_ore_kwh") or 0.0)
        target = float(continuation.get("target_kwh") or 0.0)
        optimized_asset = terminal_kwh * ref + min(terminal_kwh, target) * risk
        base_boundary_soc = (base.get("continuation") or {}).get("price_boundary_soc_pct")
        base_boundary_kwh = cap * float(base_boundary_soc) / 100.0 if base_boundary_soc is not None else cap * float(base.get("summary", {}).get("terminal_soc_pct", initial)) / 100.0
        baseline_asset = base_boundary_kwh * ref + min(base_boundary_kwh, target) * risk

    cash_saving = baseline_known_cash - known_cash
    economic_saving = cash_saving + optimized_asset - baseline_asset
    diag = horizon_diagnostics(cfg)
    terminal_pct = terminal_kwh / cap * 100.0

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "planner": PLANNER_NAME,
        "base_planner": BASE_PLANNER_NAME,
        "mode": "shadow_read_only",
        "interval_minutes": 15,
        "horizon_hours": round(n * DT_HOURS, 2),
        "initial_soc_pct": round(float(initial_measured), 2),
        "planning_initial_soc_pct": round(initial, 2),
        "horizon_diagnostics": diag,
        "constraints": {
            "battery_capacity_kwh": cap,
            "hard_min_soc_pct": hmin,
            "hard_max_soc_pct": hmax,
            "preferred_min_soc_pct": pmin,
            "preferred_max_soc_pct": pmax,
            "battery_max_charge_kw": cmax,
            "battery_max_discharge_kw": dmax,
            "physical_grid_import_limit_kw": ilim,
            "grid_export_limit_kw": elim,
            "charge_efficiency": ec,
            "discharge_efficiency": ed,
            "tariff_measurement_uses_clock_hour_average": True,
        },
        "objective": {
            "energy_cost_on_published_prices_only": True,
            "battery_degradation_cost": True,
            "dynamic_uncertainty_reserve": True,
            "piecewise_marginal_reserve_penalty": True,
            "discretionary_self_consumption_hurdle": True,
            "physical_limit_discharge_exempt_from_margin": True,
            "continuation_value_from_physical_forecast": True,
            "tariff_month_to_date_state": True,
            "tariff_costs": names,
            "tariff_price_horizon_mode": "contiguous_published_intervals_only",
        },
        "tariffs": {
            "active": names,
            "month_to_date": state,
            "optimized": tariff_eval,
            "v3_5_counterfactual": baseline_tariffs,
            "forecast_active_clock_hours": group_counts,
        },
        "continuation": {
            "enabled": bool(continuation.get("enabled")),
            "price_boundary_soc_pct": round(terminal_pct, 2),
            "target_soc_pct": round(float(continuation["target_soc_pct"]), 2) if continuation.get("target_soc_pct") is not None else None,
            "value_ore_per_kwh": round(float(continuation["value_ore_per_kwh"]), 2) if continuation.get("value_ore_per_kwh") is not None else None,
            "reference_price_ore_kwh": round(float(continuation["reference_price_ore_kwh"]), 2) if continuation.get("reference_price_ore_kwh") is not None else None,
            "risk_premium_ore_kwh": round(float(continuation.get("risk_premium_ore_kwh") or 0.0), 2),
            "unknown_net_deficit_kwh": round(float(continuation.get("unknown_net_deficit_kwh") or 0.0), 3),
            "unknown_peak_support_kwh": round(float(continuation.get("unknown_peak_support_kwh") or 0.0), 3),
            "energy_coverage_fraction": round(float(continuation.get("coverage_fraction") or 0.0), 3),
        },
        "summary": {
            "objective_cost_ore": round(known_cash + hurdle_cost + reserve_cost + upper_cost - optimized_asset, 2),
            "expected_cash_cost_ore": round(known_cash, 2),
            "baseline_cash_cost_ore": round(baseline_known_cash, 2),
            "expected_cash_saving_ore": round(cash_saving, 2),
            "expected_cash_saving_sek": round(cash_saving / 100.0, 2),
            "optimized_continuation_asset_value_ore": round(optimized_asset, 2),
            "baseline_continuation_asset_value_ore": round(baseline_asset, 2),
            "expected_saving_ore": round(economic_saving, 2),
            "expected_saving_sek": round(economic_saving / 100.0, 2),
            "expected_saving_scope": "published_prices_plus_tariffs_plus_continuation_asset_value",
            "cash_cost_scope": "published_price_intervals_plus_battery_degradation_plus_tariff",
            "priced_horizon_hours": round(n * DT_HOURS, 2),
            "unpriced_horizon_hours": diag.get("unknown_price_horizon_hours"),
            "terminal_soc_pct": round(terminal_pct, 2),
            "terminal_soc_delta_pct": round(terminal_pct - float(initial_measured), 2),
            "battery_export_kwh": round(battery_export_kwh, 3),
            "discretionary_discharge_kwh": round(discretionary_kwh, 3),
            "discretionary_shift_hurdle_cost_ore": round(hurdle_cost, 2),
            "reserve_policy_penalty_ore": round(reserve_cost, 2),
            "preferred_max_excess_penalty_ore": round(upper_cost, 2),
            "tariff_cost_ore": round(tariff_cost_ore, 2),
            "baseline_tariff_cost_ore": round(baseline_tariff_cost_ore, 2),
        },
        "rows": out,
    }


def build_shadow_plan(cfg: dict[str, Any]) -> dict[str, Any]:
    names = active_tariff_names(cfg)
    if not names:
        return build_v35_plan(cfg)
    return build_tariff_plan(cfg, names)
