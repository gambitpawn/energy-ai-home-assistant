from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from .engine_contract import EngineInput
from .tariff_scenarios import LOCAL_TZ, _calendar_active

FEATURE_SCHEMA = "neural_v1_features_v2"
BLOCK_INTERVALS = 8
BLOCK_COUNT = 18
BLOCK_FEATURES = (
    "load_mean_kw", "pv_mean_kw", "net_mean_kw", "uncertainty_mean_kw",
    "known_price_mean_ore_kwh", "price_known_fraction",
    "consumption_tariff_active_fraction", "production_tariff_active_fraction",
)
SYSTEM_FEATURES = (
    "battery_capacity_kwh", "pv_capacity_kw", "ev_max_power_kw",
    "battery_max_charge_kw", "battery_max_discharge_kw", "physical_grid_import_limit_kw", "grid_export_limit_kw",
    "charge_efficiency", "discharge_efficiency", "hard_min_soc_pct", "hard_max_soc_pct",
    "preferred_min_soc_pct", "preferred_max_soc_pct", "normal_reserve_soc_pct", "high_uncertainty_reserve_soc_pct",
    "reserve_uncertainty_full_scale_kw", "reserve_critical_soc_pct",
    "reserve_critical_penalty_ore_per_kwh_hour", "reserve_preferred_penalty_ore_per_kwh_hour",
    "reserve_target_penalty_ore_per_kwh_hour", "preferred_max_excess_penalty_ore_per_kwh_hour",
    "terminal_soc_tolerance_pct", "import_overhead_ore_kwh", "export_overhead_ore_kwh",
    "minimum_arbitrage_margin_ore_kwh", "battery_degradation_ore_kwh",
    "unknown_price_energy_coverage_fraction", "unknown_price_risk_premium_ore_kwh",
    "unknown_price_default_continuation_value_ore_kwh",
    "tariffs_enabled", "consumption_demand_enabled", "consumption_rate_sek_per_kw",
    "consumption_start_hour", "consumption_end_hour", "consumption_top_n",
    "consumption_active_month_at_decision", "consumption_active_day_at_decision", "consumption_active_at_decision",
    "consumption_historical_metric_kw", "consumption_historical_peak1_kw", "consumption_historical_peak2_kw",
    "consumption_historical_peak3_kw", "consumption_current_hour_average_kw_so_far",
    "consumption_current_hour_quarters_elapsed", "production_demand_enabled", "production_rate_sek_per_kw",
    "production_start_hour", "production_end_hour", "production_active_month_at_decision",
    "production_active_day_at_decision", "production_active_at_decision", "production_historical_metric_kw",
    "production_historical_peak_kw", "production_current_hour_average_kw_so_far",
    "production_current_hour_quarters_elapsed",
)

def _mean(values): return sum(values) / len(values) if values else 0.0
def _f(value, default=0.0):
    try: return float(default if value is None else value)
    except (TypeError, ValueError): return float(default)
def _b(value): return 1.0 if bool(value) else 0.0
def _top(values, index):
    seq=list(values or []); return _f(seq[index]) if index < len(seq) else 0.0

def _tariff_active_fraction(chunk, tariff, enabled):
    if not enabled or not chunk: return 0.0
    active=0
    for row in chunk:
        try:
            dt=datetime.fromisoformat(str(row["start"]).replace("Z", "+00:00")).astimezone(LOCAL_TZ)
            if _calendar_active(dt, tariff, False): active += 1
        except Exception: continue
    return active / float(len(chunk))

def feature_names():
    names=["initial_soc_pct","decision_hour_sin","decision_hour_cos","decision_dow_sin","decision_dow_cos",
           "horizon_fraction","price_known_fraction","known_price_min_ore_kwh","known_price_max_ore_kwh","known_price_spread_ore_kwh",
           "forecast_load_energy_kwh","forecast_pv_energy_kwh","forecast_net_energy_kwh","mean_load_uncertainty_kw","mean_pv_uncertainty_kw",*SYSTEM_FEATURES]
    for block in range(BLOCK_COUNT):
        for name in BLOCK_FEATURES: names.append(f"b{block:02d}_{name}")
    return names
FEATURE_NAMES=tuple(feature_names())

def _system_vector(engine_input):
    constraints=engine_input.constraints or {}; objective=engine_input.objective or {}; installation=objective.get("installation") or {}
    economics=objective.get("economics") or {}; tariffs=objective.get("tariffs") or {}; tariff_state=objective.get("tariff_state") or {}
    consumption=tariffs.get("consumption_demand") or {}; production=tariffs.get("production_demand") or {}
    cstate=tariff_state.get("consumption_demand") or {}; pstate=tariff_state.get("production_demand") or {}
    tariffs_enabled=bool(tariffs.get("enabled")); c_enabled=tariffs_enabled and bool(consumption.get("enabled")); p_enabled=tariffs_enabled and bool(production.get("enabled"))
    return [
        _f(installation.get("battery_capacity_kwh",constraints.get("battery_capacity_kwh",19.6))),_f(installation.get("pv_capacity_kw")),_f(installation.get("ev_max_power_kw")),
        _f(installation.get("battery_max_charge_kw",constraints.get("battery_max_charge_kw",8.0))),_f(installation.get("battery_max_discharge_kw",constraints.get("battery_max_discharge_kw",8.0))),
        _f(installation.get("physical_grid_import_limit_kw",constraints.get("physical_grid_import_limit_kw",13.8))),_f(installation.get("grid_export_limit_kw",constraints.get("grid_export_limit_kw",10.0))),
        _f(installation.get("charge_efficiency",constraints.get("charge_efficiency",0.95))),_f(installation.get("discharge_efficiency",constraints.get("discharge_efficiency",0.95))),
        _f(constraints.get("hard_min_soc_pct",5)),_f(constraints.get("hard_max_soc_pct",100)),_f(constraints.get("preferred_min_soc_pct",15)),_f(constraints.get("preferred_max_soc_pct",90)),
        _f(constraints.get("normal_reserve_soc_pct",20)),_f(constraints.get("high_uncertainty_reserve_soc_pct",28)),_f(constraints.get("reserve_uncertainty_full_scale_kw",3)),
        _f(constraints.get("reserve_critical_soc_pct",10)),_f(constraints.get("reserve_critical_penalty_ore_per_kwh_hour",300)),_f(constraints.get("reserve_preferred_penalty_ore_per_kwh_hour",100)),
        _f(constraints.get("reserve_target_penalty_ore_per_kwh_hour",10)),_f(constraints.get("preferred_max_excess_penalty_ore_per_kwh_hour",2)),_f(constraints.get("terminal_soc_tolerance_pct",3)),
        _f(economics.get("import_overhead_ore_kwh")),_f(economics.get("export_overhead_ore_kwh")),_f(economics.get("minimum_arbitrage_margin_ore_kwh",20)),_f(economics.get("battery_degradation_ore_kwh",5)),
        _f(installation.get("unknown_price_energy_coverage_fraction",.35)),_f(installation.get("unknown_price_risk_premium_ore_kwh",40)),_f(installation.get("unknown_price_default_continuation_value_ore_kwh",150)),
        _b(tariffs_enabled),_b(c_enabled),_f(consumption.get("rate_sek_per_kw")),_f(consumption.get("start_hour")),_f(consumption.get("end_hour")),_f(consumption.get("top_n",3)),
        _b(cstate.get("active_month_at_decision")),_b(cstate.get("active_day_at_decision")),_b(cstate.get("active_at_decision")),_f(cstate.get("historical_metric_kw")),
        _top(cstate.get("historical_top_values_kw"),0),_top(cstate.get("historical_top_values_kw"),1),_top(cstate.get("historical_top_values_kw"),2),
        _f(cstate.get("current_clock_hour_average_kw_so_far")),_f(cstate.get("current_clock_hour_quarters_elapsed")),
        _b(p_enabled),_f(production.get("rate_sek_per_kw")),_f(production.get("start_hour")),_f(production.get("end_hour")),_b(pstate.get("active_month_at_decision")),
        _b(pstate.get("active_day_at_decision")),_b(pstate.get("active_at_decision")),_f(pstate.get("historical_metric_kw")),_top(pstate.get("historical_top_values_kw"),0),
        _f(pstate.get("current_clock_hour_average_kw_so_far")),_f(pstate.get("current_clock_hour_quarters_elapsed")),
    ]

def vectorize(engine_input):
    rows=list(engine_input.horizon_rows)[:BLOCK_COUNT*BLOCK_INTERVALS]; dt_h=float(engine_input.interval_minutes)/60.0
    decision=datetime.fromisoformat(engine_input.decision_start.replace("Z","+00:00")).astimezone(LOCAL_TZ); hour=decision.hour+decision.minute/60.0; dow=float(decision.weekday())
    loads=[float(r.get("load_kw") or 0) for r in rows]; pvs=[float(r.get("pv_kw") or 0) for r in rows]
    load_u=[float(r.get("load_uncertainty_kw") or 0) for r in rows]; pv_u=[float(r.get("pv_uncertainty_kw") or 0) for r in rows]
    known=[float(r["price_ore_kwh"]) for r in rows if bool(r.get("price_known")) and r.get("price_ore_kwh") is not None]
    kf=len(known)/max(1,len(rows)); pmin=min(known) if known else 0.; pmax=max(known) if known else 0.
    out=[float(engine_input.initial_soc_pct),math.sin(2*math.pi*hour/24),math.cos(2*math.pi*hour/24),math.sin(2*math.pi*dow/7),math.cos(2*math.pi*dow/7),
         len(rows)/float(BLOCK_COUNT*BLOCK_INTERVALS),kf,pmin,pmax,pmax-pmin,sum(loads)*dt_h,sum(pvs)*dt_h,sum(l-p for l,p in zip(loads,pvs))*dt_h,_mean(load_u),_mean(pv_u),*_system_vector(engine_input)]
    tariffs=(engine_input.objective or {}).get("tariffs") or {}; te=bool(tariffs.get("enabled")); consumption=tariffs.get("consumption_demand") or {}; production=tariffs.get("production_demand") or {}
    ce=te and bool(consumption.get("enabled")); pe=te and bool(production.get("enabled"))
    for block in range(BLOCK_COUNT):
        chunk=rows[block*BLOCK_INTERVALS:(block+1)*BLOCK_INTERVALS]
        if not chunk: out.extend([0.0]*len(BLOCK_FEATURES)); continue
        bl=[float(r.get("load_kw") or 0) for r in chunk]; bp=[float(r.get("pv_kw") or 0) for r in chunk]
        bu=[float(r.get("load_uncertainty_kw") or 0)+float(r.get("pv_uncertainty_kw") or 0) for r in chunk]
        bk=[float(r["price_ore_kwh"]) for r in chunk if bool(r.get("price_known")) and r.get("price_ore_kwh") is not None]
        out.extend([_mean(bl),_mean(bp),_mean([l-p for l,p in zip(bl,bp)]),_mean(bu),_mean(bk),len(bk)/float(len(chunk)),
                    _tariff_active_fraction(chunk,consumption,ce),_tariff_active_fraction(chunk,production,pe)])
    if len(out)!=len(FEATURE_NAMES): raise RuntimeError(f"feature vector length mismatch: {len(out)} != {len(FEATURE_NAMES)}")
    return [float(x) for x in out]

def feature_metadata():
    return {"schema":FEATURE_SCHEMA,"feature_count":len(FEATURE_NAMES),"global_forecast_features":15,"system_policy_tariff_features":len(SYSTEM_FEATURES),
            "block_interval_count":BLOCK_INTERVALS,"block_count":BLOCK_COUNT,"block_feature_count":len(BLOCK_FEATURES),"block_hours":BLOCK_INTERVALS*.25,
            "maximum_horizon_hours":BLOCK_COUNT*BLOCK_INTERVALS*.25,"feature_names":list(FEATURE_NAMES)}
