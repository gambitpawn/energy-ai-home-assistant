from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.config as config_module
import app.price_economics as price_economics
import app.settings_store as settings_store


def _use_temp_store(monkeypatch, tmp_path: Path) -> Path:
    db_path = tmp_path / "energy_ai.db"
    monkeypatch.setattr(settings_store, "DB_PATH", db_path)
    return db_path


def test_sqlite_settings_survive_as_authoritative_overrides(monkeypatch, tmp_path: Path):
    _use_temp_store(monkeypatch, tmp_path)
    options_path = tmp_path / "options.json"
    options_path.write_text(
        json.dumps(
            {
                "battery_capacity_kwh": 19.6,
                "import_fixed_including_energy_tax_ore_kwh": 36.0,
                "import_spot_percentage": 6.86,
                "export_fixed_compensation_ore_kwh": 2.84,
                "export_spot_percentage": 6.05,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "OPTIONS_PATH", options_path)

    settings_store.set_setting_overrides(
        {
            "battery_capacity_kwh": 20.5,
            "import_fixed_including_energy_tax_ore_kwh": 41.0,
            "import_spot_percentage": 7.25,
        },
        source="test",
    )

    cfg = config_module.load_config()
    assert cfg["policy"]["battery"]["capacity_kwh"] == pytest.approx(20.5)
    assert cfg["policy"]["economics"]["import_fixed_including_energy_tax_ore_kwh"] == pytest.approx(41.0)
    assert cfg["policy"]["economics"]["import_spot_percentage"] == pytest.approx(7.25)
    assert cfg["policy"]["economics"]["export_fixed_compensation_ore_kwh"] == pytest.approx(2.84)


def test_removing_override_falls_back_to_home_assistant_option(monkeypatch, tmp_path: Path):
    _use_temp_store(monkeypatch, tmp_path)
    settings_store.set_setting_overrides({"import_spot_percentage": 7.25})
    assert settings_store.apply_setting_overrides({"import_spot_percentage": 6.86})["import_spot_percentage"] == pytest.approx(7.25)

    settings_store.delete_setting_overrides(["import_spot_percentage"])
    assert settings_store.apply_setting_overrides({"import_spot_percentage": 6.86})["import_spot_percentage"] == pytest.approx(6.86)


def test_sensitive_credentials_are_rejected(monkeypatch, tmp_path: Path):
    _use_temp_store(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        settings_store.set_setting_overrides({"openai_api_key": "secret"})
    with pytest.raises(ValueError):
        settings_store.set_setting_overrides({"some_token": "secret"})


def test_price_economics_keeps_loaded_db_values_over_raw_options(monkeypatch, tmp_path: Path):
    options_path = tmp_path / "options.json"
    options_path.write_text(
        json.dumps(
            {
                "import_fixed_including_energy_tax_ore_kwh": 36.0,
                "import_spot_percentage": 6.86,
                "export_fixed_compensation_ore_kwh": 2.84,
                "export_spot_percentage": 6.05,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(price_economics, "OPTIONS_PATH", options_path)

    loaded_runtime_economics = {
        "import_fixed_including_energy_tax_ore_kwh": 41.0,
        "import_spot_percentage": 7.25,
        "export_fixed_compensation_ore_kwh": 3.10,
        "export_spot_percentage": 6.50,
        "minimum_arbitrage_margin_ore_kwh": 20.0,
    }
    result = price_economics.current_economics_from_options(loaded_runtime_economics)
    assert result["import_fixed_including_energy_tax_ore_kwh"] == pytest.approx(41.0)
    assert result["import_spot_percentage"] == pytest.approx(7.25)
    assert result["export_fixed_compensation_ore_kwh"] == pytest.approx(3.10)
    assert result["export_spot_percentage"] == pytest.approx(6.50)


def test_v180_economics_parameter_registry_contains_spot_linked_fields():
    import app.ui_v180 as ui_v180

    economics = [p for p in ui_v180.PARAMETERS if p["section"] == "Economics"]
    keys = [p["key"] for p in economics]
    assert keys == [
        "import_fixed_including_energy_tax_ore_kwh",
        "import_spot_percentage",
        "export_fixed_compensation_ore_kwh",
        "export_spot_percentage",
        "minimum_arbitrage_margin_ore_kwh",
        "optimizer_battery_degradation_ore_kwh",
    ]
    defaults = {p["key"]: p["default"] for p in economics}
    assert defaults["import_fixed_including_energy_tax_ore_kwh"] == pytest.approx(36.0)
    assert defaults["import_spot_percentage"] == pytest.approx(6.86)
    assert defaults["export_fixed_compensation_ore_kwh"] == pytest.approx(2.84)
    assert defaults["export_spot_percentage"] == pytest.approx(6.05)
