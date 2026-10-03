from __future__ import annotations

import sqlite3
from pathlib import Path

import app.db as db
import app.diagnostics_store as diagnostics


def _set_db(monkeypatch, tmp_path: Path) -> Path:
    path = tmp_path / "energy_ai.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    return path


def test_control_run_history_persists_checkpoints(monkeypatch, tmp_path):
    _set_db(monkeypatch, tmp_path)
    run_id = diagnostics.new_control_run_id()

    assert diagnostics.start_control_run(run_id, trigger="scheduled_quarter")
    assert diagnostics.checkpoint_control_run(
        run_id,
        stage="optimizer_plan",
        status="completed",
        plan_generated_at="2026-10-02T07:00:28+00:00",
    )
    assert diagnostics.checkpoint_control_run(
        run_id,
        stage="selector_route",
        status="completed",
        decision_start="2026-10-02T07:00:00+00:00",
        information_vintage_id="vintage-1",
        routed_engine_id="adaptive_deterministic_v1",
        requested_action_kw=1.25,
    )
    assert diagnostics.checkpoint_control_run(
        run_id,
        stage="quarter_complete",
        status="completed",
        actuation_status="acknowledged",
        completed=True,
    )

    rows = diagnostics.control_history(limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["run_id"] == run_id
    assert row["status"] == "completed"
    assert row["decision_start"] == "2026-10-02T07:00:00+00:00"
    assert row["plan_generated_at"] == "2026-10-02T07:00:28+00:00"
    assert row["routed_engine_id"] == "adaptive_deterministic_v1"
    assert [event["stage"] for event in row["events"]] == [
        "quarter_start",
        "optimizer_plan",
        "selector_route",
        "quarter_complete",
    ]


def test_maintenance_history_survives_later_jobs(monkeypatch, tmp_path):
    _set_db(monkeypatch, tmp_path)

    assert diagnostics.start_maintenance_run(
        "job-1",
        lane="heavy",
        label="adaptive_maintenance",
        timeout_seconds=1800,
    )
    assert diagnostics.finish_maintenance_run(
        "job-1",
        status="timeout",
        worker_pid=459,
        restart_count=5,
        duration_seconds=1800.0,
        error="deadline",
    )
    assert diagnostics.start_maintenance_run(
        "job-2",
        lane="heavy",
        label="adaptive_maintenance",
        timeout_seconds=1800,
    )
    assert diagnostics.finish_maintenance_run(
        "job-2",
        status="completed",
        worker_pid=500,
        restart_count=5,
        duration_seconds=12.5,
    )

    rows = diagnostics.maintenance_history(label="adaptive_maintenance", limit=10)
    assert len(rows) == 2
    assert {row["status"] for row in rows} == {"timeout", "completed"}
    timeout = next(row for row in rows if row["status"] == "timeout")
    assert timeout["worker_pid"] == 459
    assert timeout["restart_count"] == 5


def test_diagnostics_writes_are_best_effort(monkeypatch):
    def broken_connect_db(*, timeout=30.0):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(diagnostics, "connect_db", broken_connect_db)
    assert diagnostics.start_control_run("run", trigger="scheduled_quarter") is False
    assert diagnostics.checkpoint_control_run("run", stage="optimizer_plan") is False
    assert diagnostics.start_maintenance_run(
        "job",
        lane="heavy",
        label="probe",
        timeout_seconds=10,
    ) is False
    assert diagnostics.finish_maintenance_run("job", status="failed") is False


def test_runtime_routes_expose_persistent_diagnostics():
    root = Path(__file__).resolve().parents[1]
    source = (root / "app" / "runtime_routes.py").read_text(encoding="utf-8")
    assert '@app.get("/maintenance/history"' in source
    assert '@app.get("/diagnostics/control-runs"' in source
    assert '@app.get("/diagnostics/window"' in source


def test_scheduled_quarter_loop_no_longer_swallows_optimizer_failure_silently():
    root = Path(__file__).resolve().parents[1]
    source = (root / "app" / "main.py").read_text(encoding="utf-8")
    block = source[source.index("async def _forecast_maintenance_loop"):source.index("@asynccontextmanager")]
    assert "start_control_run" in block
    assert 'stage="optimizer_pipeline"' in block
    assert "error=fatal_error" in block
