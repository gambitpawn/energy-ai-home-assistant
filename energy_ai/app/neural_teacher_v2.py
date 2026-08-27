from __future__ import annotations

import copy
from datetime import timedelta
from typing import Any

from .app_comparison import _actual_rows
from .engine_contract import EngineInput
from .neural_training import _utc
from .optimizer_v35_replay import solve_v35_from_rows
from .tariff_scenarios import LOCAL_TZ, _calendar_active, _solve_rows

LABEL_SOURCE_V2 = "perfect_information_policy_teacher_v2"


def _tariff_active_intervals(engine_input: EngineInput, tariff: dict[str, Any], enabled: bool) -> int:
    if not enabled:
        return 0
    count = 0
    for row in engine_input.horizon_rows:
        try:
            local = _utc(str(row["start"])).astimezone(LOCAL_TZ)
            if _calendar_active(local, tariff, False):
                count += 1
        except Exception:
            continue
    return count


def _selected_tariff(engine_input: EngineInput) -> tuple[str | None, dict[str, Any] | None, list[float]]:
    objective = engine_input.objective or {}
    tariffs = objective.get("tariffs") or {}
    tariff_state = objective.get("tariff_state") or {}
    if not bool(tariffs.get("enabled")):
        return None, None, []

    candidates = []
    for name in ("consumption_demand", "production_demand"):
        tariff = dict(tariffs.get(name) or {})
        enabled = bool(tariff.get("enabled"))
        active = _tariff_active_intervals(engine_input, tariff, enabled)
        if active <= 0:
            continue
        state = tariff_state.get(name) or {}
        peaks = [float(v) for v in (state.get("historical_top_values_kw") or [])]
        candidates.append((active, name, tariff, peaks))
    if not candidates:
        return None, None, []
    _, name, tariff, peaks = max(candidates, key=lambda x: x[0])
    return name, tariff, peaks


def _cfg_from_input(base_cfg: dict[str, Any], engine_input: EngineInput) -> dict[str, Any]:
    """Reconstruct the teacher's policy/physical config from the frozen input vintage."""
    cfg = copy.deepcopy(base_cfg)
    constraints = engine_input.constraints or {}
    objective = engine_input.objective or {}
    installation = objective.get("installation") or {}
    economics = objective.get("economics") or {}

    policy = cfg.setdefault("policy", {})
    battery = policy.setdefault("battery", {})
    for target, source in (
        ("capacity_kwh", "battery_capacity_kwh"),
        ("hard_min_soc_pct", "hard_min_soc_pct"),
        ("hard_max_soc_pct", "hard_max_soc_pct"),
        ("preferred_min_soc_pct", "preferred_min_soc_pct"),
        ("preferred_max_soc_pct", "preferred_max_soc_pct"),
        ("normal_reserve_soc_pct", "normal_reserve_soc_pct"),
        ("high_uncertainty_reserve_soc_pct", "high_uncertainty_reserve_soc_pct"),
    ):
        if source in constraints:
            battery[target] = float(constraints[source])
    if "battery_capacity_kwh" in installation:
        battery["capacity_kwh"] = float(installation["battery_capacity_kwh"])

    policy["economics"] = {
        "import_overhead_ore_kwh": float(economics.get("import_overhead_ore_kwh", 0.0)),
        "export_overhead_ore_kwh": float(economics.get("export_overhead_ore_kwh", 0.0)),
        "minimum_arbitrage_margin_ore_kwh": float(economics.get("minimum_arbitrage_margin_ore_kwh", 20.0)),
    }

    optimizer = cfg.setdefault("optimizer", {})
    mapping = {
        "battery_max_charge_kw": (installation, "battery_max_charge_kw"),
        "battery_max_discharge_kw": (installation, "battery_max_discharge_kw"),
        "physical_grid_import_limit_kw": (installation, "physical_grid_import_limit_kw"),
        "grid_export_limit_kw": (installation, "grid_export_limit_kw"),
        "battery_charge_efficiency": (installation, "charge_efficiency"),
        "battery_discharge_efficiency": (installation, "discharge_efficiency"),
        "unknown_price_energy_coverage_fraction": (installation, "unknown_price_energy_coverage_fraction"),
        "unknown_price_risk_premium_ore_kwh": (installation, "unknown_price_risk_premium_ore_kwh"),
        "unknown_price_default_continuation_value_ore_kwh": (installation, "unknown_price_default_continuation_value_ore_kwh"),
        "reserve_uncertainty_full_scale_kw": (constraints, "reserve_uncertainty_full_scale_kw"),
        "reserve_critical_soc_pct": (constraints, "reserve_critical_soc_pct"),
        "reserve_critical_penalty_ore_per_kwh_hour": (constraints, "reserve_critical_penalty_ore_per_kwh_hour"),
        "reserve_preferred_penalty_ore_per_kwh_hour": (constraints, "reserve_preferred_penalty_ore_per_kwh_hour"),
        "reserve_target_penalty_ore_per_kwh_hour": (constraints, "reserve_target_penalty_ore_per_kwh_hour"),
        "preferred_max_excess_penalty_ore_per_kwh_hour": (constraints, "preferred_max_excess_penalty_ore_per_kwh_hour"),
        "terminal_soc_tolerance_pct": (constraints, "terminal_soc_tolerance_pct"),
    }
    for target, (source_dict, source_key) in mapping.items():
        if source_key in source_dict:
            optimizer[target] = float(source_dict[source_key])
    optimizer["battery_degradation_ore_kwh"] = float(economics.get("battery_degradation_ore_kwh", optimizer.get("battery_degradation_ore_kwh", 5.0)))

    cfg["tariffs"] = copy.deepcopy(objective.get("tariffs") or cfg.get("tariffs") or {})
    return cfg


def perfect_information_teacher_v2(cfg: dict[str, Any], engine_input: EngineInput) -> tuple[float, dict[str, Any]] | None:
    horizon = list(engine_input.horizon_rows)
    if not horizon:
        return None
    start = _utc(horizon[0]["start"])
    last = _utc(horizon[-1]["start"])
    end = last + timedelta(minutes=engine_input.interval_minutes)
    actual, data = _actual_rows(start, end)
    actual_map = {_utc(r["start"]): r for r in actual}
    if len(actual_map) != len(horizon):
        return None

    injected: list[dict[str, Any]] = []
    for row in horizon:
        stamp = _utc(row["start"])
        observed = actual_map.get(stamp)
        if observed is None:
            return None
        item = dict(row)
        item["load_kw"] = float(observed["load_kw"])
        item["pv_kw"] = float(observed["pv_kw"])
        item["load_uncertainty_kw"] = 0.0
        item["pv_uncertainty_kw"] = 0.0
        item["price_known"] = True
        item["price_ore_kwh"] = float(observed["price_ore_kwh"])
        injected.append(item)

    teacher_cfg = _cfg_from_input(cfg, engine_input)
    tariff_name, tariff, historical_peaks = _selected_tariff(engine_input)
    if tariff is not None:
        solved = _solve_rows(
            injected,
            teacher_cfg,
            tariff,
            force_window=False,
            historical_peaks_kw=historical_peaks,
            initial_soc_pct=float(engine_input.initial_soc_pct),
        )
        first = (solved.get("rows") or [None])[0]
        if not first:
            return None
        action = float(first.get("discharge_kw") or 0.0) - float(first.get("charge_kw") or 0.0)
        return action, {
            "actual_coverage_fraction": data.get("actual_coverage_fraction"),
            "teacher_engine": solved.get("engine"),
            "teacher_mode": "tariff_aware_perfect_information",
            "teacher_tariff": tariff_name,
            "historical_peaks_kw": historical_peaks,
            "teacher_terminal_soc_pct": solved.get("terminal_soc_pct"),
            "teacher_tariff_metric_kw": (solved.get("tariff") or {}).get("metric_kw"),
        }

    solved = solve_v35_from_rows(teacher_cfg, injected, float(engine_input.initial_soc_pct))
    return float(solved["first_action_kw"]), {
        "actual_coverage_fraction": data.get("actual_coverage_fraction"),
        "teacher_engine": solved.get("engine"),
        "teacher_mode": "deterministic_v35_perfect_information",
        "teacher_tariff": None,
        "teacher_terminal_soc_pct": solved.get("terminal_soc_pct"),
        "teacher_objective_cost_ore": solved.get("objective_cost_ore"),
    }
