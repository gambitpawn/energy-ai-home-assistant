from datetime import datetime, timezone

import pytest

from app.pv_surplus_capture import (
    PVSurplusCaptureController,
    calculate_capture,
    current_plan_row,
    forecast_net_kw,
)


def _row() -> dict:
    return {
        "start": "2026-09-10T08:00:00+00:00",
        "grid_import_kw": 0.0,
        "grid_export_kw": 3.0,
        "battery_action_kw": 0.0,
    }


def test_forecast_net_can_be_reconstructed_from_stored_grid_and_action() -> None:
    assert forecast_net_kw(_row()) == pytest.approx(-3.0)


def test_idle_with_planned_export_preserves_that_export() -> None:
    decision = calculate_capture(
        row=_row(),
        base_action_kw=0.0,
        actual_load_kw=1.0,
        actual_pv_kw=7.0,
        current_action_kw=0.0,
        deadband_kw=0.10,
    )
    assert decision.planned_export_kw == pytest.approx(3.0)
    assert decision.actual_export_on_base_kw == pytest.approx(6.0)
    assert decision.extra_export_kw == pytest.approx(3.0)
    assert decision.target_action_kw == pytest.approx(-3.0)
    assert decision.should_dispatch is True


def test_forecast_export_is_not_captured_when_actual_matches_plan() -> None:
    decision = calculate_capture(
        row=_row(),
        base_action_kw=0.0,
        actual_load_kw=1.0,
        actual_pv_kw=4.0,
        current_action_kw=0.0,
        deadband_kw=0.10,
    )
    assert decision.planned_export_kw == pytest.approx(3.0)
    assert decision.extra_export_kw == pytest.approx(0.0)
    assert decision.target_action_kw == pytest.approx(0.0)
    assert decision.should_dispatch is False


def test_capture_does_not_force_planned_grid_import() -> None:
    row = {
        "start": "2026-09-10T08:00:00+00:00",
        "load_kw": 3.0,
        "pv_kw": 1.0,
        "battery_action_kw": 0.0,
        "grid_import_kw": 2.0,
        "grid_export_kw": 0.0,
    }
    decision = calculate_capture(
        row=row,
        base_action_kw=0.0,
        actual_load_kw=1.0,
        actual_pv_kw=2.0,
        current_action_kw=0.0,
        deadband_kw=0.10,
    )
    # The unexpected 1 kW export is absorbed, but the controller does not charge
    # another 2 kW from the grid merely to recreate the forecast 2 kW import.
    assert decision.extra_export_kw == pytest.approx(1.0)
    assert decision.target_action_kw == pytest.approx(-1.0)


def test_positive_pv_residual_reduces_planned_discharge_before_charging() -> None:
    row = {
        "start": "2026-09-10T08:00:00+00:00",
        "load_kw": 1.0,
        "pv_kw": 0.0,
        "battery_action_kw": 4.0,
        "grid_import_kw": 0.0,
        "grid_export_kw": 3.0,
    }
    decision = calculate_capture(
        row=row,
        base_action_kw=4.0,
        actual_load_kw=1.0,
        actual_pv_kw=3.0,
        current_action_kw=4.0,
        deadband_kw=0.10,
    )
    assert decision.planned_export_kw == pytest.approx(3.0)
    assert decision.actual_export_on_base_kw == pytest.approx(6.0)
    assert decision.target_action_kw == pytest.approx(1.0)


def test_controller_returns_to_original_plan_when_residual_disappears() -> None:
    controller = PVSurplusCaptureController()
    plan = {"generated_at": "g1", "rows": [_row()]}
    baseline = {
        "source": "selector_quarter_control",
        "source_id": "v1",
        "engine_id": "adaptive_deterministic_v1",
        "decision_start": "2026-09-10T08:00:00+00:00",
        "valid_until": "2026-09-10T08:15:00+00:00",
        "requested_action_kw": 0.0,
        "safe_action_kw": 0.0,
    }
    now = datetime(2026, 9, 10, 8, 5, tzinfo=timezone.utc)
    first = controller.evaluate(
        plan=plan,
        actual={"age_seconds": 1.0, "soc_pct": 35.0, "load_kw": 1.0, "pv_kw": 7.0},
        control=baseline,
        now=now,
        deadband_kw=0.10,
        max_state_age_seconds=180.0,
    )
    assert first["target_action_kw"] == pytest.approx(-3.0)

    residual_control = {
        "source": "pv_surplus_capture",
        "source_id": "v1",
        "engine_id": "adaptive_deterministic_v1",
        "decision_start": "2026-09-10T08:00:00+00:00",
        "valid_until": "2026-09-10T08:15:00+00:00",
        "requested_action_kw": -3.0,
        "safe_action_kw": -3.0,
    }
    second = controller.evaluate(
        plan=plan,
        actual={"age_seconds": 1.0, "soc_pct": 36.0, "load_kw": 1.0, "pv_kw": 4.0},
        control=residual_control,
        now=now,
        deadband_kw=0.10,
        max_state_age_seconds=180.0,
    )
    assert second["target_action_kw"] == pytest.approx(0.0)
    assert second["should_dispatch"] is True


def test_new_non_residual_command_replaces_controller_baseline() -> None:
    controller = PVSurplusCaptureController()
    plan = {"generated_at": "g1", "rows": [_row()]}
    now = datetime(2026, 9, 10, 8, 5, tzinfo=timezone.utc)
    control = {
        "source": "selector_quarter_control",
        "source_id": "new-vintage",
        "engine_id": "adaptive_deterministic_v1",
        "decision_start": "2026-09-10T08:00:00+00:00",
        "valid_until": "2026-09-10T08:15:00+00:00",
        "requested_action_kw": 1.5,
        "safe_action_kw": 1.5,
    }
    decision = controller.evaluate(
        plan=plan,
        actual={"age_seconds": 1.0, "soc_pct": 35.0, "load_kw": 1.0, "pv_kw": 7.0},
        control=control,
        now=now,
        deadband_kw=0.10,
        max_state_age_seconds=180.0,
    )
    assert decision["base_action_kw"] == pytest.approx(1.5)


def test_current_plan_row_respects_interval_boundary() -> None:
    plan = {
        "rows": [
            {"start": "2026-09-10T08:00:00+00:00"},
            {"start": "2026-09-10T08:15:00+00:00"},
        ]
    }
    now = datetime(2026, 9, 10, 8, 15, tzinfo=timezone.utc)
    assert current_plan_row(plan, now)["start"] == "2026-09-10T08:15:00+00:00"
