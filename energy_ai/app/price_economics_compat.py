from __future__ import annotations

from statistics import median
from typing import Any

from .price_economics import CURRENT_ECONOMICS, economics_payload, effective_prices


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
        ],
    }
