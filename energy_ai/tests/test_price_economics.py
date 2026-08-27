from __future__ import annotations

from pathlib import Path

import pytest

from app.adaptive_deterministic import AdaptiveParameters
from app.price_economics import (
    HISTORICAL_ECONOMICS,
    economics_for_timestamp,
    effective_prices,
    register_current_economics,
)
from app.price_economics_runtime import _adaptive_interval_result, _optimizer_interval_result


def _cfg(import_fixed=50.0, import_pct=6.86, export_fixed=2.84, export_pct=6.05):
    return {
        "policy": {
            "economics": {
                "pricing_model": "spot_linked_grid_v1",
                "import_fixed_including_energy_tax_ore_kwh": import_fixed,
                "import_spot_percentage": import_pct,
                "export_fixed_compensation_ore_kwh": export_fixed,
                "export_spot_percentage": export_pct,
                "minimum_arbitrage_margin_ore_kwh": 20.0,
            }
        },
        "optimizer": {
            "physical_grid_import_limit_kw": 13.8,
            "grid_export_limit_kw": 10.0,
            "battery_degradation_ore_kwh": 5.0,
        },
    }


def test_effective_prices_apply_fixed_and_spot_linked_terms():
    cfg = _cfg()
    prices = effective_prices(100.0, cfg)
    assert prices["effective_import_price_ore_kwh"] == pytest.approx(156.86)
    assert prices["effective_export_price_ore_kwh"] == pytest.approx(108.89)
    assert prices["import_spot_component_ore_kwh"] == pytest.approx(6.86)
    assert prices["export_spot_component_ore_kwh"] == pytest.approx(6.05)


def test_export_value_is_not_clamped_at_negative_spot():
    prices = effective_prices(-100.0, _cfg())
    assert prices["effective_export_price_ore_kwh"] == pytest.approx(-103.21)
    assert prices["effective_export_price_ore_kwh"] < 0.0


def test_optimizer_interval_uses_effective_import_and_export_prices():
    cfg = _cfg()
    import_row = {"load_kw": 1.0, "pv_kw": 0.0, "price_known": True, "price_ore_kwh": 100.0}
    result = _optimizer_interval_result(import_row, 0.0, cfg)
    assert result["effective_import_price_ore_kwh"] == pytest.approx(156.86)
    assert result["energy_cost_ore"] == pytest.approx(156.86 * 0.25)

    export_row = {"load_kw": 0.0, "pv_kw": 1.0, "price_known": True, "price_ore_kwh": 100.0}
    result = _optimizer_interval_result(export_row, 0.0, cfg)
    assert result["effective_export_price_ore_kwh"] == pytest.approx(108.89)
    assert result["energy_cost_ore"] == pytest.approx(-108.89 * 0.25)


def test_adaptive_interval_uses_same_external_economics():
    cfg = _cfg()
    row = {"load_kw": 1.0, "pv_kw": 0.0, "price_known": True, "price_ore_kwh": 100.0}
    result = _adaptive_interval_result(row, 0.0, cfg, AdaptiveParameters())
    assert result["effective_import_price_ore_kwh"] == pytest.approx(156.86)
    assert result["energy_cost_ore"] == pytest.approx(156.86 * 0.25)


def test_historical_economics_uses_version_valid_at_timestamp(monkeypatch, tmp_path: Path):
    import app.price_economics as pe

    monkeypatch.setattr(pe, "DB_PATH", tmp_path / "economics.db")
    cfg_2025 = _cfg(export_fixed=3.68, export_pct=5.58)
    cfg_2025["policy"]["economics"]["economics_valid_from"] = "2025-01-01T00:00:00+01:00"
    first = register_current_economics(cfg_2025)
    assert first["changed"] is True

    cfg_2026 = _cfg(export_fixed=2.84, export_pct=6.05)
    cfg_2026["policy"]["economics"]["economics_valid_from"] = "2026-01-01T00:00:00+01:00"
    second = register_current_economics(cfg_2026)
    assert second["changed"] is True

    old, old_meta = economics_for_timestamp(
        cfg_2026, "2025-08-01T12:00:00+02:00", HISTORICAL_ECONOMICS
    )
    assert old_meta["source"] == "version_store"
    assert old["export_fixed_compensation_ore_kwh"] == pytest.approx(3.68)
    assert old["export_spot_percentage"] == pytest.approx(5.58)

    new, new_meta = economics_for_timestamp(
        cfg_2026, "2026-08-01T12:00:00+02:00", HISTORICAL_ECONOMICS
    )
    assert new_meta["source"] == "version_store"
    assert new["export_fixed_compensation_ore_kwh"] == pytest.approx(2.84)
    assert new["export_spot_percentage"] == pytest.approx(6.05)
