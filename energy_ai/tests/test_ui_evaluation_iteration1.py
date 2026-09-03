from __future__ import annotations

# Regression contract for the unified Evaluation UI introduced in iteration 1.

from pathlib import Path

from app.ui_evaluation import EVALUATION_EXTENSION


ROOT = Path(__file__).resolve().parents[1]


def test_iteration_one_unifies_history_into_evaluation():
    assert "Actual control performance" in EVALUATION_EXTENSION
    assert "Daily opportunity captured" in EVALUATION_EXTENSION
    assert "Available opportunity" in EVALUATION_EXTENSION
    assert "Opportunity captured" in EVALUATION_EXTENSION
    assert "Evaluated days" in EVALUATION_EXTENSION
    assert 'data-view="history"' in EVALUATION_EXTENSION
    assert "historyTab.style.display='none'" in EVALUATION_EXTENSION


def test_iteration_one_keeps_history_as_persisted_core_source():
    assert "ui/history?days=" in EVALUATION_EXTENSION
    assert "ui/evaluation-history" not in EVALUATION_EXTENSION
    assert "ui/evaluation-day" not in EVALUATION_EXTENSION
    assert "loadEval=async" not in EVALUATION_EXTENSION
    assert "loadHistory=async" not in EVALUATION_EXTENSION


def test_iteration_one_does_not_replace_base_history_loader_or_renderer():
    assert "renderHistory=function" not in EVALUATION_EXTENSION
    assert "periodRequestId" in EVALUATION_EXTENSION
    assert "requestId!==periodRequestId" in EVALUATION_EXTENSION


def test_unified_ui_module_does_not_import_heavy_decomposition_engine():
    source = (ROOT / "app" / "ui_evaluation.py").read_text(encoding="utf-8")
    assert "from .regret_decomposition" not in source
    assert "connect_db" not in source
    assert "threading" not in source


def test_partial_days_are_excluded_from_capture_ratio():
    assert "x?.status==='ok'" in EVALUATION_EXTENSION
    assert "complete days only" in EVALUATION_EXTENSION.lower()
    assert "Comparable complete days only" in EVALUATION_EXTENSION


def test_models_are_out_of_scope_for_unified_evaluation():
    assert "model comparison" not in EVALUATION_EXTENSION.lower()
    assert "model selector" not in EVALUATION_EXTENSION.lower()
