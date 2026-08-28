from __future__ import annotations

import pytest

from app import adaptive_learning as learning
from app.adaptive_deterministic import AdaptiveParameters


def _quadratic_score(params: AdaptiveParameters) -> float:
    return (
        (params.pv_forecast_risk - 1.0) ** 2 * 100.0
        + (params.load_forecast_risk - 0.5) ** 2 * 100.0
        + (params.terminal_energy_value_ore_kwh - 175.0) ** 2 / 100.0
        + (params.discharge_hurdle_ore_kwh - 10.0) ** 2
        + (params.reserve_energy_value_ore_kwh - 20.0) ** 2
        + (params.charge_hurdle_ore_kwh - 5.0) ** 2
        + (params.cycling_penalty_ore_kwh - 2.0) ** 2
    )


def test_coordinate_descent_is_local_refinement():
    """v1.0.77 intentionally made coordinate descent local around its seed.

    The full learning cycle first performs global isolated sweeps; coordinate
    descent then refines only neighboring grid points. Starting directly from
    defaults therefore moves pv_forecast_risk two local steps (0 -> .25 -> .5)
    in two passes rather than jumping to the global 1.0 optimum.
    """
    start = AdaptiveParameters()
    optimum, observations = learning.coordinate_descent(start, _quadratic_score, passes=2)
    assert _quadratic_score(optimum) < _quadratic_score(start)
    assert optimum.pv_forecast_risk == pytest.approx(0.5)
    assert optimum.load_forecast_risk == pytest.approx(0.5)
    assert optimum.terminal_energy_value_ore_kwh == pytest.approx(175.0)
    assert optimum.discharge_hurdle_ore_kwh == pytest.approx(10.0)
    assert optimum.reserve_energy_value_ore_kwh == pytest.approx(20.0)
    assert optimum.charge_hurdle_ore_kwh == pytest.approx(5.0)
    assert optimum.cycling_penalty_ore_kwh == pytest.approx(2.0)
    assert observations


def test_learning_cycle_persists_daily_optimum_and_slow_candidate(tmp_path, monkeypatch):
    db = tmp_path / "adaptive.db"
    monkeypatch.setattr(learning, "DB_PATH", db)
    start = AdaptiveParameters()

    result = learning.run_learning_cycle("2026-08-26", _quadratic_score, start=start)

    assert result["ok"] is True
    assert result["daily_optimum_parameters"]["pv_forecast_risk"] == pytest.approx(1.0)
    # Candidate moves 20% toward the daily optimum rather than jumping there.
    assert result["candidate_parameters"]["pv_forecast_risk"] == pytest.approx(0.2)
    assert result["candidate_parameters"]["terminal_energy_value_ore_kwh"] == pytest.approx(155.0)
    assert learning.current_parameters("daily_optimum").pv_forecast_risk == pytest.approx(1.0)
    assert learning.current_parameters("candidate").pv_forecast_risk == pytest.approx(0.2)
    assert learning.has_completed_run("2026-08-26") is True
    status = learning.latest_learning_status()
    assert status["completed_runs"] == 1
    assert status["latest_run"]["status"] == "complete"
