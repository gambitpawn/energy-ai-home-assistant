from __future__ import annotations

from pathlib import Path

from app.ui_evaluation import EVALUATION_EXTENSION

ROOT = Path(__file__).resolve().parents[1]


def test_iteration_three_adds_trends_and_forecast_impact_view():
    assert "Capture trend" in EVALUATION_EXTENSION
    assert "Forecast quality vs economic effect" in EVALUATION_EXTENSION
    assert "Load MAE" in EVALUATION_EXTENSION
    assert "PV MAE" in EVALUATION_EXTENSION
    assert "Forecast gap" in EVALUATION_EXTENSION


def test_iteration_three_is_client_side_only():
    source = (ROOT / "app" / "ui_evaluation.py").read_text(encoding="utf-8")
    assert "from .regret_decomposition" not in source
    assert "run_pending_evaluation_decomposition" not in source
    assert "ui/evaluation-decomposition?days=" in source
    assert "ui/history?days=" in source


def test_iteration_three_adds_table_filters_and_row_details_without_affecting_period_kpis():
    assert "evalTableFilter" in EVALUATION_EXTENSION
    assert "All days" in EVALUATION_EXTENSION
    assert "Complete only" in EVALUATION_EXTENSION
    assert "Detailed ready" in EVALUATION_EXTENSION
    assert "eval-detail-row" in EVALUATION_EXTENSION
    assert "Period KPIs always use all complete days" in EVALUATION_EXTENSION


def test_iteration_three_preserves_request_race_guard_and_graceful_decomposition_failure():
    assert "periodRequestId" in EVALUATION_EXTENSION
    assert "requestId!==periodRequestId" in EVALUATION_EXTENSION
    assert "Promise.allSettled" in EVALUATION_EXTENSION
    assert "core evaluation data is unaffected" in EVALUATION_EXTENSION


def test_iteration_three_does_not_touch_model_semantics():
    assert "model comparison" not in EVALUATION_EXTENSION.lower()
    assert "selector score" not in EVALUATION_EXTENSION.lower()
