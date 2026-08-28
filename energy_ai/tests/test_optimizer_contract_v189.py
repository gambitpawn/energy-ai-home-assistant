from __future__ import annotations

from app.optimizer_contract_v189 import REQUIRED_HURDLE_KEYS, normalize_interval_result


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
