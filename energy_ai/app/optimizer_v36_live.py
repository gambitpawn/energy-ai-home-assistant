from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

from . import optimizer as v35
from .db import DB_PATH
from .price_economics import effective_prices

PLANNER_NAME = "deterministic_battery_dp_v3_6_live"
BASELINE_PLANNER = "deterministic_battery_dp_v3_5"
NOMINAL_INTERVAL_HOURS = 0.25


def _parse_ts(value: str) -> datetime:
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def latest_soc_observation(now: datetime | None = None) -> dict[str, Any] | None:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute(
            "SELECT collected_at,payload_json FROM raw_state ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    collected_at, payload_json = row
    try:
        payload = json.loads(payload_json)
        item = payload.get("battery_soc_pct") or {}
        if not item.get("available"):
            return None
        soc = float(item.get("state"))
        observed_at = _parse_ts(str(collected_at))
    except Exception:
        return None
    return {
        "soc_pct": soc,
        "observed_at": observed_at.isoformat(),
        "age_seconds": max(0.0, (now - observed_at).total_seconds()),
    }


def _live_rows(base_rows: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Trim elapsed time and preserve the remaining part of the current quarter.

    v3.5 remains quarter-based. The live planner is only used for an intra-quarter
    state correction, so the first row may be shorter than 15 minutes. Later rows
    retain the original quarter boundaries.
    """
    now = now.astimezone(timezone.utc)
    rows: list[dict[str, Any]] = []
    for source in base_rows:
        source_start = _parse_ts(str(source["start"]))
        source_end = source_start + timedelta(minutes=15)
        if source_end <= now:
            continue
        row = dict(source)
        if source_start < now < source_end:
            row["source_start"] = source_start.isoformat()
            row["start"] = now.isoformat()
            row["partial_interval"] = True
            duration = (source_end - now).total_seconds() / 3600.0
        else:
            row["partial_interval"] = False
            duration = NOMINAL_INTERVAL_HOURS
        row["end"] = source_end.isoformat()
        row["duration_hours"] = max(1.0 / 3600.0, float(duration))
        row["duration_minutes"] = row["duration_hours"] * 60.0
        rows.append(row)
    return rows


def _transition_action_kw(e0: float, e1: float, ec: float, ed: float, dt_hours: float) -> float:
    d = e1 - e0
    dt = max(1e-9, float(dt_hours))
    if d > 0:
        return -(d / ec) / dt
    if d < 0:
        return ((-d) * ed) / dt
    return 0.0


def _reserve_policy_penalty_ore(
    energy_kwh: float,
    reserve_kwh: float,
    cfg: dict[str, Any],
    cap: float,
    hard_min_soc_pct: float,
    preferred_min_soc_pct: float,
    dt_hours: float,
) -> float:
    o = cfg.get("optimizer") or {}
    preferred_pct = max(hard_min_soc_pct, preferred_min_soc_pct)
    critical_pct = min(
        preferred_pct,
        max(hard_min_soc_pct, float(o.get("reserve_critical_soc_pct", 10.0))),
    )
    hard_min_kwh = cap * hard_min_soc_pct / 100.0
    critical_kwh = cap * critical_pct / 100.0
    preferred_kwh = cap * preferred_pct / 100.0
    target_kwh = max(hard_min_kwh, reserve_kwh)

    critical_hi = min(critical_kwh, target_kwh)
    preferred_hi = min(preferred_kwh, target_kwh)
    critical_missing = v35._zone_shortfall_kwh(energy_kwh, hard_min_kwh, critical_hi)
    preferred_missing = v35._zone_shortfall_kwh(energy_kwh, critical_kwh, preferred_hi)
    target_missing = v35._zone_shortfall_kwh(energy_kwh, preferred_kwh, target_kwh)

    critical_rate = max(0.0, float(o.get("reserve_critical_penalty_ore_per_kwh_hour", 300.0)))
    preferred_rate = max(0.0, float(o.get("reserve_preferred_penalty_ore_per_kwh_hour", 100.0)))
    target_rate = max(0.0, float(o.get("reserve_target_penalty_ore_per_kwh_hour", 10.0)))
    return (
        critical_missing * critical_rate
        + preferred_missing * preferred_rate
        + target_missing * target_rate
    ) * dt_hours


def _continuation_profile(
    rows: list[dict[str, Any]],
    cfg: dict[str, Any],
    cap: float,
    preferred_max_kwh: float,
    eta_discharge: float,
) -> dict[str, Any]:
    o = cfg.get("optimizer") or {}
    unknown = [r for r in rows if not r["price_known"]]
    known = [r for r in rows if r["price_known"]]
    if not unknown:
        return {
            "enabled": False,
            "target_kwh": None,
            "target_soc_pct": None,
            "value_ore_per_kwh": None,
            "unknown_net_deficit_kwh": 0.0,
            "unknown_peak_support_kwh": 0.0,
            "reference_price_ore_kwh": None,
        }
    lim = float(o.get("physical_grid_import_limit_kw", 13.8))
    frac = max(0.0, min(1.0, float(o.get("unknown_price_energy_coverage_fraction", 0.35))))
    riskmax = max(0.0, float(o.get("unknown_price_risk_premium_ore_kwh", 40.0)))
    default = max(0.0, float(o.get("unknown_price_default_continuation_value_ore_kwh", 150.0)))
    scale = max(0.01, float(o.get("reserve_uncertainty_full_scale_kw", 3.0)))
    reserve = max((v35._dynamic_reserve_kwh(r, cfg, cap)[0] for r in unknown), default=0.0)
    deficit = sum(
        max(0.0, float(r["load_kw"]) - float(r["pv_kw"])) * float(r["duration_hours"])
        for r in unknown
    )
    peak = sum(
        max(0.0, float(r["load_kw"]) - float(r["pv_kw"]) - lim)
        * float(r["duration_hours"])
        / max(0.01, eta_discharge)
        for r in unknown
    )
    covered = deficit * frac / max(0.01, eta_discharge)
    target = min(preferred_max_kwh, max(reserve + covered, reserve + peak))
    buys = [
        effective_prices(float(r["price_ore_kwh"]), cfg)["effective_import_price_ore_kwh"]
        for r in known
        if r.get("price_ore_kwh") is not None
    ]
    ref = float(median(buys)) if buys else default
    avgunc = sum(
        max(0.0, float(r.get("load_uncertainty_kw") or 0.0))
        + max(0.0, float(r.get("pv_uncertainty_kw") or 0.0))
        for r in unknown
    ) / len(unknown)
    risk = riskmax * (
        0.6 * min(1.0, deficit / max(0.01, cap))
        + 0.4 * min(1.0, avgunc / scale)
    )
    return {
        "enabled": True,
        "target_kwh": target,
        "target_soc_pct": target / cap * 100.0,
        "value_ore_per_kwh": ref + risk,
        "unknown_net_deficit_kwh": deficit,
        "unknown_peak_support_kwh": peak,
        "reference_price_ore_kwh": ref,
        "risk_premium_ore_kwh": risk,
        "coverage_fraction": frac,
        "price_semantics": "current_economics",
    }


def _interval_result(row: dict[str, Any], action: float, cfg: dict[str, Any]) -> dict[str, float | None]:
    o = cfg.get("optimizer") or {}
    e = (cfg.get("policy") or {}).get("economics") or {}
    dt = float(row["duration_hours"])
    ilim = float(o.get("physical_grid_import_limit_kw", 13.8))
    elim = float(o.get("grid_export_limit_kw", 10.0))
    load, pv = float(row["load_kw"]), float(row["pv_kw"])
    net = load - pv
    grid = net - action
    imp, rawexp = max(0.0, grid), max(0.0, -grid)
    exp, curt = min(rawexp, elim), max(0.0, rawexp - elim)
    pv_surplus, charge = max(0.0, pv - load), max(0.0, -action)
    pv_charge = min(charge, pv_surplus)
    grid_charge = max(0.0, charge - pv_charge)
    batt_export = min(exp, max(0.0, action - max(0.0, net))) if action > 0 and exp > 0 else 0.0
    required = max(0.0, net - ilim)
    discretionary = max(0.0, action - required) if action > 0 else 0.0
    feasible = imp <= ilim + 1e-9
    degr = abs(action) * dt * float(o.get("battery_degradation_ore_kwh", 5.0))
    buy = sell = None
    if not row["price_known"]:
        if grid_charge > 1e-6 or batt_export > 1e-6 or (required <= 1e-6 and action > 1e-6):
            feasible = False
        energy = hurdle = 0.0
    else:
        prices = effective_prices(float(row["price_ore_kwh"]), cfg)
        buy = float(prices["effective_import_price_ore_kwh"])
        sell = float(prices["effective_export_price_ore_kwh"])
        energy = imp * dt * buy - exp * dt * sell
        hurdle = discretionary * dt * float(e.get("minimum_arbitrage_margin_ore_kwh", 20.0))
    cash = energy + degr
    return {
        "feasible": 1.0 if feasible else 0.0,
        "grid_import_kw": imp,
        "grid_export_kw": exp,
        "battery_export_kw": batt_export,
        "pv_charge_kw": pv_charge,
        "grid_charge_kw": grid_charge,
        "required_physical_discharge_kw": required,
        "discretionary_discharge_kw": discretionary,
        "curtailed_kw": curt,
        "effective_import_price_ore_kwh": buy,
        "effective_export_price_ore_kwh": sell,
        "energy_cost_ore": energy,
        "degradation_cost_ore": degr,
        "cash_cost_ore": cash,
        "discretionary_shift_hurdle_cost_ore": hurdle,
        "arbitrage_hurdle_cost_ore": hurdle,
        "interval_cost_ore": cash + hurdle,
    }


def _baseline_cost(rows: list[dict[str, Any]], cfg: dict[str, Any]) -> float:
    return sum(float(_interval_result(r, 0.0, cfg)["cash_cost_ore"] or 0.0) for r in rows)


def build_live_plan(
    cfg: dict[str, Any],
    *,
    now: datetime | None = None,
    replan_reason: str = "soc_deviation",
) -> dict[str, Any]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    rows = _live_rows(v35._build_horizon(cfg), now)
    if not rows:
        raise RuntimeError("No live optimizer horizon remains after current time")

    o = cfg.get("optimizer") or {}
    b = (cfg.get("policy") or {}).get("battery") or {}
    cap = float(b.get("capacity_kwh", 19.6))
    hmin, hmax = float(b.get("hard_min_soc_pct", 5.0)), float(b.get("hard_max_soc_pct", 100.0))
    pmin, pmax = float(b.get("preferred_min_soc_pct", 15.0)), float(b.get("preferred_max_soc_pct", 90.0))
    normal = float(b.get("normal_reserve_soc_pct", 20.0))
    high = float(b.get("high_uncertainty_reserve_soc_pct", 28.0))
    cmax = float(o.get("battery_max_charge_kw", 8.0))
    dmax = float(o.get("battery_max_discharge_kw", 8.0))
    ec = float(o.get("battery_charge_efficiency", 0.95))
    ed = float(o.get("battery_discharge_efficiency", 0.95))
    reqstep = float(o.get("soc_grid_step_kwh", 0.5))
    termtol = float(o.get("terminal_soc_tolerance_pct", 3.0))
    termtie = float(o.get("terminal_soc_tiebreak_ore_per_kwh", 5.0))

    obs = latest_soc_observation(now)
    if obs is None:
        raise RuntimeError("Current battery SOC is unavailable")
    max_age = float(o.get("soc_observation_max_age_seconds", 180.0))
    if float(obs["age_seconds"]) > max_age:
        raise RuntimeError(
            f"Current battery SOC is stale: {obs['age_seconds']:.1f}s > {max_age:.1f}s"
        )
    soc = float(obs["soc_pct"])
    initial = cap * soc / 100.0
    mink, maxk = cap * hmin / 100.0, cap * hmax / 100.0
    pmaxk = cap * pmax / 100.0
    states, effstep = v35._state_grid(mink, maxk, reqstep, initial)
    init_idx = min(range(len(states)), key=lambda i: abs(states[i] - initial))

    continuation = _continuation_profile(rows, cfg, cap, pmaxk, ed)
    known_n = sum(1 for r in rows if r["price_known"])
    boundary = known_n - 1 if 0 < known_n < len(rows) else None
    costs: dict[int, float] = {init_idx: 0.0}
    parents: list[dict[int, tuple[Any, ...]]] = []
    excess_rate = max(0.0, float(o.get("preferred_max_excess_penalty_ore_per_kwh_hour", 2.0)))

    for t, row in enumerate(rows):
        dt = float(row["duration_hours"])
        rk, rp = v35._dynamic_reserve_kwh(row, cfg, cap)
        nxt: dict[int, float] = {}
        back: dict[int, tuple[Any, ...]] = {}
        for i0, prior in costs.items():
            for i1, e1 in enumerate(states):
                action = _transition_action_kw(states[i0], e1, ec, ed, dt)
                if action < -cmax - 1e-9 or action > dmax + 1e-9:
                    continue
                res = _interval_result(row, action, cfg)
                if not res["feasible"]:
                    continue
                reserve_adj = _reserve_policy_penalty_ore(e1, rk, cfg, cap, hmin, pmin, dt)
                upper_adj = max(0.0, e1 - pmaxk) * excess_rate * dt
                continuation_adj = 0.0
                if boundary is not None and t == boundary:
                    target = float(continuation.get("target_kwh") or 0.0)
                    ref = float(continuation.get("reference_price_ore_kwh") or 0.0)
                    risk = float(continuation.get("risk_premium_ore_kwh") or 0.0)
                    continuation_adj -= e1 * ref
                    if e1 < target:
                        continuation_adj += (target - e1) * risk
                adj = reserve_adj + upper_adj + continuation_adj
                total = prior + float(res["interval_cost_ore"] or 0.0) + adj
                if i1 not in nxt or total < nxt[i1]:
                    nxt[i1] = total
                    back[i1] = (
                        i0, action, res, rk, rp, adj,
                        reserve_adj, upper_adj, continuation_adj,
                    )
        if not nxt:
            raise RuntimeError(
                f"No feasible live optimizer states at {row['start']}; check grid/battery limits"
            )
        costs = nxt
        parents.append(back)

    if continuation.get("enabled"):
        best, term_applied = min(costs, key=costs.get), False
    else:
        tol = cap * termtol / 100.0
        candidates = [i for i in costs if abs(states[i] - initial) <= tol + 1e-9]
        if not candidates:
            nearest = min(abs(states[i] - initial) for i in costs)
            candidates = [i for i in costs if abs(abs(states[i] - initial) - nearest) <= 1e-9]
        best = min(candidates, key=lambda i: costs[i] + abs(states[i] - initial) * termtie)
        term_applied = True

    path: list[tuple[Any, ...]] = []
    idx = best
    for t in range(len(rows) - 1, -1, -1):
        prev, action, res, rk, rp, adj, radj, uadj, cadj = parents[t][idx]
        path.append((prev, idx, action, res, rk, rp, adj, radj, uadj, cadj))
        idx = prev
    path.reverse()

    out: list[dict[str, Any]] = []
    obj = cash = bexp = ddis = hurdle = padj = 0.0
    reserve_padj = upper_padj = continuation_padj = 0.0
    boundary_soc = None
    for t, (row, (i0, i1, action, res, rk, rp, adj, radj, uadj, cadj)) in enumerate(zip(rows, path)):
        dt = float(row["duration_hours"])
        obj += float(res["interval_cost_ore"] or 0.0) + adj
        cash += float(res["cash_cost_ore"] or 0.0)
        bexp += float(res["battery_export_kw"] or 0.0) * dt
        ddis += float(res["discretionary_discharge_kw"] or 0.0) * dt
        hurdle += float(res["discretionary_shift_hurdle_cost_ore"] or 0.0)
        padj += adj
        reserve_padj += radj
        upper_padj += uadj
        continuation_padj += cadj
        if boundary is not None and t == boundary:
            boundary_soc = states[i1] / cap * 100.0
        reason, flow = v35._classify_action(row, action, res)
        out.append({
            **row,
            "soc_start_pct": round(states[i0] / cap * 100.0, 2),
            "battery_action_kw": round(action, 4),
            "expected_soc_pct": round(states[i1] / cap * 100.0, 2),
            "reserve_soc_pct": round(rp, 2),
            "grid_import_kw": round(float(res["grid_import_kw"] or 0.0), 4),
            "grid_export_kw": round(float(res["grid_export_kw"] or 0.0), 4),
            "battery_export_kw": round(float(res["battery_export_kw"] or 0.0), 4),
            "required_physical_discharge_kw": round(float(res["required_physical_discharge_kw"] or 0.0), 4),
            "discretionary_discharge_kw": round(float(res["discretionary_discharge_kw"] or 0.0), 4),
            "curtailed_kw": round(float(res["curtailed_kw"] or 0.0), 4),
            "effective_import_price_ore_kwh": res["effective_import_price_ore_kwh"],
            "effective_export_price_ore_kwh": res["effective_export_price_ore_kwh"],
            "energy_cost_ore": round(float(res["energy_cost_ore"] or 0.0), 4) if row["price_known"] else None,
            "degradation_cost_ore": round(float(res["degradation_cost_ore"] or 0.0), 4),
            "cash_cost_ore": round(float(res["cash_cost_ore"] or 0.0), 4),
            "discretionary_shift_hurdle_cost_ore": round(float(res["discretionary_shift_hurdle_cost_ore"] or 0.0), 4),
            "arbitrage_hurdle_cost_ore": round(float(res["discretionary_shift_hurdle_cost_ore"] or 0.0), 4),
            "reserve_policy_penalty_ore": round(radj, 4),
            "preferred_max_excess_penalty_ore": round(uadj, 4),
            "continuation_policy_adjustment_ore": round(cadj, 4),
            "policy_adjustment_ore": round(adj, 4),
            "objective_cost_ore": round(float(res["interval_cost_ore"] or 0.0) + adj, 4),
            "reason": reason,
            "flow_breakdown_kw": {k: round(v, 4) for k, v in flow.items()},
        })

    baseline = _baseline_cost(rows, cfg)
    term_soc = states[best] / cap * 100.0
    ref = float(continuation.get("reference_price_ore_kwh") or 0.0)
    risk = float(continuation.get("risk_premium_ore_kwh") or 0.0)
    target = float(continuation.get("target_kwh") or 0.0)
    boundary_kwh = cap * boundary_soc / 100.0 if boundary_soc is not None else states[best]
    opt_asset = base_asset = 0.0
    if continuation.get("enabled"):
        opt_asset = boundary_kwh * ref + min(boundary_kwh, target) * risk
        base_asset = initial * ref + min(initial, target) * risk
    cash_save = baseline - cash
    econ_save = cash_save + opt_asset - base_asset

    known_hours = sum(float(r["duration_hours"]) for r in rows if r["price_known"])
    unknown_hours = sum(float(r["duration_hours"]) for r in rows if not r["price_known"])
    total_hours = known_hours + unknown_hours
    diag = dict(v35.horizon_diagnostics(cfg))
    diag.update({
        "known_price_horizon_hours": round(known_hours, 4),
        "unknown_price_horizon_hours": round(unknown_hours, 4),
        "live_horizon_hours": round(total_hours, 4),
        "first_interval_minutes": round(float(rows[0]["duration_minutes"]), 3),
        "first_interval_partial": bool(rows[0].get("partial_interval")),
        "live_replan_at": now.isoformat(),
    })
    critical_pct = min(
        max(hmin, float(o.get("reserve_critical_soc_pct", 10.0))),
        max(hmin, pmin),
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "planner": PLANNER_NAME,
        "baseline_planner": BASELINE_PLANNER,
        "mode": "shadow_read_only_receding_horizon",
        "comparison_eligible": False,
        "comparison_exclusion_reason": "intra_quarter_variable_duration_live_replan",
        "replan_reason": replan_reason,
        "interval_minutes": 15,
        "horizon_hours": round(total_hours, 4),
        "initial_soc_pct": round(soc, 2),
        "initial_soc_observed_at": obs["observed_at"],
        "initial_soc_age_seconds": round(float(obs["age_seconds"]), 2),
        "horizon_diagnostics": diag,
        "constraints": {
            "battery_capacity_kwh": cap,
            "hard_min_soc_pct": hmin,
            "hard_max_soc_pct": hmax,
            "preferred_min_soc_pct": pmin,
            "preferred_max_soc_pct": pmax,
            "normal_reserve_soc_pct": normal,
            "high_uncertainty_reserve_soc_pct": high,
            "reserve_uncertainty_full_scale_kw": float(o.get("reserve_uncertainty_full_scale_kw", 3.0)),
            "reserve_penalty_mode": "piecewise_marginal",
            "reserve_critical_soc_pct": critical_pct,
            "reserve_critical_penalty_ore_per_kwh_hour": float(o.get("reserve_critical_penalty_ore_per_kwh_hour", 300.0)),
            "reserve_preferred_penalty_ore_per_kwh_hour": float(o.get("reserve_preferred_penalty_ore_per_kwh_hour", 100.0)),
            "reserve_target_penalty_ore_per_kwh_hour": float(o.get("reserve_target_penalty_ore_per_kwh_hour", 10.0)),
            "preferred_max_excess_penalty_ore_per_kwh_hour": excess_rate,
            "battery_max_charge_kw": cmax,
            "battery_max_discharge_kw": dmax,
            "physical_grid_import_limit_kw": float(o.get("physical_grid_import_limit_kw", 13.8)),
            "grid_export_limit_kw": float(o.get("grid_export_limit_kw", 10.0)),
            "charge_efficiency": ec,
            "discharge_efficiency": ed,
            "soc_grid_requested_step_kwh": reqstep,
            "soc_grid_effective_max_step_kwh": round(effstep, 6),
            "soc_grid_state_count": len(states),
            "soc_grid_includes_hard_boundaries": True,
            "soc_grid_includes_initial_soc": True,
            "terminal_soc_tolerance_pct": termtol,
            "variable_first_interval": True,
            "soc_observation_max_age_seconds": max_age,
        },
        "objective": {
            "energy_cost_on_published_prices_only": True,
            "battery_degradation_cost": True,
            "dynamic_uncertainty_reserve": True,
            "time_calibrated_reserve_shortfall_penalty": True,
            "piecewise_marginal_reserve_penalty": True,
            "fair_terminal_soc_constraint_when_fully_priced": True,
            "battery_export_arbitrage": True,
            "minimum_net_discretionary_shift_margin_ore_kwh": float(
                ((cfg.get("policy") or {}).get("economics") or {}).get(
                    "minimum_arbitrage_margin_ore_kwh", 20.0
                )
            ),
            "physical_limit_discharge_exempt_from_margin": True,
            "variable_price_horizon": True,
            "unknown_price_grid_charging": False,
            "unknown_price_battery_export": False,
            "continuation_value_from_physical_forecast": True,
            "component_forecasts_included": True,
            "variable_duration_first_interval": True,
        },
        "continuation": {
            "enabled": bool(continuation.get("enabled")),
            "price_boundary_soc_pct": round(boundary_soc, 2) if boundary_soc is not None else None,
            "target_soc_pct": round(float(continuation["target_soc_pct"]), 2) if continuation.get("target_soc_pct") is not None else None,
            "value_ore_per_kwh": round(float(continuation["value_ore_per_kwh"]), 2) if continuation.get("value_ore_per_kwh") is not None else None,
            "reference_price_ore_kwh": round(float(continuation["reference_price_ore_kwh"]), 2) if continuation.get("reference_price_ore_kwh") is not None else None,
            "risk_premium_ore_kwh": round(float(continuation.get("risk_premium_ore_kwh") or 0.0), 2),
            "unknown_net_deficit_kwh": round(float(continuation.get("unknown_net_deficit_kwh") or 0.0), 3),
            "unknown_peak_support_kwh": round(float(continuation.get("unknown_peak_support_kwh") or 0.0), 3),
            "energy_coverage_fraction": round(float(continuation.get("coverage_fraction") or 0.0), 3),
        },
        "summary": {
            "objective_cost_ore": round(obj, 2),
            "expected_cash_cost_ore": round(cash, 2),
            "baseline_cash_cost_ore": round(baseline, 2),
            "expected_cash_saving_ore": round(cash_save, 2),
            "expected_cash_saving_sek": round(cash_save / 100.0, 2),
            "optimized_continuation_asset_value_ore": round(opt_asset, 2),
            "baseline_continuation_asset_value_ore": round(base_asset, 2),
            "expected_saving_ore": round(econ_save, 2),
            "expected_saving_sek": round(econ_save / 100.0, 2),
            "expected_saving_scope": "published_prices_plus_continuation_asset_value",
            "cash_cost_scope": "published_price_intervals_plus_battery_degradation",
            "priced_horizon_hours": round(known_hours, 4),
            "unpriced_horizon_hours": round(unknown_hours, 4),
            "terminal_soc_pct": round(term_soc, 2),
            "terminal_soc_delta_pct": round(term_soc - soc, 2),
            "terminal_soc_constraint_applied": term_applied,
            "battery_export_kwh": round(bexp, 3),
            "discretionary_discharge_kwh": round(ddis, 3),
            "discretionary_shift_hurdle_cost_ore": round(hurdle, 2),
            "arbitrage_hurdle_cost_ore": round(hurdle, 2),
            "reserve_policy_penalty_ore": round(reserve_padj, 2),
            "preferred_max_excess_penalty_ore": round(upper_padj, 2),
            "continuation_policy_adjustment_ore": round(continuation_padj, 2),
            "policy_adjustment_ore": round(padj, 2),
        },
        "rows": out,
    }
