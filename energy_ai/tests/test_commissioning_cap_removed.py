from __future__ import annotations

from pathlib import Path

from app import actuator_config
from app.actuator_physical_cap_v190 import apply_physical_command_cap, install_physical_command_cap_patch


ROOT = Path(__file__).resolve().parents[1]


def test_retired_cap_is_a_noop_even_if_legacy_value_is_two_kw():
    safety = {
        "requested_action_kw": 7.0,
        "safe_action_kw": 7.0,
        "safe_interval_kw": {"min": -8.0, "max": 8.0},
        "clamped": False,
        "reasons": [],
    }
    cfg = {"actuator": {"max_physical_command_kw": 2.0}}

    install_physical_command_cap_patch()
    result = apply_physical_command_cap(safety, cfg)

    assert result == safety
    assert result["safe_action_kw"] == 7.0
    assert "physical_command_cap_kw" not in result
    assert "cap_applied" not in result


def test_actuator_configuration_no_longer_contains_commissioning_cap():
    assert "actuator_max_physical_command_kw" not in actuator_config.ACTUATOR_DEFAULTS
    assert "actuator_max_physical_command_kw" not in actuator_config.OPTION_TO_RUNTIME


def test_home_assistant_schema_keeps_only_ignored_upgrade_compatibility_key():
    config = (ROOT / "config.yaml").read_text(encoding="utf-8")
    assert 'version: "1.0.97"' in config
    assert "Legacy compatibility only; ignored by the runtime" in config
    assert "actuator_max_physical_command_kw: 8.0" in config


def test_production_operator_wrapper_retires_old_parameter_and_status_semantics():
    source = (ROOT / "app" / "runtime_operator.py").read_text(encoding="utf-8")
    assert '"actuator_max_physical_command_kw"' in source
    assert "_RETIRED_PARAMETER_KEYS" in source
    assert '"enabled": False' in source
    assert '"temporary_commissioning_cap_removed"' in source
    assert 'RELEASE_BUILD = "1.0.97"' in source
