from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.extraordinary_actions import (
    OFFGRID_MODE,
    required_start_energy,
    reserve_overlay,
)

ROOT = Path(__file__).resolve().parents[1]


def _cfg() -> dict:
    return {
        "policy": {
            "battery": {
                "capacity_kwh": 20.0,
                "hard_min_soc_pct": 5.0,
                "hard_max_soc_pct": 100.0,
            }
        },
        "optimizer": {
            "battery_charge_efficiency": 0.95,
            "battery_discharge_efficiency": 0.95,
            "battery_max_charge_kw": 8.0,
            "battery_max_discharge_kw": 8.0,
            "physical_grid_import_limit_kw": 13.8,
        },
        "actuator": {"soc_guard_margin_pct": 1.0},
    }


def _row(start: datetime, *, load=2.0, pv=0.0, load_unc=0.0, pv_unc=0.0) -> dict:
    return {
        "start": start.astimezone(timezone.utc).isoformat(),
        "load_kw": load,
        "pv_kw": pv,
        "load_uncertainty_kw": load_unc,
        "pv_uncertainty_kw": pv_unc,
        "price_known": True,
        "price_ore_kwh": 100.0,
    }


def test_required_outage_soc_uses_pessimistic_forecast_and_soc_guards():
    start = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
    action = {
        "action_id": 1,
        "status": "scheduled",
        "starts_at": start.isoformat(),
        "ends_at": (start + timedelta(hours=1)).isoformat(),
    }
    plan = {
        "rows": [
            _row(start + timedelta(minutes=15 * i), load=3.0, pv=1.0, load_unc=0.5, pv_unc=0.25)
            for i in range(4)
        ]
    }
    result = required_start_energy(action, plan, _cfg())
    assert result["available"] is True
    assert result["coverage_fraction"] == 1.0
    assert result["required_start_soc_pct"] > 6.0
    assert result["usable_max_soc_pct"] == 99.0
    assert result["uncertainty_policy"] == "load_plus_uncertainty_pv_minus_uncertainty"


def test_partial_outage_forecast_exposes_conservative_full_charge_fallback():
    start = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
    action = {
        "action_id": 2,
        "status": "scheduled",
        "starts_at": start.isoformat(),
        "ends_at": (start + timedelta(hours=2)).isoformat(),
    }
    plan = {"rows": [_row(start), _row(start + timedelta(minutes=15))]}
    result = required_start_energy(action, plan, _cfg())
    assert result["available"] is False
    assert result["fallback_target_soc_pct"] == 99.0
    assert result["reason"] == "forecast_does_not_cover_entire_outage_window"


def test_reserve_overlay_prevents_selected_engine_from_eroding_outage_readiness():
    now = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
    outage = now + timedelta(hours=1)
    action = {
        "action_id": 3,
        "status": "scheduled",
        "starts_at": outage.isoformat(),
        "ends_at": (outage + timedelta(hours=1)).isoformat(),
    }
    rows = [
        _row(now + timedelta(minutes=15 * i), load=10.0, pv=0.0, load_unc=1.0)
        for i in range(8)
    ]
    plan = {"rows": rows}
    candidate = {
        "source": "selector_quarter_control",
        "source_id": "vintage",
        "engine_id": "deterministic_v35",
        "decision_start": now.isoformat(),
        "valid_until": (now + timedelta(minutes=15)).isoformat(),
        "requested_action_kw": 6.0,
    }
    result = reserve_overlay(
        candidate,
        action,
        plan,
        _cfg(),
        {"soc_pct": 35.0, "load_kw": 10.0, "pv_kw": 0.0},
    )
    assert result["changed"] is True
    assert result["candidate"]["source"] == "extraordinary_action_reserve_overlay"
    assert result["candidate"]["requested_action_kw"] < candidate["requested_action_kw"]
    assert result["assessment"]["required_start_soc_pct"] > 35.0


def test_action_runtime_does_not_modify_frozen_v35_planner():
    optimizer = (ROOT / "app" / "optimizer.py").read_text(encoding="utf-8")
    assert 'PLANNER_NAME = "deterministic_battery_dp_v3_5"' in optimizer
    assert "extraordinary_action" not in optimizer
    runtime = (ROOT / "app" / "runtime_operator.py").read_text(encoding="utf-8")
    assert "install_extraordinary_actions" in runtime
    assert runtime.index("install_pv_surplus_capture(base)") < runtime.index("install_extraordinary_actions(")


def test_actions_ui_and_routes_are_installed():
    ui = (ROOT / "app" / "ui_actions.py").read_text(encoding="utf-8")
    actions = (ROOT / "app" / "extraordinary_actions.py").read_text(encoding="utf-8")
    assert 'data-view="actions"' in ui
    assert '@app.get("/actions"' in actions
    assert '@app.post("/actions/self-sufficiency"' in actions
    assert '@app.post("/actions/{action_id}/force-end"' in actions


def test_offgrid_transition_is_zero_first_and_grid_return_uses_voltage_not_power():
    source = (ROOT / "app" / "solinteg_command.py").read_text(encoding="utf-8")
    start = source.index("async def enter_offgrid_mode")
    end = source.index("async def grid_availability")
    block = source[start:end]
    assert block.index("set_power_target(0.0") < block.index("set_working_mode")
    assert 'mode: str = "EMS Off-Grid"' in block
    grid_block = source[end:source.index("async def enter_control_mode_zero")]
    assert "_is_grid_voltage_entity" in grid_block
    assert "grid power" in grid_block.lower()
    assert "180.0" in grid_block and "275.0" in grid_block


def test_shadow_transition_is_blocked_while_action_has_control_priority():
    source = (ROOT / "app" / "operator_mode_control.py").read_text(encoding="utf-8")
    shadow = source[source.index('async def operator_mode_shadow'):source.index('@app.post("/control/operator-mode/active"')]
    assert "control_authority_status()" in shadow
    assert "extraordinary_action_has_control_priority" in shadow


def test_prepare_reconcile_has_single_immediate_refresh_not_ten_second_heavy_loop():
    source = (ROOT / "app" / "extraordinary_actions.py").read_text(encoding="utf-8")
    assert 'if not runtime.get("prep_refresh_at") or not control.get("already_active")' in source
    assert "timeout=5.0" in source
    assert OFFGRID_MODE == "EMS Off-Grid"
