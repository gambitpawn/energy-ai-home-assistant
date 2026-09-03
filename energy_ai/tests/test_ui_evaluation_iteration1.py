from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from app.ui_evaluation import EVALUATION_EXTENSION, install_evaluation_routes


ROOT = Path(__file__).resolve().parents[1]


def test_iteration_one_unifies_history_into_evaluation():
    assert "Actual control performance" in EVALUATION_EXTENSION
    assert "Daily opportunity captured" in EVALUATION_EXTENSION
    assert "Available opportunity" in EVALUATION_EXTENSION
    assert "Opportunity captured" in EVALUATION_EXTENSION
    assert "Evaluated days" in EVALUATION_EXTENSION
    assert 'data-view="history"' in EVALUATION_EXTENSION
    assert "historyTab.style.display='none'" in EVALUATION_EXTENSION


def test_iteration_one_uses_only_existing_persisted_read_endpoints():
    assert "ui/history?days=" in EVALUATION_EXTENSION
    assert "ui/evaluation-history" not in EVALUATION_EXTENSION
    assert "ui/evaluation-day" not in EVALUATION_EXTENSION
    assert "regret-decomposition" not in EVALUATION_EXTENSION
    assert "loadEval=async" not in EVALUATION_EXTENSION
    assert "loadHistory=async" not in EVALUATION_EXTENSION


def test_iteration_one_registers_no_new_backend_routes():
    app = FastAPI()
    before = list(app.router.routes)

    install_evaluation_routes(app, {})

    assert list(app.router.routes) == before


def test_iteration_one_has_no_heavy_decomposition_dependency():
    source = (ROOT / "app" / "ui_evaluation.py").read_text(encoding="utf-8")
    assert "regret_decomposition" not in source
    assert "asyncio.to_thread" not in source
    assert "connect_db" not in source
    assert "threading" not in source


def test_partial_days_are_excluded_from_capture_ratio():
    assert "x?.status==='ok'" in EVALUATION_EXTENSION
    assert "complete days only" in EVALUATION_EXTENSION.lower()
    assert "Comparable complete days only" in EVALUATION_EXTENSION


def test_models_are_out_of_scope_for_evaluation_iteration_one():
    assert "model comparison" not in EVALUATION_EXTENSION.lower()
    assert "model selector" not in EVALUATION_EXTENSION.lower()
