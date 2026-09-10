from __future__ import annotations

import math

from app.adaptive_deterministic import AdaptiveParameters, _interval_result_adaptive
from app.price_economics import effective_prices


def _cfg() -> dict:
    return {
        "policy": {
            "economics": {
                "pricing_model": "spot_linked_grid_v1",
                "import_fixed_including_energy_tax_ore_kwh": 36.0,
                "import_spot_percentage": 6.86,
                "export_fixed_compensation_ore_kwh": 2.84,
                "export_spot_percentage": 6.05,
                "minimum_arbitrage_margin_ore_kwh": 20.0,
                # Deliberately misleading legacy aliases: adaptive decisions must
                # ignore these and use the common effective-pricing model.
                "import_overhead_ore_kwh": 0.0,
                "export_overhead_ore_kwh": 0.0,
            }
        },
        "optimizer": {
            "physical_grid_import_limit_kw": 13.8,
            "grid_export_limit_kw": 10.0,
        },
    }


def test_adaptive_interval_uses_common_effective_grid_prices() -> None:
    cfg = _cfg()
    row = {
        "load_kw": 0.0,
        "pv_kw": 0.0,
        "price_known": True,
        "price_ore_kwh": 100.0,
    }
    params = AdaptiveParameters(cycling_penalty_ore_kwh=0.0)

    import_result = _interval_result_adaptive(row, -1.0, cfg, params)
    export_result = _interval_result_adaptive(row, 1.0, cfg, params)
    expected = effective_prices(100.0, cfg)

    assert math.isclose(
        import_result["effective_import_price_ore_kwh"],
        expected["effective_import_price_ore_kwh"],
        rel_tol=0.0,
        abs_tol=1e-9,
    )
    assert math.isclose(
        export_result["effective_export_price_ore_kwh"],
        expected["effective_export_price_ore_kwh"],
        rel_tol=0.0,
        abs_tol=1e-9,
    )
    assert math.isclose(
        import_result["energy_cost_ore"],
        expected["effective_import_price_ore_kwh"] * 0.25,
        rel_tol=0.0,
        abs_tol=1e-9,
    )
    assert math.isclose(
        export_result["energy_cost_ore"],
        -expected["effective_export_price_ore_kwh"] * 0.25,
        rel_tol=0.0,
        abs_tol=1e-9,
    )


def test_pv_charge_has_no_grid_energy_charge_while_export_has_opportunity_value() -> None:
    cfg = _cfg()
    row = {
        "load_kw": 0.5,
        "pv_kw": 4.5,
        "price_known": True,
        "price_ore_kwh": 100.0,
    }
    params = AdaptiveParameters(cycling_penalty_ore_kwh=0.0)

    idle = _interval_result_adaptive(row, 0.0, cfg, params)
    pv_charge = _interval_result_adaptive(row, -4.0, cfg, params)

    assert idle["grid_export_kw"] == 4.0
    assert pv_charge["grid_charge_kw"] == 0.0
    assert pv_charge["grid_export_kw"] == 0.0
    assert pv_charge["energy_cost_ore"] == 0.0
    assert idle["energy_cost_ore"] < 0.0


def test_grid_charge_at_same_spot_costs_more_than_foregone_pv_export() -> None:
    cfg = _cfg()
    params = AdaptiveParameters(cycling_penalty_ore_kwh=0.0, charge_hurdle_ore_kwh=0.0)

    pv_row = {
        "load_kw": 0.5,
        "pv_kw": 4.5,
        "price_known": True,
        "price_ore_kwh": 100.0,
    }
    night_row = {
        "load_kw": 0.5,
        "pv_kw": 0.0,
        "price_known": True,
        "price_ore_kwh": 100.0,
    }

    export_now = _interval_result_adaptive(pv_row, 0.0, cfg, params)
    charge_from_grid_later = _interval_result_adaptive(night_row, -4.0, cfg, params)

    export_revenue = -export_now["energy_cost_ore"]
    baseline_load_cost = 0.5 * 0.25 * charge_from_grid_later["effective_import_price_ore_kwh"]
    later_grid_charge_cost = charge_from_grid_later["energy_cost_ore"] - baseline_load_cost

    assert later_grid_charge_cost > export_revenue
