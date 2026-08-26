from app.historical_closed_loop_v2 import energy_balance_diagnostics


def test_energy_balance_passes_with_solinteg_export_positive_grid():
    rows = [
        {"load_kw": 2.0, "pv_kw": 5.0, "grid_power_kw": 3.0, "battery_power_kw": 0.0},
        {"load_kw": 6.0, "pv_kw": 1.0, "grid_power_kw": -3.0, "battery_power_kw": 2.0},
        {"load_kw": 3.0, "pv_kw": 1.0, "grid_power_kw": 1.0, "battery_power_kw": 3.0},
    ]
    result = energy_balance_diagnostics(rows)
    assert result["pass"] is True
    assert abs(result["signed_residual_kwh"]) < 1e-9
    assert result["interval_mae_kw"] == 0.0


def test_energy_balance_rejects_import_positive_interpretation_data():
    # Same physical examples but grid sign is deliberately reversed relative to
    # the Solinteg convention expected by the evaluator.
    rows = [
        {"load_kw": 2.0, "pv_kw": 5.0, "grid_power_kw": -3.0, "battery_power_kw": 0.0},
        {"load_kw": 6.0, "pv_kw": 1.0, "grid_power_kw": 3.0, "battery_power_kw": 2.0},
        {"load_kw": 3.0, "pv_kw": 1.0, "grid_power_kw": -1.0, "battery_power_kw": 3.0},
    ]
    result = energy_balance_diagnostics(rows)
    assert result["pass"] is False
    assert result["interval_mae_kw"] > result["interval_mae_tolerance_kw"]
