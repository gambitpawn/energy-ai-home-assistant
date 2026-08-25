from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

from .db import DB_PATH

PLANNER_NAME = "deterministic_battery_dp_v3_1"
DT_HOURS = 0.25


def _parse_ts(value: str) -> datetime:
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _canonical_ts(value: str) -> str:
    return _parse_ts(value).replace(second=0, microsecond=0).isoformat()


def _latest_soc_pct() -> float | None:
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute("SELECT payload_json FROM raw_state ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    try:
        p = json.loads(row[0])
        item = p.get("battery_soc_pct") or {}
        if not item.get("available"):
            return None
        return float(item.get("state"))
    except Exception:
        return None


def _latest_load_rows() -> dict[str, dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute("SELECT MAX(generated_at) FROM load_forecast_15m").fetchone()
        generated = row[0] if row else None
        if not generated:
            return {}
        rows = c.execute(
            "SELECT start_utc,payload_json FROM load_forecast_15m WHERE generated_at=? ORDER BY start_utc",
            (generated,),
        ).fetchall()
    out = {}
    for start, payload in rows:
        try:
            out[_canonical_ts(str(start))] = json.loads(payload)
        except Exception:
            continue
    return out


def _latest_pv_rows() -> dict[str, dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute("SELECT MAX(generated_at) FROM pv_forecast_15m").fetchone()
        generated = row[0] if row else None
        if not generated:
            return {}
        rows = c.execute(
            "SELECT start_utc,payload_json,forecast_kw,uncertainty_kw FROM pv_forecast_15m WHERE generated_at=? ORDER BY start_utc",
            (generated,),
        ).fetchall()
    out = {}
    for start, payload, forecast_kw, uncertainty_kw in rows:
        item = {
            "pv_power_forecast_kw": float(forecast_kw),
            "pv_power_uncertainty_kw": float(uncertainty_kw),
        }
        if payload:
            try:
                item.update(json.loads(payload))
            except Exception:
                pass
        try:
            out[_canonical_ts(str(start))] = item
        except Exception:
            continue
    return out


def _price_rows() -> dict[str, float]:
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute("SELECT start_utc,price_ore_kwh FROM price_15m ORDER BY start_utc").fetchall()
    out = {}
    for start, price in rows:
        try:
            out[_canonical_ts(str(start))] = float(price)
        except Exception:
            continue
    return out


def _price_coverage(starts: list[str], prices: dict[str, float]) -> tuple[int, str | None, str | None]:
    """Return contiguous known-price coverage from the first physical interval."""
    known = 0
    for start in starts:
        if start not in prices:
            break
        known += 1
    if known == 0:
        return 0, None, starts[0] if starts else None
    last_known = starts[known - 1]
    first_unknown = starts[known] if known < len(starts) else None
    return known, last_known, first_unknown


def horizon_diagnostics(cfg: dict[str, Any]) -> dict[str, Any]:
    load = _latest_load_rows()
    pv = _latest_pv_rows()
    prices = _price_rows()
    matched = sorted(set(load).intersection(pv), key=_parse_ts)
    horizon_intervals = int((cfg.get("forecast") or {}).get("horizon_hours", 36)) * 4
    starts = matched[:horizon_intervals]
    known_count, last_known, first_unknown = _price_coverage(starts, prices)
    known_until = None
    if last_known:
        known_until = (_parse_ts(last_known) + timedelta(minutes=15)).isoformat()
    return {
        "load_intervals": len(load),
        "pv_intervals": len(pv),
        "price_intervals": len(prices),
        "matched_load_pv_intervals": len(matched),
        "first_load": min(load, key=_parse_ts) if load else None,
        "first_pv": min(pv, key=_parse_ts) if pv else None,
        "first_match": matched[0] if matched else None,
        "requested_horizon_intervals": horizon_intervals,
        "physical_horizon_intervals": len(starts),
        "known_price_intervals": known_count,
        "unknown_price_intervals": max(0, len(starts) - known_count),
        "known_price_horizon_hours": round(known_count * DT_HOURS, 2),
        "unknown_price_horizon_hours": round(max(0, len(starts) - known_count) * DT_HOURS, 2),
        "last_known_price_start": last_known,
        "known_price_until": known_until,
        "first_unknown_price_start": first_unknown,
        "price_fallback_used": False,
        "price_coverage_mode": "contiguous_published_intervals_only",
        "timestamp_join": "normalized_utc_minute",
    }


def _build_horizon(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    load = _latest_load_rows()
    pv = _latest_pv_rows()
    prices = _price_rows()
    starts = sorted(set(load).intersection(pv), key=_parse_ts)
    horizon_intervals = int((cfg.get("forecast") or {}).get("horizon_hours", 36)) * 4
    starts = starts[:horizon_intervals]
    if not starts:
        diag = horizon_diagnostics(cfg)
        raise RuntimeError(
            f"No overlapping load and PV forecast rows are available after UTC normalization; diagnostics={diag}"
        )

    known_count, _, _ = _price_coverage(starts, prices)
    rows = []
    for idx, start in enumerate(starts):
        stamp = _parse_ts(start)
        l = load[start]
        p = pv[start]
        price_known = idx < known_count
        price = prices[start] if price_known else None
        rows.append(
            {
                "start": stamp.isoformat(),
                "load_kw": float(l.get("house_load_forecast_kw") or l.get("total_forecast_kw") or 0.0),
                "base_load_kw": float(l.get("base_household_forecast_kw") or 0.0),
                "component_forecast_kw": l.get("component_forecast_kw") or {},
                "load_uncertainty_kw": float(l.get("house_load_uncertainty_kw") or 0.0),
                "pv_kw": float(p.get("pv_power_forecast_kw") or 0.0),
                "pv_uncertainty_kw": float(p.get("pv_power_uncertainty_kw") or 0.0),
                "price_known": price_known,
                "price_ore_kwh": float(price) if price is not None else None,
            }
        )
    return rows


def _state_grid(min_kwh: float, max_kwh: float, step_kwh: float, initial_kwh: float) -> list[float]:
    n = max(1, int(math.floor((max_kwh - min_kwh) / step_kwh)))
    states = [round(min_kwh + i * step_kwh, 6) for i in range(n + 1)]
    if states[-1] < max_kwh - 1e-6:
        states.append(round(max_kwh, 6))
    states.append(round(max(min_kwh, min(max_kwh, initial_kwh)), 6))
    return sorted(set(states))


def _transition_action_kw(e0: float, e1: float, eta_charge: float, eta_discharge: float) -> float:
    delta = e1 - e0
    if delta > 0:
        return -(delta / eta_charge) / DT_HOURS
    if delta < 0:
        return ((-delta) * eta_discharge) / DT_HOURS
    return 0.0


def _dynamic_reserve_kwh(
    row: dict[str, Any], cfg: dict[str, Any], capacity: float
) -> tuple[float, float]:
    policy = (cfg.get("policy") or {}).get("battery") or {}
    opt = cfg.get("optimizer") or {}
    normal_pct = float(policy.get("normal_reserve_soc_pct", 20.0))
    high_pct = float(policy.get("high_uncertainty_reserve_soc_pct", 28.0))
    full_scale_kw = max(0.01, float(opt.get("reserve_uncertainty_full_scale_kw", 3.0)))
    uncertainty = max(0.0, float(row.get("load_uncertainty_kw") or 0.0)) + max(
        0.0, float(row.get("pv_uncertainty_kw") or 0.0)
    )
    ratio = min(1.0, uncertainty / full_scale_kw)
    reserve_pct = normal_pct + (high_pct - normal_pct) * ratio
    return capacity * reserve_pct / 100.0, reserve_pct


def _continuation_profile(
    rows: list[dict[str, Any]],
    cfg: dict[str, Any],
    capacity: float,
    preferred_max_kwh: float,
    eta_discharge: float,
) -> dict[str, Any]:
    opt = cfg.get("optimizer") or {}
    econ = (cfg.get("policy") or {}).get("economics") or {}
    unknown = [r for r in rows if not r.get("price_known")]
    known = [r for r in rows if r.get("price_known")]
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

    import_limit = float(opt.get("physical_grid_import_limit_kw", 13.8))
    coverage_fraction = max(
        0.0, min(1.0, float(opt.get("unknown_price_energy_coverage_fraction", 0.35)))
    )
    risk_premium_max = max(
        0.0, float(opt.get("unknown_price_risk_premium_ore_kwh", 40.0))
    )
    default_value = max(
        0.0, float(opt.get("unknown_price_default_continuation_value_ore_kwh", 150.0))
    )
    full_scale_kw = max(0.01, float(opt.get("reserve_uncertainty_full_scale_kw", 3.0)))

    reserve_values = [_dynamic_reserve_kwh(r, cfg, capacity)[0] for r in unknown]
    reserve_target_kwh = max(reserve_values) if reserve_values else 0.0
    net_deficit_kwh = sum(
        max(0.0, float(r["load_kw"]) - float(r["pv_kw"])) * DT_HOURS for r in unknown
    )
    peak_support_kwh = sum(
        max(0.0, float(r["load_kw"]) - float(r["pv_kw"]) - import_limit)
        * DT_HOURS
        / max(0.01, eta_discharge)
        for r in unknown
    )
    covered_energy_kwh = net_deficit_kwh * coverage_fraction / max(0.01, eta_discharge)
    target_kwh = min(
        preferred_max_kwh,
        max(
            reserve_target_kwh + covered_energy_kwh,
            reserve_target_kwh + peak_support_kwh,
        ),
    )

    known_buy_prices = [
        float(r["price_ore_kwh"]) + float(econ.get("import_overhead_ore_kwh", 0.0))
        for r in known
        if r.get("price_ore_kwh") is not None
    ]
    reference = float(median(known_buy_prices)) if known_buy_prices else default_value
    avg_uncertainty = (
        sum(
            max(0.0, float(r.get("load_uncertainty_kw") or 0.0))
            + max(0.0, float(r.get("pv_uncertainty_kw") or 0.0))
            for r in unknown
        )
        / len(unknown)
    )
    load_pressure = min(1.0, net_deficit_kwh / max(0.01, capacity))
    uncertainty_pressure = min(1.0, avg_uncertainty / full_scale_kw)
    risk_premium = risk_premium_max * (0.6 * load_pressure + 0.4 * uncertainty_pressure)
    continuation_value = reference + risk_premium

    return {
        "enabled": True,
        "target_kwh": target_kwh,
        "target_soc_pct": target_kwh / capacity * 100.0,
        "value_ore_per_kwh": continuation_value,
        "unknown_net_deficit_kwh": net_deficit_kwh,
        "unknown_peak_support_kwh": peak_support_kwh,
        "reference_price_ore_kwh": reference,
        "risk_premium_ore_kwh": risk_premium,
        "coverage_fraction": coverage_fraction,
    }


def _interval_result(
    row: dict[str, Any], action_kw: float, cfg: dict[str, Any]
) -> dict[str, float]:
    opt = cfg.get("optimizer") or {}
    econ = (cfg.get("policy") or {}).get("economics") or {}
    import_limit = float(opt.get("physical_grid_import_limit_kw", 13.8))
    export_limit = float(opt.get("grid_export_limit_kw", 10.0))
    import_overhead = float(econ.get("import_overhead_ore_kwh", 0.0))
    export_overhead = float(econ.get("export_overhead_ore_kwh", 0.0))
    degradation = float(opt.get("battery_degradation_ore_kwh", 5.0))
    minimum_shift_margin = float(econ.get("minimum_arbitrage_margin_ore_kwh", 20.0))

    load_kw = float(row["load_kw"])
    pv_kw = float(row["pv_kw"])
    net_before_battery = load_kw - pv_kw
    grid_net = net_before_battery - action_kw
    grid_import = max(0.0, grid_net)
    raw_export = max(0.0, -grid_net)
    grid_export = min(raw_export, export_limit)
    curtailed = max(0.0, raw_export - export_limit)

    pv_surplus = max(0.0, pv_kw - load_kw)
    charge_kw = max(0.0, -action_kw)
    pv_charge_kw = min(charge_kw, pv_surplus)
    grid_charge_kw = max(0.0, charge_kw - pv_charge_kw)

    battery_export_kw = 0.0
    if action_kw > 0.0 and grid_export > 0.0:
        discharge_needed_for_self_consumption = max(0.0, net_before_battery)
        battery_export_kw = min(
            grid_export, max(0.0, action_kw - discharge_needed_for_self_consumption)
        )

    required_physical_discharge_kw = max(0.0, net_before_battery - import_limit)
    discretionary_discharge_kw = 0.0
    if action_kw > 0.0:
        discretionary_discharge_kw = max(0.0, action_kw - required_physical_discharge_kw)

    feasible = grid_import <= import_limit + 1e-9
    price_known = bool(row.get("price_known"))

    if not price_known:
        # Beyond published prices we keep a physical horizon, not a synthetic economic one.
        # No speculative grid charging or battery-to-grid arbitrage is allowed.
        if grid_charge_kw > 1e-6 or battery_export_kw > 1e-6:
            feasible = False
        # Battery discharge is reserved for a physical import-limit need; otherwise hold.
        if required_physical_discharge_kw <= 1e-6 and action_kw > 1e-6:
            feasible = False
        energy_cost = 0.0
        degradation_cost = abs(action_kw) * DT_HOURS * degradation
        cash_cost = degradation_cost
        discretionary_shift_hurdle_cost = 0.0
        objective_cost = cash_cost
    else:
        price = float(row["price_ore_kwh"])
        buy = price + import_overhead
        sell = max(0.0, price - export_overhead)
        energy_cost = grid_import * DT_HOURS * buy - grid_export * DT_HOURS * sell
        degradation_cost = abs(action_kw) * DT_HOURS * degradation
        # The minimum margin applies to every discretionary use of stored energy,
        # whether it offsets household import or is exported. Physical fuse support
        # is exempt because it is a constraint, not an arbitrage decision.
        discretionary_shift_hurdle_cost = (
            discretionary_discharge_kw * DT_HOURS * minimum_shift_margin
        )
        cash_cost = energy_cost + degradation_cost
        objective_cost = cash_cost + discretionary_shift_hurdle_cost

    return {
        "feasible": 1.0 if feasible else 0.0,
        "grid_import_kw": grid_import,
        "grid_export_kw": grid_export,
        "battery_export_kw": battery_export_kw,
        "pv_charge_kw": pv_charge_kw,
        "grid_charge_kw": grid_charge_kw,
        "required_physical_discharge_kw": required_physical_discharge_kw,
        "discretionary_discharge_kw": discretionary_discharge_kw,
        "curtailed_kw": curtailed,
        "energy_cost_ore": energy_cost,
        "degradation_cost_ore": degradation_cost,
        "cash_cost_ore": cash_cost,
        "discretionary_shift_hurdle_cost_ore": discretionary_shift_hurdle_cost,
        # Legacy alias retained in stored payloads/history readers.
        "arbitrage_hurdle_cost_ore": discretionary_shift_hurdle_cost,
        "interval_cost_ore": objective_cost,
    }


def _baseline_cost(rows: list[dict[str, Any]], cfg: dict[str, Any]) -> float:
    return sum(_interval_result(r, 0.0, cfg)["cash_cost_ore"] for r in rows)


def _classify_action(
    row: dict[str, Any], action_kw: float, result: dict[str, float]
) -> tuple[str, dict[str, float]]:
    if not row.get("price_known"):
        if action_kw > 0.05:
            return "physical_limit_discharge", {
                "battery_to_load_kw": action_kw,
                "battery_to_grid_kw": 0.0,
            }
        if action_kw < -0.05:
            return "pv_charge_unpriced", {
                "pv_to_battery_kw": result.get("pv_charge_kw", 0.0),
                "grid_to_battery_kw": 0.0,
            }
        return "hold_unpriced", {}

    if action_kw > 0.05:
        if result.get("battery_export_kw", 0.0) > 0.05:
            return "export_arbitrage", {
                "battery_to_grid_kw": result.get("battery_export_kw", 0.0),
                "battery_to_load_kw": max(
                    0.0, action_kw - result.get("battery_export_kw", 0.0)
                ),
            }
        return "self_consumption_discharge", {
            "battery_to_load_kw": action_kw,
            "battery_to_grid_kw": 0.0,
        }
    if action_kw < -0.05:
        pv_charge_kw = result.get("pv_charge_kw", 0.0)
        grid_charge_kw = result.get("grid_charge_kw", 0.0)
        if pv_charge_kw > 0.05 and grid_charge_kw > 0.05:
            reason = "mixed_charge"
        elif pv_charge_kw > 0.05:
            reason = "pv_charge"
        else:
            reason = "grid_charge"
        return reason, {
            "pv_to_battery_kw": pv_charge_kw,
            "grid_to_battery_kw": grid_charge_kw,
        }
    return "hold", {}


def build_plan(cfg: dict[str, Any]) -> dict[str, Any]:
    rows = _build_horizon(cfg)
    policy = (cfg.get("policy") or {}).get("battery") or {}
    opt = cfg.get("optimizer") or {}
    capacity = float(policy.get("capacity_kwh", 19.6))
    hard_min_pct = float(policy.get("hard_min_soc_pct", 5.0))
    hard_max_pct = float(policy.get("hard_max_soc_pct", 100.0))
    preferred_min_pct = float(policy.get("preferred_min_soc_pct", 15.0))
    preferred_max_pct = float(policy.get("preferred_max_soc_pct", 90.0))
    normal_reserve_pct = float(policy.get("normal_reserve_soc_pct", 20.0))
    high_reserve_pct = float(policy.get("high_uncertainty_reserve_soc_pct", 28.0))
    max_charge_kw = float(opt.get("battery_max_charge_kw", 8.0))
    max_discharge_kw = float(opt.get("battery_max_discharge_kw", 8.0))
    eta_charge = float(opt.get("battery_charge_efficiency", 0.95))
    eta_discharge = float(opt.get("battery_discharge_efficiency", 0.95))
    state_step = float(opt.get("soc_grid_step_kwh", 0.5))
    reserve_penalty = float(opt.get("reserve_penalty_ore_per_kwh", 100.0))
    terminal_tolerance_pct = float(opt.get("terminal_soc_tolerance_pct", 3.0))
    terminal_tiebreak_ore_kwh = float(
        opt.get("terminal_soc_tiebreak_ore_per_kwh", 5.0)
    )

    soc = _latest_soc_pct()
    if soc is None:
        raise RuntimeError("Current battery SOC is unavailable")
    initial_kwh = capacity * soc / 100.0
    min_kwh = capacity * hard_min_pct / 100.0
    max_kwh = capacity * hard_max_pct / 100.0
    preferred_min_kwh = capacity * preferred_min_pct / 100.0
    preferred_max_kwh = capacity * preferred_max_pct / 100.0
    states = _state_grid(min_kwh, max_kwh, state_step, initial_kwh)
    initial_idx = min(range(len(states)), key=lambda i: abs(states[i] - initial_kwh))

    continuation = _continuation_profile(
        rows, cfg, capacity, preferred_max_kwh, eta_discharge
    )
    known_count = sum(1 for r in rows if r.get("price_known"))
    boundary_idx = known_count - 1 if 0 < known_count < len(rows) else None

    costs = {initial_idx: 0.0}
    parents: list[
        dict[int, tuple[int, float, dict[str, float], float, float, float]]
    ] = []

    for t, row in enumerate(rows):
        reserve_kwh, reserve_pct = _dynamic_reserve_kwh(row, cfg, capacity)
        next_costs: dict[int, float] = {}
        back: dict[int, tuple[int, float, dict[str, float], float, float, float]] = {}
        for i0, prior_cost in costs.items():
            e0 = states[i0]
            for i1, e1 in enumerate(states):
                action = _transition_action_kw(e0, e1, eta_charge, eta_discharge)
                if action < -max_charge_kw - 1e-9 or action > max_discharge_kw + 1e-9:
                    continue
                result = _interval_result(row, action, cfg)
                if not result["feasible"]:
                    continue

                policy_adjustment = 0.0
                if e1 < reserve_kwh:
                    policy_adjustment += (reserve_kwh - e1) * reserve_penalty
                if e1 < preferred_min_kwh:
                    policy_adjustment += (
                        (preferred_min_kwh - e1) * reserve_penalty * 0.10
                    )
                if e1 > preferred_max_kwh:
                    policy_adjustment += (
                        (e1 - preferred_max_kwh) * reserve_penalty * 0.02
                    )

                if boundary_idx is not None and t == boundary_idx:
                    target_kwh = float(continuation.get("target_kwh") or 0.0)
                    reference_value = float(
                        continuation.get("reference_price_ore_kwh") or 0.0
                    )
                    risk_premium = float(
                        continuation.get("risk_premium_ore_kwh") or 0.0
                    )
                    # Every stored kWh retains a base continuation value. Energy up to
                    # the target also carries an extra risk premium.
                    policy_adjustment -= e1 * reference_value
                    if e1 < target_kwh:
                        policy_adjustment += (target_kwh - e1) * risk_premium

                total = prior_cost + result["interval_cost_ore"] + policy_adjustment
                if i1 not in next_costs or total < next_costs[i1]:
                    next_costs[i1] = total
                    back[i1] = (
                        i0,
                        action,
                        result,
                        reserve_kwh,
                        reserve_pct,
                        policy_adjustment,
                    )
        if not next_costs:
            raise RuntimeError(
                f"No feasible optimizer states at {row['start']}; check grid/battery limits"
            )
        costs = next_costs
        parents.append(back)

    if continuation.get("enabled"):
        # When price coverage ends inside the physical horizon, the continuation target
        # replaces the artificial end-of-horizon SOC constraint.
        best_idx = min(costs, key=lambda idx: costs[idx])
        terminal_constraint_applied = False
    else:
        tolerance_kwh = capacity * terminal_tolerance_pct / 100.0
        terminal_candidates = [
            idx for idx in costs if abs(states[idx] - initial_kwh) <= tolerance_kwh + 1e-9
        ]
        if not terminal_candidates:
            nearest_delta = min(abs(states[idx] - initial_kwh) for idx in costs)
            terminal_candidates = [
                idx
                for idx in costs
                if abs(abs(states[idx] - initial_kwh) - nearest_delta) <= 1e-9
            ]
        best_idx = min(
            terminal_candidates,
            key=lambda idx: costs[idx]
            + abs(states[idx] - initial_kwh) * terminal_tiebreak_ore_kwh,
        )
        terminal_constraint_applied = True

    path: list[
        tuple[int, int, float, dict[str, float], float, float, float]
    ] = []
    idx = best_idx
    for t in range(len(rows) - 1, -1, -1):
        prev_idx, action, result, reserve_kwh, reserve_pct, policy_adjustment = parents[
            t
        ][idx]
        path.append(
            (
                prev_idx,
                idx,
                action,
                result,
                reserve_kwh,
                reserve_pct,
                policy_adjustment,
            )
        )
        idx = prev_idx
    path.reverse()

    out_rows = []
    objective_cost = 0.0
    cash_cost = 0.0
    total_battery_export_kwh = 0.0
    total_discretionary_discharge_kwh = 0.0
    total_shift_hurdle_ore = 0.0
    total_policy_adjustment_ore = 0.0
    boundary_soc_pct = None

    for t, (row, (_, i1, action, result, reserve_kwh, reserve_pct, policy_adjustment)) in enumerate(
        zip(rows, path)
    ):
        objective_cost += result["interval_cost_ore"] + policy_adjustment
        cash_cost += result["cash_cost_ore"]
        total_battery_export_kwh += (
            result.get("battery_export_kw", 0.0) * DT_HOURS
        )
        total_discretionary_discharge_kwh += (
            result.get("discretionary_discharge_kw", 0.0) * DT_HOURS
        )
        total_shift_hurdle_ore += result.get(
            "discretionary_shift_hurdle_cost_ore", 0.0
        )
        total_policy_adjustment_ore += policy_adjustment
        if boundary_idx is not None and t == boundary_idx:
            boundary_soc_pct = states[i1] / capacity * 100.0

        reason, flow = _classify_action(row, action, result)
        out_rows.append(
            {
                **row,
                "battery_action_kw": round(action, 4),
                "expected_soc_pct": round(states[i1] / capacity * 100.0, 2),
                "reserve_soc_pct": round(reserve_pct, 2),
                "grid_import_kw": round(result["grid_import_kw"], 4),
                "grid_export_kw": round(result["grid_export_kw"], 4),
                "battery_export_kw": round(
                    result.get("battery_export_kw", 0.0), 4
                ),
                "required_physical_discharge_kw": round(
                    result.get("required_physical_discharge_kw", 0.0), 4
                ),
                "discretionary_discharge_kw": round(
                    result.get("discretionary_discharge_kw", 0.0), 4
                ),
                "curtailed_kw": round(result["curtailed_kw"], 4),
                "energy_cost_ore": (
                    round(result["energy_cost_ore"], 4)
                    if row.get("price_known")
                    else None
                ),
                "degradation_cost_ore": round(
                    result["degradation_cost_ore"], 4
                ),
                "cash_cost_ore": round(result["cash_cost_ore"], 4),
                "discretionary_shift_hurdle_cost_ore": round(
                    result.get("discretionary_shift_hurdle_cost_ore", 0.0), 4
                ),
                "arbitrage_hurdle_cost_ore": round(
                    result.get("discretionary_shift_hurdle_cost_ore", 0.0), 4
                ),
                "policy_adjustment_ore": round(policy_adjustment, 4),
                "objective_cost_ore": round(
                    result["interval_cost_ore"] + policy_adjustment, 4
                ),
                "reason": reason,
                "flow_breakdown_kw": {
                    k: round(v, 4) for k, v in flow.items()
                },
            }
        )

    baseline = _baseline_cost(rows, cfg)
    generated = datetime.now(timezone.utc).isoformat()
    terminal_soc_pct = states[best_idx] / capacity * 100.0
    terminal_delta_pct = terminal_soc_pct - soc
    diag = horizon_diagnostics(cfg)

    continuation_reference = float(
        continuation.get("reference_price_ore_kwh") or 0.0
    )
    continuation_risk = float(
        continuation.get("risk_premium_ore_kwh") or 0.0
    )
    continuation_target_kwh = float(continuation.get("target_kwh") or 0.0)
    boundary_kwh = (
        capacity * boundary_soc_pct / 100.0
        if boundary_soc_pct is not None
        else states[best_idx]
    )
    optimized_continuation_asset_ore = 0.0
    baseline_continuation_asset_ore = 0.0
    if continuation.get("enabled"):
        optimized_continuation_asset_ore = (
            boundary_kwh * continuation_reference
            + min(boundary_kwh, continuation_target_kwh) * continuation_risk
        )
        baseline_continuation_asset_ore = (
            initial_kwh * continuation_reference
            + min(initial_kwh, continuation_target_kwh) * continuation_risk
        )

    cash_saving_ore = baseline - cash_cost
    economic_saving_ore = (
        cash_saving_ore
        + optimized_continuation_asset_ore
        - baseline_continuation_asset_ore
    )

    return {
        "generated_at": generated,
        "planner": PLANNER_NAME,
        "mode": "shadow_read_only",
        "interval_minutes": 15,
        "horizon_hours": len(rows) // 4,
        "initial_soc_pct": round(soc, 2),
        "horizon_diagnostics": diag,
        "constraints": {
            "battery_capacity_kwh": capacity,
            "hard_min_soc_pct": hard_min_pct,
            "hard_max_soc_pct": hard_max_pct,
            "preferred_min_soc_pct": preferred_min_pct,
            "preferred_max_soc_pct": preferred_max_pct,
            "normal_reserve_soc_pct": normal_reserve_pct,
            "high_uncertainty_reserve_soc_pct": high_reserve_pct,
            "reserve_uncertainty_full_scale_kw": float(
                opt.get("reserve_uncertainty_full_scale_kw", 3.0)
            ),
            "battery_max_charge_kw": max_charge_kw,
            "battery_max_discharge_kw": max_discharge_kw,
            "physical_grid_import_limit_kw": float(
                opt.get("physical_grid_import_limit_kw", 13.8)
            ),
            "grid_export_limit_kw": float(opt.get("grid_export_limit_kw", 10.0)),
            "charge_efficiency": eta_charge,
            "discharge_efficiency": eta_discharge,
            "soc_grid_step_kwh": state_step,
            "terminal_soc_tolerance_pct": terminal_tolerance_pct,
        },
        "objective": {
            "energy_cost_on_published_prices_only": True,
            "battery_degradation_cost": True,
            "dynamic_uncertainty_reserve": True,
            "fair_terminal_soc_constraint_when_fully_priced": True,
            "battery_export_arbitrage": True,
            "minimum_net_discretionary_shift_margin_ore_kwh": float(
                ((cfg.get("policy") or {}).get("economics") or {}).get(
                    "minimum_arbitrage_margin_ore_kwh", 20.0
                )
            ),
            "discretionary_self_consumption_hurdle": True,
            "physical_limit_discharge_exempt_from_margin": True,
            "grid_import_soft_target": False,
            "variable_price_horizon": True,
            "unknown_price_grid_charging": False,
            "unknown_price_battery_export": False,
            "continuation_value_from_physical_forecast": True,
            "component_forecasts_included": True,
        },
        "continuation": {
            "enabled": bool(continuation.get("enabled")),
            "price_boundary_soc_pct": (
                round(boundary_soc_pct, 2)
                if boundary_soc_pct is not None
                else None
            ),
            "target_soc_pct": (
                round(float(continuation["target_soc_pct"]), 2)
                if continuation.get("target_soc_pct") is not None
                else None
            ),
            "value_ore_per_kwh": (
                round(float(continuation["value_ore_per_kwh"]), 2)
                if continuation.get("value_ore_per_kwh") is not None
                else None
            ),
            "reference_price_ore_kwh": (
                round(float(continuation["reference_price_ore_kwh"]), 2)
                if continuation.get("reference_price_ore_kwh") is not None
                else None
            ),
            "risk_premium_ore_kwh": round(
                float(continuation.get("risk_premium_ore_kwh") or 0.0), 2
            ),
            "unknown_net_deficit_kwh": round(
                float(continuation.get("unknown_net_deficit_kwh") or 0.0), 3
            ),
            "unknown_peak_support_kwh": round(
                float(continuation.get("unknown_peak_support_kwh") or 0.0), 3
            ),
            "energy_coverage_fraction": round(
                float(continuation.get("coverage_fraction") or 0.0), 3
            ),
        },
        "summary": {
            "objective_cost_ore": round(objective_cost, 2),
            "expected_cash_cost_ore": round(cash_cost, 2),
            "baseline_cash_cost_ore": round(baseline, 2),
            "expected_cash_saving_ore": round(cash_saving_ore, 2),
            "expected_cash_saving_sek": round(cash_saving_ore / 100.0, 2),
            "optimized_continuation_asset_value_ore": round(
                optimized_continuation_asset_ore, 2
            ),
            "baseline_continuation_asset_value_ore": round(
                baseline_continuation_asset_ore, 2
            ),
            "expected_saving_ore": round(economic_saving_ore, 2),
            "expected_saving_sek": round(economic_saving_ore / 100.0, 2),
            "expected_saving_scope": "published_prices_plus_continuation_asset_value",
            "cash_cost_scope": "published_price_intervals_plus_battery_degradation",
            "priced_horizon_hours": diag["known_price_horizon_hours"],
            "unpriced_horizon_hours": diag["unknown_price_horizon_hours"],
            "terminal_soc_pct": round(terminal_soc_pct, 2),
            "terminal_soc_delta_pct": round(terminal_delta_pct, 2),
            "terminal_soc_constraint_applied": terminal_constraint_applied,
            "battery_export_kwh": round(total_battery_export_kwh, 3),
            "discretionary_discharge_kwh": round(
                total_discretionary_discharge_kwh, 3
            ),
            "discretionary_shift_hurdle_cost_ore": round(
                total_shift_hurdle_ore, 2
            ),
            "arbitrage_hurdle_cost_ore": round(total_shift_hurdle_ore, 2),
            "policy_adjustment_ore": round(total_policy_adjustment_ore, 2),
        },
        "rows": out_rows,
    }
