from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from app import evaluation_decomposition
from app.ui_evaluation import EVALUATION_EXTENSION, install_evaluation_routes


ROOT = Path(__file__).resolve().parents[1]


def test_iteration_two_exposes_gap_decomposition_labels():
    assert "Why did we miss?" in EVALUATION_EXTENSION
    assert "Forecast gap" in EVALUATION_EXTENSION
    assert "Future-price horizon" in EVALUATION_EXTENSION
    assert "Planner / policy" in EVALUATION_EXTENSION
    assert "Remaining gap decomposition" in EVALUATION_EXTENSION


def test_ui_reads_decomposition_artifacts_without_triggering_compute():
    source = (ROOT / "app" / "ui_evaluation.py").read_text(encoding="utf-8")
    assert "decomposition_history" in source
    assert "from .regret_decomposition" not in source
    assert "run_pending_evaluation_decomposition" not in source
    assert "Promise.allSettled" in EVALUATION_EXTENSION
    assert "core evaluation data is unaffected" in EVALUATION_EXTENSION


def test_iteration_two_registers_only_read_only_decomposition_route():
    app = FastAPI()
    install_evaluation_routes(app, {})
    routes = [r for r in app.router.routes if getattr(r, "path", None) == "/ui/evaluation-decomposition"]
    assert len(routes) == 1
    assert routes[0].methods == {"GET"}


def test_artifact_identity_uses_payload_fingerprint_not_refresh_timestamp():
    source = (ROOT / "app" / "evaluation_decomposition.py").read_text(encoding="utf-8")
    assert "source_fingerprint" in source
    assert "SELECT local_date,payload_json FROM optimizer_day_eval" in source
    assert "source_created_at" not in source


def test_decomposition_reader_is_compute_free(monkeypatch):
    monkeypatch.setattr(
        evaluation_decomposition,
        "_stored_evaluations",
        lambda days: [{"local_date": "2026-09-01", "source_fingerprint": "abc", "status": "ok"}],
    )
    monkeypatch.setattr(evaluation_decomposition, "_artifact_row", lambda *args: None)
    result = evaluation_decomposition.decomposition_history({}, 7)
    assert result["days"] == [{"local_date": "2026-09-01", "status": "pending", "valid": False}]
    assert result["pending_days"] == 1


def test_nightly_schedule_is_mid_gap_and_supports_retroactive_backfill():
    source = (ROOT / "app" / "runtime_maintenance.py").read_text(encoding="utf-8")
    assert "_seconds_until_daily_utc(hour=2, minute=50)" in source
    assert "for _ in range(14)" in source
    assert '"evaluation_decomposition"' in source
    assert "run_pending_evaluation_decomposition" in source
    assert "await asyncio.sleep(20)" in source
    assert "03:50,\n    09:50, 15:50, 21:50 UTC" in source


def test_background_runner_is_not_part_of_ui_or_control_activation():
    ui_source = (ROOT / "app" / "ui_evaluation.py").read_text(encoding="utf-8")
    operator_source = (ROOT / "app" / "runtime_operator.py").read_text(encoding="utf-8")
    assert "run_pending_evaluation_decomposition" not in ui_source
    assert "run_pending_evaluation_decomposition" not in operator_source
