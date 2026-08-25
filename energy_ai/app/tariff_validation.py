from __future__ import annotations

import copy
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from .monthly_replay import _dataset_month, _join, _solve as _solve_consumption_month, _ts, market_month
from .optimizer import _build_horizon, _latest_soc_pct, build_plan
from .tariff_scenarios import DT_HOURS, _LP, _hour_groups, _tariff_metric_from_hourly, _template

ENGINE_NAME = "tariff_validation_v1"


async def _month_rows(month: str, refresh_market: bool = False):
    data, dd = _dataset_month(month)
    if dd["coverage_fraction"] < 0.90:
        raise RuntimeError(f"Historical load/PV coverage for {month} is only {dd['coverage_fraction']:.1%}; require at least 90%")
    market, md = await market_month(month, refresh_market)
    rows, jd = _join(data, market)
    if jd["join_fraction_of_dataset"] < 0.90:
        raise RuntimeError(f"Historical market-data join for {month} is only {jd['join_fraction_of_dataset']:.1%}; require at least 90%")
    contiguous = [rows[0]] if rows else []
    for row in rows[1:]:
        if _ts(row["start"]) - _ts(contiguous[-1]["start"]) != timedelta(minutes=15):
            break
        contiguous.append(row)
    if len(contiguous) / max(1, len(rows)) < 0.95:
        raise RuntimeError("Monthly replay has a material internal timestamp gap; refusing to optimize across missing history")
    return contiguous, {"training": dd, "market": md, "join": jd, "optimized_contiguous_intervals": len(contiguous)}


def _evaluate(rows: list[dict[str, Any]], tariff: dict[str, Any], force_window: bool = False) -> dict[str, Any]:
    groups = _hour_groups(rows, tariff, force_window)
    key = "grid_import_kw" if tariff["kind"] == "import_top3_mean" else "grid_export_kw"
    hourly, details = [], []
    for group in groups:
        value = sum(float(rows[i].get(key) or 0.0) for i in group["indices"]) / 4.0
        hourly.append(value)
        details.append({"date": group["date"], "hour": group["hour"], "kw": round(value, 4)})
    metric = _tariff_metric_from_hourly(hourly, tariff, [])
    return {
        **metric,
        "active_hour_count": len(hourly),
        "max_hour_kw": round(max(hourly, default=0.0), 4),
        "top_hours": sorted(details, key=lambda x: x["kw"], reverse=True)[:10],
        "force_window": force_window,
    }


def _solve_generic(
    rows: list[dict[str, Any]],
    cfg: dict[str, Any],
    *,
    tariff_names: list[str],
    initial_soc_pct: float = 50.0,
    force_window: bool = False,
) -> dict[str, Any]:
    if not rows:
        raise RuntimeError("No replay rows")

    battery = (cfg.get("policy") or {}).get("battery") or {}
    econ = (cfg.get("policy") or {}).get("economics") or {}
    opt = cfg.get("optimizer") or {}

    cap = float(battery.get("capacity_kwh", 19.6))
    hmin = float(battery.get("hard_min_soc_pct", 5.0))
    hmax = float(battery.get("hard_max_soc_pct", 100.0))
    pmin = float(battery.get("preferred_min_soc_pct", 15.0))
    pmax = float(battery.get("preferred_max_soc_pct", 90.0))
    reserve_pct = float(battery.get("normal_reserve_soc_pct", 20.0))
    initial = max(hmin, min(hmax, float(initial_soc_pct)))
    e0 = cap * initial / 100.0

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

    critical = max(hmin, min(pmin, float(opt.get("reserve_critical_soc_pct", 10.0))))
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
    discretionary = lp.add_vars("disc_discharge", n, 0, dmax)
    ztarget = lp.add_vars("z_target", n, 0, cap)
    zpreferred = lp.add_vars("z_preferred", n, 0, cap)
    zcritical = lp.add_vars("z_critical", n, 0, cap)
    zupper = lp.add_vars("z_upper", n, 0, cap)

    negative = [i for i, r in enumerate(rows) if float(r["price_ore_kwh"]) + import_overhead < 0]
    ybat = lp.add_vars("ybat_negative", len(negative), 0, 1, integral=True) if negative else np.array([], dtype=int)
    ygrid = lp.add_vars("ygrid_negative", len(negative), 0, 1, integral=True) if negative else np.array([], dtype=int)
    negpos = {t: j for j, t in enumerate(negative)}

    reserve_kwh = cap * reserve_pct / 100.0
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

        required = max(0.0, net - ilim)
        lp.constraint({int(discretionary[t]): 1, int(discharge[t]): -1}, lb=-required)

        if t in negpos:
            j = negpos[t]
            lp.constraint({int(charge[t]): 1, int(ybat[j]): -cmax}, ub=0)
            lp.constraint({int(discharge[t]): 1, int(ybat[j]): dmax}, ub=dmax)
            lp.constraint({int(imp[t]): 1, int(ygrid[j]): -ilim}, ub=0)
            lp.constraint({int(exp[t]): 1, int(ygrid[j]): elim}, ub=elim)

        buy = float(row["price_ore_kwh"]) + import_overhead
        sell = max(0.0, float(row["price_ore_kwh"]) - export_overhead)
        lp.set_obj(imp[t], (buy + 0.001) * DT_HOURS)
        lp.set_obj(exp[t], (-sell + 0.001) * DT_HOURS)
        lp.set_obj(charge[t], degradation_rate * DT_HOURS)
        lp.set_obj(discharge[t], degradation_rate * DT_HOURS)
        lp.set_obj(discretionary[t], margin * DT_HOURS)

        lp.constraint({int(ztarget[t]): 1, int(soc[t]): 1}, lb=reserve_kwh)
        lp.constraint({int(zpreferred[t]): 1, int(soc[t]): 1}, lb=preferred_kwh)
        lp.constraint({int(zcritical[t]): 1, int(soc[t]): 1}, lb=critical_kwh)
        lp.constraint({int(zupper[t]): 1, int(soc[t]): -1}, lb=-upper_kwh)
        lp.set_obj(ztarget[t], target_rate * DT_HOURS)
        lp.set_obj(zpreferred[t], (preferred_rate - target_rate) * DT_HOURS)
        lp.set_obj(zcritical[t], (critical_rate - preferred_rate) * DT_HOURS)
        lp.set_obj(zupper[t], upper_rate * DT_HOURS)

    lp.constraint({int(soc[-1]): 1}, lb=e0, ub=e0)

    templates = {name: _template(cfg, name) for name in tariff_names}
    group_counts: dict[str, int] = {}
    for name, tariff in templates.items():
        groups = _hour_groups(rows, tariff, force_window)
        group_counts[name] = len(groups)
        rate_ore = float(tariff["rate_sek_per_kw"]) * 100.0
        if tariff["kind"] == "import_top3_mean":
            theta = lp.add_vars(f"{name}_theta", 1, 0, ilim)[0]
            z = lp.add_vars(f"{name}_excess", len(groups), 0, ilim)
            lp.set_obj(theta, rate_ore)
            if len(z):
                lp.set_obj(z, rate_ore / 3.0)
            for j, group in enumerate(groups):
                coeff = {int(z[j]): 1, int(theta): 1}
                for i in group["indices"]:
                    coeff[int(imp[i])] = coeff.get(int(imp[i]), 0.0) - 0.25
                lp.constraint(coeff, lb=0)
        elif tariff["kind"] == "export_max_hour":
            peak = lp.add_vars(f"{name}_peak", 1, 0, elim)[0]
            lp.set_obj(peak, rate_ore)
            for group in groups:
                coeff = {int(peak): 1}
                for i in group["indices"]:
                    coeff[int(exp[i])] = coeff.get(int(exp[i]), 0.0) - 0.25
                lp.constraint(coeff, lb=0)
        else:
            raise ValueError(tariff["kind"])

    res = lp.solve(time_limit=90)
    if not res.success or res.x is None:
        return {"status": "infeasible_or_timeout", "solver_status": int(res.status), "solver_message": str(res.message)}

    x = res.x
    out = []
    energy = degradation = hurdle = reserve = upper = 0.0
    for t, row in enumerate(rows):
        c = max(0.0, float(x[charge[t]]))
        d = max(0.0, float(x[discharge[t]]))
        gi = max(0.0, float(x[imp[t]]))
        ge = max(0.0, float(x[exp[t]]))
        dd = max(0.0, float(x[discretionary[t]]))
        buy = float(row["price_ore_kwh"]) + import_overhead
        sell = max(0.0, float(row["price_ore_kwh"]) - export_overhead)
        energy += (gi * buy - ge * sell) * DT_HOURS
        degradation += (c + d) * degradation_rate * DT_HOURS
        hurdle += dd * margin * DT_HOURS
        reserve += (
            float(x[ztarget[t]]) * target_rate
            + float(x[zpreferred[t]]) * (preferred_rate - target_rate)
            + float(x[zcritical[t]]) * (critical_rate - preferred_rate)
        ) * DT_HOURS
        upper += float(x[zupper[t]]) * upper_rate * DT_HOURS
        out.append({
            "start": row["start"],
            "grid_import_kw": gi,
            "grid_export_kw": ge,
            "charge_kw": c,
            "discharge_kw": d,
            "soc_pct": float(x[soc[t]]) / cap * 100.0,
        })

    tariff_eval = {name: _evaluate(out, tariff, force_window) for name, tariff in templates.items()}
    tariff_cost_ore = sum(float(v["cost_sek"]) * 100.0 for v in tariff_eval.values())
    cash = energy + degradation + tariff_cost_ore
    objective = cash + hurdle + reserve + upper
    return {
        "status": "optimal" if res.status == 0 else "feasible",
        "solver_status": int(res.status),
        "solver_message": str(res.message),
        "initial_soc_pct": initial,
        "terminal_soc_pct": round(float(x[soc[-1]]) / cap * 100.0, 3),
        "tariffs": tariff_eval,
        "economics": {
            "energy_cost_ore": round(energy, 2),
            "battery_degradation_cost_ore": round(degradation, 2),
            "tariff_cost_ore": round(tariff_cost_ore, 2),
            "cash_plus_tariff_ore": round(cash, 2),
            "cash_plus_tariff_sek": round(cash / 100.0, 2),
            "discretionary_shift_hurdle_ore": round(hurdle, 2),
            "reserve_policy_penalty_ore": round(reserve, 2),
            "preferred_max_excess_penalty_ore": round(upper, 2),
            "objective_cost_ore": round(objective, 2),
        },
        "diagnostics": {
            "intervals": n,
            "negative_price_intervals": len(negative),
            "active_tariff_clock_hours": group_counts,
        },
        "_rows": out,
    }


def _public_solution(solution: dict[str, Any]) -> dict[str, Any]:
    clean = dict(solution)
    clean.pop("_rows", None)
    return clean


async def minimum_feasible_import_cap(
    cfg: dict[str, Any],
    month: str,
    *,
    refresh_market: bool = False,
    initial_soc_pct: float = 50.0,
    precision_kw: float = 0.01,
    max_cap_kw: float = 2.0,
) -> dict[str, Any]:
    rows, data = await _month_rows(month, refresh_market)
    precision = max(0.001, min(0.25, float(precision_kw)))
    upper = max(precision, float(max_cap_kw))

    zero = _solve_consumption_month(rows, cfg, tariff_enabled=False, hourly_cap_kw=0.0, initial_soc_pct=initial_soc_pct)
    if zero.get("status") in {"optimal", "feasible"}:
        physical_min = 0.0
        iterations = 0
    else:
        high_result = _solve_consumption_month(rows, cfg, tariff_enabled=False, hourly_cap_kw=upper, initial_soc_pct=initial_soc_pct)
        while high_result.get("status") not in {"optimal", "feasible"} and upper < 13.8:
            upper = min(13.8, upper * 2.0)
            high_result = _solve_consumption_month(rows, cfg, tariff_enabled=False, hourly_cap_kw=upper, initial_soc_pct=initial_soc_pct)
        if high_result.get("status") not in {"optimal", "feasible"}:
            raise RuntimeError("No feasible hourly import cap found up to the physical import limit")
        low = 0.0
        iterations = 0
        while upper - low > precision and iterations < 20:
            mid = (low + upper) / 2.0
            trial = _solve_consumption_month(rows, cfg, tariff_enabled=False, hourly_cap_kw=mid, initial_soc_pct=initial_soc_pct)
            if trial.get("status") in {"optimal", "feasible"}:
                upper = mid
            else:
                low = mid
            iterations += 1
        physical_min = upper

    economic = _solve_consumption_month(rows, cfg, tariff_enabled=True, initial_soc_pct=initial_soc_pct)
    economic_metric = float((economic.get("tariff") or {}).get("metric_kw") or 0.0)
    return {
        "engine": ENGINE_NAME,
        "test": "minimum_feasible_import_cap",
        "month": month,
        "test_only": True,
        "data": data,
        "precision_kw": precision,
        "minimum_feasible_hourly_cap_kw": round(physical_min, 4),
        "economic_optimum_top3_kw": round(economic_metric, 4),
        "economic_minus_physical_kw": round(economic_metric - physical_min, 4),
        "iterations": iterations,
        "interpretation": "Near-zero difference means the monthly economic optimum is effectively set by battery/load physics rather than a chosen soft target.",
    }


async def production_month_replay(
    cfg: dict[str, Any],
    month: str,
    *,
    refresh_market: bool = False,
    initial_soc_pct: float = 50.0,
) -> dict[str, Any]:
    rows, data = await _month_rows(month, refresh_market)
    base = _solve_generic(rows, cfg, tariff_names=[], initial_soc_pct=initial_soc_pct)
    optimal = _solve_generic(rows, cfg, tariff_names=["production_demand"], initial_soc_pct=initial_soc_pct)
    tariff = _template(cfg, "production_demand")
    base_eval = _evaluate(base["_rows"], tariff, False)
    optimal_eval = (optimal.get("tariffs") or {}).get("production_demand") or {}
    base_public = _public_solution(base)
    base_public["production_tariff_evaluation"] = base_eval
    base_energy_deg_ore = float((base.get("economics") or {}).get("energy_cost_ore") or 0.0) + float((base.get("economics") or {}).get("battery_degradation_cost_ore") or 0.0)
    base_total_ore = base_energy_deg_ore + float(base_eval.get("cost_sek") or 0.0) * 100.0
    optimal_total_ore = float((optimal.get("economics") or {}).get("cash_plus_tariff_ore") or 0.0)
    base_public["economics_with_production_tariff"] = {
        "energy_plus_degradation_ore": round(base_energy_deg_ore, 2),
        "production_tariff_cost_ore": round(float(base_eval.get("cost_sek") or 0.0) * 100.0, 2),
        "cash_plus_tariff_ore": round(base_total_ore, 2),
        "cash_plus_tariff_sek": round(base_total_ore / 100.0, 2),
    }
    return {
        "engine": ENGINE_NAME,
        "test": "production_month_replay",
        "month": month,
        "test_only": True,
        "tariff_source_status": tariff.get("source_status"),
        "data": data,
        "base": base_public,
        "tariff_optimal": _public_solution(optimal),
        "comparison": {
            "export_peak_reduction_kw": round(float(base_eval.get("metric_kw") or 0.0) - float(optimal_eval.get("metric_kw") or 0.0), 4),
            "tariff_cost_reduction_sek": round(float(base_eval.get("cost_sek") or 0.0) - float(optimal_eval.get("cost_sek") or 0.0), 2),
            "net_cash_saving_sek": round((base_total_ore - optimal_total_ore) / 100.0, 2),
        },
    }


def _canonical_plan(plan: dict[str, Any]) -> dict[str, Any]:
    row_keys = ("start", "battery_action_kw", "expected_soc_pct", "grid_import_kw", "grid_export_kw", "reason", "objective_cost_ore")
    return {
        "planner": plan.get("planner"),
        "initial_soc_pct": plan.get("initial_soc_pct"),
        "summary": plan.get("summary"),
        "rows": [{k: row.get(k) for k in row_keys} for row in (plan.get("rows") or [])],
    }


def disabled_tariff_regression(cfg: dict[str, Any]) -> dict[str, Any]:
    original = build_plan(cfg)
    absent_cfg = copy.deepcopy(cfg)
    absent_cfg.pop("tariffs", None)
    disabled_cfg = copy.deepcopy(cfg)
    disabled_cfg["tariffs"] = {
        "consumption_demand": {"enabled": False},
        "production_demand": {"enabled": False},
    }
    absent = build_plan(absent_cfg)
    disabled = build_plan(disabled_cfg)
    a, b, c = _canonical_plan(original), _canonical_plan(absent), _canonical_plan(disabled)
    return {
        "engine": ENGINE_NAME,
        "test": "disabled_tariff_v3_5_regression",
        "test_only": True,
        "planner": original.get("planner"),
        "tariffs_absent_exact_match": a == b,
        "tariffs_disabled_exact_match": a == c,
        "pass": a == b == c,
        "compared_rows": len(a["rows"]),
    }


def combined_live_test(cfg: dict[str, Any], initial_soc_pct: float | None = None) -> dict[str, Any]:
    rows_full = _build_horizon(cfg)
    rows = []
    for row in rows_full:
        if not row.get("price_known"):
            break
        rows.append({
            "start": row["start"],
            "load_kw": row["load_kw"],
            "pv_kw": row["pv_kw"],
            "price_ore_kwh": row["price_ore_kwh"],
        })
    if not rows:
        raise RuntimeError("No contiguous published-price intervals for combined tariff test")
    initial = float(initial_soc_pct if initial_soc_pct is not None else (_latest_soc_pct() or 5.0))
    base = _solve_generic(rows, cfg, tariff_names=[], initial_soc_pct=initial, force_window=True)
    combined = _solve_generic(rows, cfg, tariff_names=["consumption_demand", "production_demand"], initial_soc_pct=initial, force_window=True)
    consumption = _template(cfg, "consumption_demand")
    production = _template(cfg, "production_demand")
    base_tariffs = {
        "consumption_demand": _evaluate(base["_rows"], consumption, True),
        "production_demand": _evaluate(base["_rows"], production, True),
    }
    base_tariff_cost_ore = sum(float(v.get("cost_sek") or 0.0) * 100.0 for v in base_tariffs.values())
    base_energy_deg_ore = float((base.get("economics") or {}).get("energy_cost_ore") or 0.0) + float((base.get("economics") or {}).get("battery_degradation_cost_ore") or 0.0)
    combined_tariffs = combined.get("tariffs") or {}
    return {
        "engine": ENGINE_NAME,
        "test": "combined_live_counterfactual",
        "test_only": True,
        "force_window": True,
        "known_price_intervals": len(rows),
        "initial_soc_pct": initial,
        "base": {
            **_public_solution(base),
            "tariffs": base_tariffs,
            "cash_plus_both_tariffs_ore": round(base_energy_deg_ore + base_tariff_cost_ore, 2),
        },
        "combined": _public_solution(combined),
        "comparison": {
            "consumption_metric_reduction_kw": round(float(base_tariffs["consumption_demand"].get("metric_kw") or 0.0) - float((combined_tariffs.get("consumption_demand") or {}).get("metric_kw") or 0.0), 4),
            "production_metric_reduction_kw": round(float(base_tariffs["production_demand"].get("metric_kw") or 0.0) - float((combined_tariffs.get("production_demand") or {}).get("metric_kw") or 0.0), 4),
            "net_cash_saving_ore": round((base_energy_deg_ore + base_tariff_cost_ore) - float((combined.get("economics") or {}).get("cash_plus_tariff_ore") or 0.0), 2),
        },
        "pass": combined.get("status") in {"optimal", "feasible"},
        "note": "Both tariff clock windows are forced active for a counterfactual simultaneous-objective test; normal calendar eligibility is intentionally ignored.",
    }
