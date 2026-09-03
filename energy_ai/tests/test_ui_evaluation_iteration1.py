from __future__ import annotations

from app.ui_evaluation import EVALUATION_EXTENSION, _regret_ui, _summary_from_evaluation


def test_evaluation_summary_exposes_opportunity_and_capture():
    result = {
        "status": "ok",
        "data": {"plan_action_coverage_fraction": 1.0},
        "comparison": {
            "realtime_economic_saving_vs_zero_battery_sek": 9.62,
            "perfect_information_gap_sek": 18.45,
        },
    }
    regret = {"valid": True, "total_gap_sek": 18.45}

    summary = _summary_from_evaluation(result, regret)

    assert summary["saving_sek"] == 9.62
    assert summary["remaining_gap_sek"] == 18.45
    assert summary["opportunity_sek"] == 28.07
    assert summary["capture_fraction"] == 0.3427
    assert summary["comparable"] is True


def test_evaluation_summary_excludes_partial_day_and_unstable_ratio():
    result = {
        "status": "partial_plan_coverage",
        "data": {"plan_action_coverage_fraction": 0.625},
        "comparison": {
            "realtime_economic_saving_vs_zero_battery_sek": -0.01,
            "perfect_information_gap_sek": 0.02,
        },
    }

    summary = _summary_from_evaluation(result, {"valid": False})

    assert summary["opportunity_sek"] == 0.01
    assert summary["capture_fraction"] is None
    assert summary["comparable"] is False


def test_regret_ui_uses_unpublished_price_semantics():
    raw = {
        "status": "valid",
        "valid_decomposition": True,
        "decomposition": {
            "forecast_regret_sek": 3.2,
            "price_information_regret_sek": 1.1,
            "planner_horizon_policy_residual_sek": 14.15,
            "realtime_to_hindsight_total_gap_sek": 18.45,
        },
    }

    ui = _regret_ui(raw)

    assert ui["valid"] is True
    assert ui["forecast_gap_sek"] == 3.2
    assert ui["unpublished_price_horizon_sek"] == 1.1
    assert ui["planner_policy_gap_sek"] == 14.15
    assert "published" in ui["definition"]["unpublished_price_horizon"]


def test_iteration_one_ui_is_scoped_to_evaluation_and_history():
    assert "Daily opportunity captured" in EVALUATION_EXTENSION
    assert "Unpublished price horizon" in EVALUATION_EXTENSION
    assert "Opportunity captured" in EVALUATION_EXTENSION
    assert "/ui/evaluation-history" in EVALUATION_EXTENSION
    assert "model comparison" not in EVALUATION_EXTENSION.lower()
