from __future__ import annotations

from app.optimizer_contract_v189 import REQUIRED_HURDLE_KEYS, normalize_interval_result
from app.price_economics_runtime import _optimizer_interval_result as repriced_interval_result


def test_normalize_restores_frozen_v35_hurdle_aliases_from_runtime_economics_key():
    source = {
        "cash_cost_ore": 12.5,
        "hurdle_cost_ore": 3.25,
        "interval_cost_ore": 15.75,
    }
    result = normalize_interval_result(source)
    assert result["hurdle_cost_ore"] == 3.25
    assert result["discretionary_shift_hurdle_cost_ore"] == 3.25
    assert result["arbitrage_hurdle_cost_ore"] == 3.25
    assert result["cash_cost_ore"] == 12.5
    assert result["interval_cost_ore"] == 15.75
    assert set(REQUIRED_HURDLE_KEYS).issubset(result)


def test_actual_repriced_optimizer_result_is_compatible_after_normalization():
    cfg = {
        "optimizer": {
            "physical_grid_import_limit_kw": 13.8,
            "grid_export_limit_kw": 10.0,
            "battery_degradation_ore_kwh": 5.0,
        },
        "policy": {
            "economics": {
                "import_fixed_including_energy_tax_ore_kwh": 36.0,
                "import_spot_percentage": 6.86,
                "export_fixed_compensation_ore_kwh": 2.84,
                "export_spot_percentage": 6.05,
                "minimum_arbitrage_margin_ore_kwh": 20.0,
            }
        },
    }
    row = {
        "load_kw": 5.0,
        "pv_kw": 0.0,
        "price_known": True,
        "price_ore_kwh": 100.0,
    }
    repriced = repriced_interval_result(row, 2.0, cfg)
    # This is the exact legacy/runtime mismatch that caused the v1.0.88 refresh crash.
    assert "hurdle_cost_ore" in repriced
    assert "discretionary_shift_hurdle_cost_ore" not in repriced

    fixed = normalize_interval_result(repriced)
    assert set(REQUIRED_HURDLE_KEYS).issubset(fixed)
    assert fixed["discretionary_shift_hurdle_cost_ore"] == repriced["hurdle_cost_ore"]
    assert fixed["arbitrage_hurdle_cost_ore"] == repriced["hurdle_cost_ore"]
    assert fixed["interval_cost_ore"] == repriced["interval_cost_ore"]


def test_normalize_preserves_existing_frozen_v35_alias_value():
    source = {
        "hurdle_cost_ore": 99.0,
        "discretionary_shift_hurdle_cost_ore": 4.5,
        "arbitrage_hurdle_cost_ore": 4.5,
    }
    result = normalize_interval_result(source)
    assert result["discretionary_shift_hurdle_cost_ore"] == 4.5
    assert result["arbitrage_hurdle_cost_ore"] == 4.5
    # Existing fields are never rewritten; the compatibility layer only fills gaps.
    assert result["hurdle_cost_ore"] == 99.0


def test_normalize_uses_zero_only_when_no_hurdle_key_exists():
    result = normalize_interval_result({"cash_cost_ore": 1.0})
    assert result["hurdle_cost_ore"] == 0.0
    assert result["discretionary_shift_hurdle_cost_ore"] == 0.0
    assert result["arbitrage_hurdle_cost_ore"] == 0.0
