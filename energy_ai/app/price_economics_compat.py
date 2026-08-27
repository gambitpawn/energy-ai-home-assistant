from __future__ import annotations

from contextvars import ContextVar
from statistics import median
from typing import Any

from .price_economics import CURRENT_ECONOMICS, economics_payload, effective_prices


_TARIFF_LIVE_CONTEXT: ContextVar[
    tuple[dict[str, Any], list[dict[str, Any]]] | None
] = ContextVar("tariff_live_economics_context", default=None)


def _historical_actual_interval_solinteg(row: dict[str, Any], cfg: dict[str, Any]) -> dict[str, float]:
    from . import historical_closed_loop_v2 as v2

    opt = cfg.get("optimizer") or {}
    raw_grid = float(row["grid_power_kw"])
    batt = float(row["battery_power_kw"])
    grid_import_positive = -raw_grid
    imp = max(0.0, grid_import_positive)
    exp = max(0.0, -grid_import_positive)
    prices = effective_prices(float(row["price_ore_kwh"]), cfg)
    energy_cost = (
        imp * prices["effective_import_price_ore_kwh"]
        - exp * prices["effective_export_price_ore_kwh"]
    ) * v2.DT_HOURS
    degradation = abs(batt) * v2.DT_HOURS * float(opt.get("battery_degradation_ore_kwh", 5.0))
    return {
        "grid_import_kw": imp,
        "grid_export_kw": exp,
        "effective_import_price_ore_kwh": prices["effective_import_price_ore_kwh"],
        "effective_export_price_ore_kwh": prices["effective_export_price_ore_kwh"],
        "energy_cost_ore": energy_cost,
        "degradation_cost_ore": degradation,
        "cash_cost_ore": energy_cost + degradation,
        "throughput_kwh": abs(batt) * v2.DT_HOURS,
        "charge_kwh": max(0.0, -batt) * v2.DT_HOURS,
        "discharge_kwh": max(0.0, batt) * v2.DT_HOURS,
    }


def _wrap_historical_compare(original, cfg: dict[str, Any]):
    def wrapped(*args, **kwargs):
        from . import historical_closed_loop as h

        result = original(*args, **kwargs)
        if not isinstance(result, dict) or result.get("status") in {
            "unsupported_active_tariffs", "no_actual_data", "insufficient_actual_coverage", "missing_soc"
        }:
            return result
        start = kwargs.get("start")
        end = kwargs.get("end")
        hours = kwargs.get("hours")
        days = kwargs.get("days")
        a, b = h.resolve_window(start=start, end=end, hours=hours, days=days)
        rows, _ = h._actual_rows(a, b)
        if not rows:
            return result
        ref = float(median([
            effective_prices(float(r["price_ore_kwh"]), cfg)["effective_import_price_ore_kwh"]
            for r in rows
        ]))
        actual = result.get("actual_app") or {}
        shadow = result.get("shadow_planner_closed_loop") or {}
        comparison = result.get("comparison") or {}
        battery = (cfg.get("policy") or {}).get("battery") or {}
        cap = float(battery.get("capacity_kwh", 19.6))
        init = float(actual.get("initial_soc_pct") or 0.0)
        aa = cap * (float(actual.get("terminal_soc_pct") or init) - init) / 100.0 * ref
        sa = cap * (float(shadow.get("terminal_soc_pct") or init) - init) / 100.0 * ref
        ac = float(actual.get("cash_cost_ore") or 0.0) - aa
        sc = float(shadow.get("cash_cost_ore") or 0.0) - sa
        advantage = ac - sc
        actual.update({
            "terminal_asset_adjustment_ore": round(aa, 2),
            "economic_cost_ore": round(ac, 2),
            "economic_cost_sek": round(ac / 100.0, 2),
        })
        shadow.update({
            "terminal_asset_adjustment_ore": round(sa, 2),
            "economic_cost_ore": round(sc, 2),
            "economic_cost_sek": round(sc / 100.0, 2),
        })
        comparison.update({
            "planner_advantage_ore": round(advantage, 2),
            "planner_advantage_sek": round(advantage / 100.0, 2),
            "cash_cost_difference_ore": round(
                float(actual.get("cash_cost_ore") or 0.0) - float(shadow.get("cash_cost_ore") or 0.0), 2
            ),
        })
        if result.get("valid_comparison"):
            eps = float(getattr(h, "WINNER_EPSILON_ORE", 1.0))
            result["winner"] = "shadow_planner" if advantage > eps else "actual_app" if advantage < -eps else "tie"
        result["valuation"] = {
            **(result.get("valuation") or {}),
            "reference_price_ore_kwh": round(ref, 3),
            "economic_cost_definition": "effective import cost minus effective export revenue plus battery degradation minus terminal battery asset adjustment",
            "economics_mode": CURRENT_ECONOMICS,
            "pricing": economics_payload(cfg),
        }
        return result
    return wrapped


def _wrap_regret_decomposition(original, cfg: dict[str, Any]):
    """Make the legacy internal terminal-reference formula equal current economics.

    The decomposition's interval economics are already patched through its bound
    solver/evaluator hooks. Its one remaining legacy expression is
    median(spot + import_overhead). Because the configured import formula is
    affine in spot, we can set a temporary compatibility overhead for exactly
    that evaluation window so the old expression equals median effective import.
    """
    def wrapped(*args, **kwargs):
        from . import regret_decomposition as rd

        a, b = rd.resolve_window(
            start=kwargs.get("start"), end=kwargs.get("end"),
            hours=kwargs.get("hours"), days=kwargs.get("days"),
        )
        rows, _ = rd._actual_rows(a, b)
        econ = (cfg.get("policy") or {}).get("economics") or {}
        old_alias = econ.get("import_overhead_ore_kwh")
        if rows:
            spot_median = float(median([float(r["price_ore_kwh"]) for r in rows]))
            effective_median = float(median([
                effective_prices(float(r["price_ore_kwh"]), cfg)["effective_import_price_ore_kwh"]
                for r in rows
            ]))
            econ["import_overhead_ore_kwh"] = effective_median - spot_median
        try:
            result = original(*args, **kwargs)
        finally:
            if old_alias is None:
                econ.pop("import_overhead_ore_kwh", None)
            else:
                econ["import_overhead_ore_kwh"] = old_alias
        if isinstance(result, dict):
            result.setdefault("valuation", {}).update({
                "economics_mode": CURRENT_ECONOMICS,
                "pricing": economics_payload(cfg),
            })
        return result
    return wrapped


def _economics_aware_tariff_live_lp(base_cls):
    """Replace tariff_live's legacy import/export objective coefficients only.

    tariff_live owns its combined demand-tariff MILP. We leave every variable,
    constraint and tariff coefficient untouched. Only objective coefficients for
    grid import/export are derived from the common effective price function.
    """
    class EconomicsAwareTariffLiveLP(base_cls):
        def __init__(self):
            super().__init__()
            self._energy_variables: dict[int, tuple[str, int]] = {}

        def add_vars(self, name, count, lb=0.0, ub=float("inf"), integral=False):
            idx = super().add_vars(name, count, lb, ub, integral)
            if name in {"import", "export"}:
                values = idx.tolist() if hasattr(idx, "tolist") else list(idx)
                for position, raw_idx in enumerate(values):
                    self._energy_variables[int(raw_idx)] = (name, position)
            return idx

        def set_obj(self, idx, coeff):
            scalar = not hasattr(idx, "__len__")
            if scalar:
                binding = self._energy_variables.get(int(idx))
                context = _TARIFF_LIVE_CONTEXT.get()
                if binding is not None and context is not None:
                    cfg, rows_full = context
                    direction, position = binding
                    if 0 <= position < len(rows_full):
                        row = rows_full[position]
                        prices = effective_prices(float(row["price_ore_kwh"]), cfg)
                        from . import tariff_live as tl
                        coeff = (
                            prices["effective_import_price_ore_kwh"] * tl.DT_HOURS
                            if direction == "import"
                            else -prices["effective_export_price_ore_kwh"] * tl.DT_HOURS
                        )
            return super().set_obj(idx, coeff)

    return EconomicsAwareTariffLiveLP


def _reprice_tariff_live_result(result: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    from . import tariff_live as tl

    if not isinstance(result, dict) or result.get("planner") != tl.PLANNER_NAME:
        return result
    rows = result.get("rows") or []
    energy_cost = 0.0
    degradation_cost = 0.0
    for row in rows:
        if not row.get("price_known") or row.get("price_ore_kwh") is None:
            continue
        prices = effective_prices(float(row["price_ore_kwh"]), cfg)
        import_kw = float(row.get("grid_import_kw") or 0.0)
        export_kw = float(row.get("grid_export_kw") or 0.0)
        energy = (
            import_kw * prices["effective_import_price_ore_kwh"]
            - export_kw * prices["effective_export_price_ore_kwh"]
        ) * tl.DT_HOURS
        degradation = float(row.get("degradation_cost_ore") or 0.0)
        policy_adjustment = (
            float(row.get("discretionary_shift_hurdle_cost_ore") or 0.0)
            + float(row.get("reserve_policy_penalty_ore") or 0.0)
            + float(row.get("preferred_max_excess_penalty_ore") or 0.0)
            + float(row.get("continuation_policy_adjustment_ore") or 0.0)
        )
        row.update({
            "effective_import_price_ore_kwh": round(prices["effective_import_price_ore_kwh"], 4),
            "effective_export_price_ore_kwh": round(prices["effective_export_price_ore_kwh"], 4),
            "energy_cost_ore": round(energy, 4),
            "cash_cost_ore": round(energy + degradation, 4),
            "policy_adjustment_ore": round(policy_adjustment, 4),
            "objective_cost_ore": round(energy + degradation + policy_adjustment, 4),
        })
        energy_cost += energy
        degradation_cost += degradation

    summary = result.get("summary") or {}
    tariff_cost = float(summary.get("tariff_cost_ore") or 0.0)
    baseline_cash = float(summary.get("baseline_cash_cost_ore") or 0.0)
    expected_cash = energy_cost + degradation_cost + tariff_cost
    cash_saving = baseline_cash - expected_cash
    optimized_asset = float(summary.get("optimized_continuation_asset_value_ore") or 0.0)
    baseline_asset = float(summary.get("baseline_continuation_asset_value_ore") or 0.0)
    economic_saving = cash_saving + optimized_asset - baseline_asset
    policy_total = (
        float(summary.get("discretionary_shift_hurdle_cost_ore") or 0.0)
        + float(summary.get("reserve_policy_penalty_ore") or 0.0)
        + float(summary.get("preferred_max_excess_penalty_ore") or 0.0)
    )
    summary.update({
        "objective_cost_ore": round(expected_cash + policy_total - optimized_asset, 2),
        "expected_cash_cost_ore": round(expected_cash, 2),
        "expected_cash_saving_ore": round(cash_saving, 2),
        "expected_cash_saving_sek": round(cash_saving / 100.0, 2),
        "expected_saving_ore": round(economic_saving, 2),
        "expected_saving_sek": round(economic_saving / 100.0, 2),
        "energy_cost_ore": round(energy_cost, 2),
        "battery_degradation_cost_ore": round(degradation_cost, 2),
        "economics_mode": CURRENT_ECONOMICS,
    })
    result.setdefault("objective", {}).update({
        "spot_linked_grid_economics": True,
        "economics_mode": CURRENT_ECONOMICS,
        "pricing": economics_payload(cfg),
    })
    return result


def _install_tariff_live_economics(cfg: dict[str, Any]) -> list[str]:
    from . import optimizer as op
    from . import optimizer_evaluation as oe
    from . import tariff_entry as te
    from . import tariff_live as tl

    original_build_horizon = tl._build_horizon
    original_lp = tl._LP
    original_build_tariff_plan = tl.build_tariff_plan

    def build_horizon_with_economics_context(local_cfg):
        rows = original_build_horizon(local_cfg)
        _TARIFF_LIVE_CONTEXT.set((local_cfg, rows))
        return rows

    EconomicsAwareLP = _economics_aware_tariff_live_lp(original_lp)

    def build_tariff_plan_with_economics(*args, **kwargs):
        try:
            result = original_build_tariff_plan(*args, **kwargs)
        finally:
            _TARIFF_LIVE_CONTEXT.set(None)
        return _reprice_tariff_live_result(result, cfg)

    tl._build_horizon = build_horizon_with_economics_context
    tl._LP = EconomicsAwareLP
    # tariff_live imported the continuation helper by value before v1.79.
    tl._continuation_profile = op._continuation_profile
    tl.build_tariff_plan = build_tariff_plan_with_economics

    # tariff_entry imported evaluate_day by value. Rebind the day endpoint to the
    # common repriced evaluator installed earlier in the v1.79 startup chain.
    te.evaluate_optimizer_day = oe.evaluate_day

    return [
        "tariff_live._build_horizon",
        "tariff_live._LP",
        "tariff_live._continuation_profile",
        "tariff_live.build_tariff_plan",
        "tariff_entry.evaluate_optimizer_day",
    ]


def install_compatibility_patches(cfg: dict[str, Any]) -> dict[str, Any]:
    from . import app_comparison as ac
    from . import app_comparison_v2 as ac2
    from . import historical_closed_loop as h
    from . import historical_closed_loop_v2 as h2
    from . import optimizer_evaluation as oe
    from . import regret_decomposition as rd
    from . import runtime_entry_v169 as rt169

    # Importing v2 writes its legacy actual-economics hook into v1; override it
    # after import with the same Solinteg sign correction plus current economics.
    h2._actual_interval_solinteg = _historical_actual_interval_solinteg
    h._actual_interval = _historical_actual_interval_solinteg

    original_historical = h.compare_closed_loop
    h.compare_closed_loop = _wrap_historical_compare(original_historical, cfg)

    # app_comparison_v2 imported the v1 callable by value; refresh the alias after
    # the v1 economics wrapper has been installed.
    ac2._compare_v1 = ac.compare_app_vs_planner

    # regret_decomposition imported these functions by value before v1.79. Rebind
    # all economic hooks and then wrap its one in-function legacy terminal formula.
    rd._apply_action = oe._apply_action
    rd._hindsight = oe._hindsight
    rd.compare_closed_loop = h2.compare_closed_loop
    original_regret = rd.regret_decomposition
    rd.regret_decomposition = _wrap_regret_decomposition(original_regret, cfg)
    # runtime_entry_v169 also imported regret_decomposition by value for the API.
    rt169.regret_decomposition = rd.regret_decomposition

    tariff_live_paths = _install_tariff_live_economics(cfg)

    return {
        "installed": True,
        "patched_paths": [
            "historical_closed_loop._actual_interval",
            "historical_closed_loop.compare_closed_loop",
            "historical_closed_loop_v2._actual_interval_solinteg",
            "app_comparison_v2._compare_v1",
            "regret_decomposition._apply_action",
            "regret_decomposition._hindsight",
            "regret_decomposition.compare_closed_loop",
            "regret_decomposition.regret_decomposition",
            "runtime_entry_v169.regret_decomposition",
            *tariff_live_paths,
        ],
    }
