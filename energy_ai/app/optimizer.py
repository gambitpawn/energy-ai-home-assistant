from __future__ import annotations

import json, math, sqlite3
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any
from .db import DB_PATH

PLANNER_NAME = "deterministic_battery_dp_v3_5"
DT_HOURS = 0.25


def _parse_ts(v: str) -> datetime:
    d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _canonical_ts(v: str) -> str:
    return _parse_ts(v).replace(second=0, microsecond=0).isoformat()


def _latest_soc_pct() -> float | None:
    with sqlite3.connect(DB_PATH) as c:
        r = c.execute("SELECT payload_json FROM raw_state ORDER BY id DESC LIMIT 1").fetchone()
    if not r:
        return None
    try:
        item = (json.loads(r[0]).get("battery_soc_pct") or {})
        return float(item.get("state")) if item.get("available") else None
    except Exception:
        return None


def _latest_load_rows() -> dict[str, dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as c:
        r = c.execute("SELECT MAX(generated_at) FROM load_forecast_15m").fetchone()
        g = r[0] if r else None
        if not g:
            return {}
        rows = c.execute(
            "SELECT start_utc,payload_json FROM load_forecast_15m WHERE generated_at=? ORDER BY start_utc",
            (g,),
        ).fetchall()
    out = {}
    for s, p in rows:
        try:
            out[_canonical_ts(str(s))] = json.loads(p)
        except Exception:
            pass
    return out


def _latest_pv_rows() -> dict[str, dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as c:
        r = c.execute("SELECT MAX(generated_at) FROM pv_forecast_15m").fetchone()
        g = r[0] if r else None
        if not g:
            return {}
        rows = c.execute(
            "SELECT start_utc,payload_json,forecast_kw,uncertainty_kw FROM pv_forecast_15m WHERE generated_at=? ORDER BY start_utc",
            (g,),
        ).fetchall()
    out = {}
    for s, p, f, u in rows:
        item = {"pv_power_forecast_kw": float(f), "pv_power_uncertainty_kw": float(u)}
        if p:
            try:
                item.update(json.loads(p))
            except Exception:
                pass
        try:
            out[_canonical_ts(str(s))] = item
        except Exception:
            pass
    return out


def _price_rows() -> dict[str, float]:
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute("SELECT start_utc,price_ore_kwh FROM price_15m ORDER BY start_utc").fetchall()
    out = {}
    for s, p in rows:
        try:
            out[_canonical_ts(str(s))] = float(p)
        except Exception:
            pass
    return out


def _price_coverage(starts: list[str], prices: dict[str, float]) -> tuple[int, str | None, str | None]:
    n = 0
    for s in starts:
        if s not in prices:
            break
        n += 1
    if not n:
        return 0, None, starts[0] if starts else None
    return n, starts[n - 1], starts[n] if n < len(starts) else None


def horizon_diagnostics(cfg: dict[str, Any]) -> dict[str, Any]:
    load, pv, prices = _latest_load_rows(), _latest_pv_rows(), _price_rows()
    matched = sorted(set(load).intersection(pv), key=_parse_ts)
    requested = int((cfg.get("forecast") or {}).get("horizon_hours", 36)) * 4
    starts = matched[:requested]
    n, last, first_unknown = _price_coverage(starts, prices)
    return {
        "load_intervals": len(load), "pv_intervals": len(pv), "price_intervals": len(prices),
        "matched_load_pv_intervals": len(matched),
        "first_load": min(load, key=_parse_ts) if load else None,
        "first_pv": min(pv, key=_parse_ts) if pv else None,
        "first_match": matched[0] if matched else None,
        "requested_horizon_intervals": requested, "physical_horizon_intervals": len(starts),
        "known_price_intervals": n, "unknown_price_intervals": max(0, len(starts) - n),
        "known_price_horizon_hours": round(n * DT_HOURS, 2),
        "unknown_price_horizon_hours": round(max(0, len(starts) - n) * DT_HOURS, 2),
        "last_known_price_start": last,
        "known_price_until": (_parse_ts(last) + timedelta(minutes=15)).isoformat() if last else None,
        "first_unknown_price_start": first_unknown, "price_fallback_used": False,
        "price_coverage_mode": "contiguous_published_intervals_only",
        "timestamp_join": "normalized_utc_minute",
    }


def _build_horizon(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    load, pv, prices = _latest_load_rows(), _latest_pv_rows(), _price_rows()
    starts = sorted(set(load).intersection(pv), key=_parse_ts)
    starts = starts[: int((cfg.get("forecast") or {}).get("horizon_hours", 36)) * 4]
    if not starts:
        raise RuntimeError(f"No overlapping load and PV forecast rows are available after UTC normalization; diagnostics={horizon_diagnostics(cfg)}")
    n, _, _ = _price_coverage(starts, prices)
    out = []
    for i, s in enumerate(starts):
        l, p = load[s], pv[s]
        known = i < n
        out.append({
            "start": _parse_ts(s).isoformat(),
            "load_kw": float(l.get("house_load_forecast_kw") or l.get("total_forecast_kw") or 0),
            "base_load_kw": float(l.get("base_household_forecast_kw") or 0),
            "component_forecast_kw": l.get("component_forecast_kw") or {},
            "load_uncertainty_kw": float(l.get("house_load_uncertainty_kw") or 0),
            "pv_kw": float(p.get("pv_power_forecast_kw") or 0),
            "pv_uncertainty_kw": float(p.get("pv_power_uncertainty_kw") or 0),
            "price_known": known, "price_ore_kwh": float(prices[s]) if known else None,
        })
    return out


def _segment(a: float, b: float, max_step: float) -> tuple[list[float], float]:
    if b <= a + 1e-12:
        return [round(a, 6)], 0.0
    n = max(1, int(math.ceil((b - a) / max(1e-6, max_step))))
    step = (b - a) / n
    vals = [round(a + i * step, 6) for i in range(n + 1)]
    vals[0], vals[-1] = round(a, 6), round(b, 6)
    return vals, step


def _state_grid(min_kwh: float, max_kwh: float, step_kwh: float, initial_kwh: float) -> tuple[list[float], float]:
    """Grid includes hard min, measured start SOC and hard max with no short boundary hop."""
    initial = max(min_kwh, min(max_kwh, initial_kwh))
    left, ls = _segment(min_kwh, initial, step_kwh)
    right, rs = _segment(initial, max_kwh, step_kwh)
    states = left + right[1:]
    return sorted(set(states)), max(ls, rs)


def _transition_action_kw(e0: float, e1: float, ec: float, ed: float) -> float:
    d = e1 - e0
    return -(d / ec) / DT_HOURS if d > 0 else ((-d) * ed) / DT_HOURS if d < 0 else 0.0


def _dynamic_reserve_kwh(row: dict[str, Any], cfg: dict[str, Any], cap: float) -> tuple[float, float]:
    b, o = (cfg.get("policy") or {}).get("battery") or {}, cfg.get("optimizer") or {}
    lo, hi = float(b.get("normal_reserve_soc_pct", 20)), float(b.get("high_uncertainty_reserve_soc_pct", 28))
    scale = max(.01, float(o.get("reserve_uncertainty_full_scale_kw", 3)))
    unc = max(0, float(row.get("load_uncertainty_kw") or 0)) + max(0, float(row.get("pv_uncertainty_kw") or 0))
    pct = lo + (hi - lo) * min(1, unc / scale)
    return cap * pct / 100, pct


def _zone_shortfall_kwh(energy_kwh: float, lower_kwh: float, upper_kwh: float) -> float:
    """Missing energy inside one marginal reserve zone, with no overlap between zones."""
    if upper_kwh <= lower_kwh + 1e-12 or energy_kwh >= upper_kwh:
        return 0.0
    return max(0.0, upper_kwh - max(energy_kwh, lower_kwh))


def _reserve_policy_penalty_ore(
    energy_kwh: float,
    reserve_kwh: float,
    cfg: dict[str, Any],
    cap: float,
    hard_min_soc_pct: float,
    preferred_min_soc_pct: float,
) -> float:
    """Piecewise marginal risk cost: critical > preferred > reserve-target zone."""
    o = cfg.get("optimizer") or {}
    preferred_pct = max(hard_min_soc_pct, preferred_min_soc_pct)
    critical_pct = min(
        preferred_pct,
        max(hard_min_soc_pct, float(o.get("reserve_critical_soc_pct", 10.0))),
    )
    hard_min_kwh = cap * hard_min_soc_pct / 100
    critical_kwh = cap * critical_pct / 100
    preferred_kwh = cap * preferred_pct / 100
    target_kwh = max(hard_min_kwh, reserve_kwh)

    critical_hi = min(critical_kwh, target_kwh)
    preferred_hi = min(preferred_kwh, target_kwh)
    critical_missing = _zone_shortfall_kwh(energy_kwh, hard_min_kwh, critical_hi)
    preferred_missing = _zone_shortfall_kwh(energy_kwh, critical_kwh, preferred_hi)
    target_missing = _zone_shortfall_kwh(energy_kwh, preferred_kwh, target_kwh)

    critical_rate = max(0.0, float(o.get("reserve_critical_penalty_ore_per_kwh_hour", 300.0)))
    preferred_rate = max(0.0, float(o.get("reserve_preferred_penalty_ore_per_kwh_hour", 100.0)))
    target_rate = max(0.0, float(o.get("reserve_target_penalty_ore_per_kwh_hour", 10.0)))
    return (
        critical_missing * critical_rate
        + preferred_missing * preferred_rate
        + target_missing * target_rate
    ) * DT_HOURS


def _continuation_profile(rows, cfg, cap, preferred_max_kwh, eta_discharge):
    o, e = cfg.get("optimizer") or {}, (cfg.get("policy") or {}).get("economics") or {}
    unknown, known = [r for r in rows if not r["price_known"]], [r for r in rows if r["price_known"]]
    if not unknown:
        return {"enabled": False, "target_kwh": None, "target_soc_pct": None, "value_ore_per_kwh": None,
                "unknown_net_deficit_kwh": 0.0, "unknown_peak_support_kwh": 0.0, "reference_price_ore_kwh": None}
    lim = float(o.get("physical_grid_import_limit_kw", 13.8))
    frac = max(0, min(1, float(o.get("unknown_price_energy_coverage_fraction", .35))))
    riskmax = max(0, float(o.get("unknown_price_risk_premium_ore_kwh", 40)))
    default = max(0, float(o.get("unknown_price_default_continuation_value_ore_kwh", 150)))
    scale = max(.01, float(o.get("reserve_uncertainty_full_scale_kw", 3)))
    reserve = max((_dynamic_reserve_kwh(r, cfg, cap)[0] for r in unknown), default=0)
    deficit = sum(max(0, r["load_kw"] - r["pv_kw"]) * DT_HOURS for r in unknown)
    peak = sum(max(0, r["load_kw"] - r["pv_kw"] - lim) * DT_HOURS / max(.01, eta_discharge) for r in unknown)
    covered = deficit * frac / max(.01, eta_discharge)
    target = min(preferred_max_kwh, max(reserve + covered, reserve + peak))
    buys = [float(r["price_ore_kwh"]) + float(e.get("import_overhead_ore_kwh", 0)) for r in known if r["price_ore_kwh"] is not None]
    ref = float(median(buys)) if buys else default
    avgunc = sum(max(0, float(r.get("load_uncertainty_kw") or 0)) + max(0, float(r.get("pv_uncertainty_kw") or 0)) for r in unknown) / len(unknown)
    risk = riskmax * (.6 * min(1, deficit / max(.01, cap)) + .4 * min(1, avgunc / scale))
    return {"enabled": True, "target_kwh": target, "target_soc_pct": target / cap * 100,
            "value_ore_per_kwh": ref + risk, "unknown_net_deficit_kwh": deficit,
            "unknown_peak_support_kwh": peak, "reference_price_ore_kwh": ref,
            "risk_premium_ore_kwh": risk, "coverage_fraction": frac}


def _interval_result(row, action, cfg):
    o, e = cfg.get("optimizer") or {}, (cfg.get("policy") or {}).get("economics") or {}
    ilim, elim = float(o.get("physical_grid_import_limit_kw", 13.8)), float(o.get("grid_export_limit_kw", 10))
    load, pv = float(row["load_kw"]), float(row["pv_kw"])
    net = load - pv
    grid = net - action
    imp, rawexp = max(0, grid), max(0, -grid)
    exp, curt = min(rawexp, elim), max(0, rawexp - elim)
    pv_surplus, charge = max(0, pv - load), max(0, -action)
    pv_charge, grid_charge = min(charge, pv_surplus), max(0, charge - min(charge, pv_surplus))
    batt_export = min(exp, max(0, action - max(0, net))) if action > 0 and exp > 0 else 0
    required = max(0, net - ilim)
    discretionary = max(0, action - required) if action > 0 else 0
    feasible = imp <= ilim + 1e-9
    degr = abs(action) * DT_HOURS * float(o.get("battery_degradation_ore_kwh", 5))
    if not row["price_known"]:
        if grid_charge > 1e-6 or batt_export > 1e-6 or (required <= 1e-6 and action > 1e-6):
            feasible = False
        energy, hurdle = 0.0, 0.0
    else:
        price = float(row["price_ore_kwh"])
        buy = price + float(e.get("import_overhead_ore_kwh", 0))
        sell = max(0, price - float(e.get("export_overhead_ore_kwh", 0)))
        energy = imp * DT_HOURS * buy - exp * DT_HOURS * sell
        hurdle = discretionary * DT_HOURS * float(e.get("minimum_arbitrage_margin_ore_kwh", 20))
    cash = energy + degr
    return {"feasible": 1.0 if feasible else 0.0, "grid_import_kw": imp, "grid_export_kw": exp,
            "battery_export_kw": batt_export, "pv_charge_kw": pv_charge, "grid_charge_kw": grid_charge,
            "required_physical_discharge_kw": required, "discretionary_discharge_kw": discretionary,
            "curtailed_kw": curt, "energy_cost_ore": energy, "degradation_cost_ore": degr,
            "cash_cost_ore": cash, "discretionary_shift_hurdle_cost_ore": hurdle,
            "arbitrage_hurdle_cost_ore": hurdle, "interval_cost_ore": cash + hurdle}


def _baseline_cost(rows, cfg):
    return sum(_interval_result(r, 0, cfg)["cash_cost_ore"] for r in rows)


def _classify_action(row, a, r):
    if not row["price_known"]:
        if a > .05: return "physical_limit_discharge", {"battery_to_load_kw": a, "battery_to_grid_kw": 0.0}
        if a < -.05: return "pv_charge_unpriced", {"pv_to_battery_kw": r["pv_charge_kw"], "grid_to_battery_kw": 0.0}
        return "hold_unpriced", {}
    if a > .05:
        if r["battery_export_kw"] > .05:
            return "export_arbitrage", {"battery_to_grid_kw": r["battery_export_kw"], "battery_to_load_kw": max(0, a-r["battery_export_kw"])}
        return "self_consumption_discharge", {"battery_to_load_kw": a, "battery_to_grid_kw": 0.0}
    if a < -.05:
        p, g = r["pv_charge_kw"], r["grid_charge_kw"]
        return ("mixed_charge" if p > .05 and g > .05 else "pv_charge" if p > .05 else "grid_charge"), {"pv_to_battery_kw": p, "grid_to_battery_kw": g}
    return "hold", {}


def build_plan(cfg: dict[str, Any]) -> dict[str, Any]:
    rows, o = _build_horizon(cfg), cfg.get("optimizer") or {}
    b = (cfg.get("policy") or {}).get("battery") or {}
    cap = float(b.get("capacity_kwh", 19.6))
    hmin, hmax = float(b.get("hard_min_soc_pct", 5)), float(b.get("hard_max_soc_pct", 100))
    pmin, pmax = float(b.get("preferred_min_soc_pct", 15)), float(b.get("preferred_max_soc_pct", 90))
    normal, high = float(b.get("normal_reserve_soc_pct", 20)), float(b.get("high_uncertainty_reserve_soc_pct", 28))
    cmax, dmax = float(o.get("battery_max_charge_kw", 8)), float(o.get("battery_max_discharge_kw", 8))
    ec, ed = float(o.get("battery_charge_efficiency", .95)), float(o.get("battery_discharge_efficiency", .95))
    reqstep = float(o.get("soc_grid_step_kwh", .5))
    termtol = float(o.get("terminal_soc_tolerance_pct", 3))
    termtie = float(o.get("terminal_soc_tiebreak_ore_per_kwh", 5))
    soc = _latest_soc_pct()
    if soc is None:
        raise RuntimeError("Current battery SOC is unavailable")
    initial = cap * soc / 100
    mink, maxk, pmink, pmaxk = cap*hmin/100, cap*hmax/100, cap*pmin/100, cap*pmax/100
    states, effstep = _state_grid(mink, maxk, reqstep, initial)
    init_idx = min(range(len(states)), key=lambda i: abs(states[i]-initial))
    continuation = _continuation_profile(rows, cfg, cap, pmaxk, ed)
    known_n = sum(1 for r in rows if r["price_known"])
    boundary = known_n-1 if 0 < known_n < len(rows) else None
    costs = {init_idx: 0.0}
    parents = []
    excess_rate = max(0.0, float(o.get("preferred_max_excess_penalty_ore_per_kwh_hour", 2.0)))

    for t, row in enumerate(rows):
        rk, rp = _dynamic_reserve_kwh(row, cfg, cap)
        nxt, back = {}, {}
        for i0, prior in costs.items():
            for i1, e1 in enumerate(states):
                a = _transition_action_kw(states[i0], e1, ec, ed)
                if a < -cmax-1e-9 or a > dmax+1e-9:
                    continue
                res = _interval_result(row, a, cfg)
                if not res["feasible"]:
                    continue

                reserve_adj = _reserve_policy_penalty_ore(e1, rk, cfg, cap, hmin, pmin)
                upper_adj = max(0.0, e1-pmaxk) * excess_rate * DT_HOURS
                continuation_adj = 0.0
                if boundary is not None and t == boundary:
                    target = float(continuation.get("target_kwh") or 0)
                    ref = float(continuation.get("reference_price_ore_kwh") or 0)
                    risk = float(continuation.get("risk_premium_ore_kwh") or 0)
                    continuation_adj -= e1 * ref
                    if e1 < target:
                        continuation_adj += (target-e1) * risk
                adj = reserve_adj + upper_adj + continuation_adj
                total = prior + res["interval_cost_ore"] + adj
                if i1 not in nxt or total < nxt[i1]:
                    nxt[i1] = total
                    back[i1] = (i0, a, res, rk, rp, adj, reserve_adj, upper_adj, continuation_adj)
        if not nxt:
            raise RuntimeError(f"No feasible optimizer states at {row['start']}; check grid/battery limits")
        costs, parents = nxt, parents+[back]

    if continuation.get("enabled"):
        best, term_applied = min(costs, key=costs.get), False
    else:
        tol = cap*termtol/100
        cand = [i for i in costs if abs(states[i]-initial) <= tol+1e-9]
        if not cand:
            d = min(abs(states[i]-initial) for i in costs)
            cand = [i for i in costs if abs(abs(states[i]-initial)-d) <= 1e-9]
        best, term_applied = min(cand, key=lambda i: costs[i]+abs(states[i]-initial)*termtie), True

    path, idx = [], best
    for t in range(len(rows)-1, -1, -1):
        prev, a, res, rk, rp, adj, radj, uadj, cadj = parents[t][idx]
        path.append((prev, idx, a, res, rk, rp, adj, radj, uadj, cadj))
        idx = prev
    path.reverse()

    out = []
    obj = cash = bexp = ddis = hurdle = padj = 0.0
    reserve_padj = upper_padj = continuation_padj = 0.0
    boundary_soc = None
    for t, (row, (_, i1, a, res, rk, rp, adj, radj, uadj, cadj)) in enumerate(zip(rows, path)):
        obj += res["interval_cost_ore"] + adj
        cash += res["cash_cost_ore"]
        bexp += res["battery_export_kw"] * DT_HOURS
        ddis += res["discretionary_discharge_kw"] * DT_HOURS
        hurdle += res["discretionary_shift_hurdle_cost_ore"]
        padj += adj
        reserve_padj += radj
        upper_padj += uadj
        continuation_padj += cadj
        if boundary is not None and t == boundary:
            boundary_soc = states[i1]/cap*100
        reason, flow = _classify_action(row, a, res)
        out.append({
            **row,
            "battery_action_kw": round(a,4),
            "expected_soc_pct": round(states[i1]/cap*100,2),
            "reserve_soc_pct": round(rp,2),
            "grid_import_kw": round(res["grid_import_kw"],4),
            "grid_export_kw": round(res["grid_export_kw"],4),
            "battery_export_kw": round(res["battery_export_kw"],4),
            "required_physical_discharge_kw": round(res["required_physical_discharge_kw"],4),
            "discretionary_discharge_kw": round(res["discretionary_discharge_kw"],4),
            "curtailed_kw": round(res["curtailed_kw"],4),
            "energy_cost_ore": round(res["energy_cost_ore"],4) if row["price_known"] else None,
            "degradation_cost_ore": round(res["degradation_cost_ore"],4),
            "cash_cost_ore": round(res["cash_cost_ore"],4),
            "discretionary_shift_hurdle_cost_ore": round(res["discretionary_shift_hurdle_cost_ore"],4),
            "arbitrage_hurdle_cost_ore": round(res["discretionary_shift_hurdle_cost_ore"],4),
            "reserve_policy_penalty_ore": round(radj,4),
            "preferred_max_excess_penalty_ore": round(uadj,4),
            "continuation_policy_adjustment_ore": round(cadj,4),
            "policy_adjustment_ore": round(adj,4),
            "objective_cost_ore": round(res["interval_cost_ore"]+adj,4),
            "reason": reason,
            "flow_breakdown_kw": {k: round(v,4) for k,v in flow.items()},
        })

    baseline, diag = _baseline_cost(rows,cfg), horizon_diagnostics(cfg)
    term_soc = states[best]/cap*100
    ref = float(continuation.get("reference_price_ore_kwh") or 0)
    risk = float(continuation.get("risk_premium_ore_kwh") or 0)
    target = float(continuation.get("target_kwh") or 0)
    bk = cap*boundary_soc/100 if boundary_soc is not None else states[best]
    opt_asset = base_asset = 0.0
    if continuation.get("enabled"):
        opt_asset = bk*ref + min(bk,target)*risk
        base_asset = initial*ref + min(initial,target)*risk
    cash_save = baseline-cash
    econ_save = cash_save+opt_asset-base_asset

    critical_pct = min(max(hmin, float(o.get("reserve_critical_soc_pct",10.0))), max(hmin,pmin))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "planner": PLANNER_NAME,
        "mode":"shadow_read_only",
        "interval_minutes":15,
        "horizon_hours":len(rows)//4,
        "initial_soc_pct":round(soc,2),
        "horizon_diagnostics":diag,
        "constraints":{
            "battery_capacity_kwh":cap,
            "hard_min_soc_pct":hmin,
            "hard_max_soc_pct":hmax,
            "preferred_min_soc_pct":pmin,
            "preferred_max_soc_pct":pmax,
            "normal_reserve_soc_pct":normal,
            "high_uncertainty_reserve_soc_pct":high,
            "reserve_uncertainty_full_scale_kw":float(o.get("reserve_uncertainty_full_scale_kw",3)),
            "reserve_penalty_mode":"piecewise_marginal",
            "reserve_critical_soc_pct":critical_pct,
            "reserve_critical_penalty_ore_per_kwh_hour":float(o.get("reserve_critical_penalty_ore_per_kwh_hour",300)),
            "reserve_preferred_penalty_ore_per_kwh_hour":float(o.get("reserve_preferred_penalty_ore_per_kwh_hour",100)),
            "reserve_target_penalty_ore_per_kwh_hour":float(o.get("reserve_target_penalty_ore_per_kwh_hour",10)),
            "preferred_max_excess_penalty_ore_per_kwh_hour":excess_rate,
            "reserve_penalty_interval_hours":DT_HOURS,
            "battery_max_charge_kw":cmax,
            "battery_max_discharge_kw":dmax,
            "physical_grid_import_limit_kw":float(o.get("physical_grid_import_limit_kw",13.8)),
            "grid_export_limit_kw":float(o.get("grid_export_limit_kw",10)),
            "charge_efficiency":ec,
            "discharge_efficiency":ed,
            "soc_grid_requested_step_kwh":reqstep,
            "soc_grid_effective_max_step_kwh":round(effstep,6),
            "soc_grid_state_count":len(states),
            "soc_grid_includes_hard_boundaries":True,
            "soc_grid_includes_initial_soc":True,
            "terminal_soc_tolerance_pct":termtol,
        },
        "objective":{
            "energy_cost_on_published_prices_only":True,
            "battery_degradation_cost":True,
            "dynamic_uncertainty_reserve":True,
            "time_calibrated_reserve_shortfall_penalty":True,
            "piecewise_marginal_reserve_penalty":True,
            "fair_terminal_soc_constraint_when_fully_priced":True,
            "battery_export_arbitrage":True,
            "minimum_net_discretionary_shift_margin_ore_kwh":float(((cfg.get("policy") or {}).get("economics") or {}).get("minimum_arbitrage_margin_ore_kwh",20)),
            "discretionary_self_consumption_hurdle":True,
            "physical_limit_discharge_exempt_from_margin":True,
            "grid_import_soft_target":False,
            "variable_price_horizon":True,
            "unknown_price_grid_charging":False,
            "unknown_price_battery_export":False,
            "continuation_value_from_physical_forecast":True,
            "component_forecasts_included":True,
        },
        "continuation":{
            "enabled":bool(continuation.get("enabled")),
            "price_boundary_soc_pct":round(boundary_soc,2) if boundary_soc is not None else None,
            "target_soc_pct":round(float(continuation["target_soc_pct"]),2) if continuation.get("target_soc_pct") is not None else None,
            "value_ore_per_kwh":round(float(continuation["value_ore_per_kwh"]),2) if continuation.get("value_ore_per_kwh") is not None else None,
            "reference_price_ore_kwh":round(float(continuation["reference_price_ore_kwh"]),2) if continuation.get("reference_price_ore_kwh") is not None else None,
            "risk_premium_ore_kwh":round(float(continuation.get("risk_premium_ore_kwh") or 0),2),
            "unknown_net_deficit_kwh":round(float(continuation.get("unknown_net_deficit_kwh") or 0),3),
            "unknown_peak_support_kwh":round(float(continuation.get("unknown_peak_support_kwh") or 0),3),
            "energy_coverage_fraction":round(float(continuation.get("coverage_fraction") or 0),3),
        },
        "summary":{
            "objective_cost_ore":round(obj,2),
            "expected_cash_cost_ore":round(cash,2),
            "baseline_cash_cost_ore":round(baseline,2),
            "expected_cash_saving_ore":round(cash_save,2),
            "expected_cash_saving_sek":round(cash_save/100,2),
            "optimized_continuation_asset_value_ore":round(opt_asset,2),
            "baseline_continuation_asset_value_ore":round(base_asset,2),
            "expected_saving_ore":round(econ_save,2),
            "expected_saving_sek":round(econ_save/100,2),
            "expected_saving_scope":"published_prices_plus_continuation_asset_value",
            "cash_cost_scope":"published_price_intervals_plus_battery_degradation",
            "priced_horizon_hours":diag["known_price_horizon_hours"],
            "unpriced_horizon_hours":diag["unknown_price_horizon_hours"],
            "terminal_soc_pct":round(term_soc,2),
            "terminal_soc_delta_pct":round(term_soc-soc,2),
            "terminal_soc_constraint_applied":term_applied,
            "battery_export_kwh":round(bexp,3),
            "discretionary_discharge_kwh":round(ddis,3),
            "discretionary_shift_hurdle_cost_ore":round(hurdle,2),
            "arbitrage_hurdle_cost_ore":round(hurdle,2),
            "reserve_policy_penalty_ore":round(reserve_padj,2),
            "preferred_max_excess_penalty_ore":round(upper_padj,2),
            "continuation_policy_adjustment_ore":round(continuation_padj,2),
            "policy_adjustment_ore":round(padj,2),
        },
        "rows":out,
    }
