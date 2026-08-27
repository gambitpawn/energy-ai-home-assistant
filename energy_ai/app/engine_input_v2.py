from __future__ import annotations

from typing import Any

from .engine_contract import EngineInput, common_constraints_from_plan, common_objective_from_cfg, normalize_horizon_row
from .engine_tariff_state import tariff_state_for_decision


def enriched_common_objective(
    cfg: dict[str, Any],
    decision_start: str,
    information_as_of: str,
) -> dict[str, Any]:
    objective = common_objective_from_cfg(cfg)
    battery = (cfg.get("policy") or {}).get("battery") or {}
    ev = (cfg.get("policy") or {}).get("ev") or {}
    pv = ((cfg.get("forecast") or {}).get("pv") or {})
    optimizer = cfg.get("optimizer") or {}
    objective["installation"] = {
        "battery_capacity_kwh": float(battery.get("capacity_kwh", 19.6)),
        "pv_capacity_kw": float(pv.get("capacity_kw", 0.0)),
        "ev_max_power_kw": float(ev.get("max_power_kw", 0.0)),
        "battery_max_charge_kw": float(optimizer.get("battery_max_charge_kw", 8.0)),
        "battery_max_discharge_kw": float(optimizer.get("battery_max_discharge_kw", 8.0)),
        "physical_grid_import_limit_kw": float(optimizer.get("physical_grid_import_limit_kw", 13.8)),
        "grid_export_limit_kw": float(optimizer.get("grid_export_limit_kw", 10.0)),
        "charge_efficiency": float(optimizer.get("battery_charge_efficiency", 0.95)),
        "discharge_efficiency": float(optimizer.get("battery_discharge_efficiency", 0.95)),
        "unknown_price_energy_coverage_fraction": float(optimizer.get("unknown_price_energy_coverage_fraction", 0.35)),
        "unknown_price_risk_premium_ore_kwh": float(optimizer.get("unknown_price_risk_premium_ore_kwh", 40.0)),
        "unknown_price_default_continuation_value_ore_kwh": float(optimizer.get("unknown_price_default_continuation_value_ore_kwh", 150.0)),
    }
    objective["tariff_state"] = tariff_state_for_decision(
        cfg,
        decision_start,
        information_as_of=information_as_of,
    )
    return objective


def input_from_optimizer_plan_v2(plan: dict[str, Any], cfg: dict[str, Any]) -> EngineInput:
    rows = tuple(normalize_horizon_row(dict(r)) for r in (plan.get("rows") or []))
    if not rows:
        raise ValueError("optimizer plan has no rows")
    decision_start = str(rows[0]["start"])
    generated_at = str(plan["generated_at"])
    return EngineInput(
        generated_at=generated_at,
        decision_start=decision_start,
        initial_soc_pct=float(plan["initial_soc_pct"]),
        interval_minutes=int(plan.get("interval_minutes") or 15),
        horizon_rows=rows,
        constraints=common_constraints_from_plan(plan),
        objective=enriched_common_objective(cfg, decision_start, generated_at),
        source={
            "kind": "optimizer_plan_information_vintage",
            "source_planner": str(plan.get("planner") or "unknown"),
            "mode": str(plan.get("mode") or "unknown"),
            "input_profile": "generalized_installation_tariff_v2",
        },
    )
