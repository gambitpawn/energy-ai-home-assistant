from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from app.db_compaction import (
    _paths, compact_before_server_start, prepare_restart_if_safe, status,
)
from app.retired_models import remove_retired_model_artifacts, RETIRED_ENGINE_IDS
from app.engine_registry import registry_status
from app.engine_operator_selection import registered_engine_ids


def _database(path: Path) -> None:
    with sqlite3.connect(path) as c:
        c.executescript("""
            CREATE TABLE state_15m(bucket_start TEXT PRIMARY KEY,payload_json TEXT);
            CREATE TABLE neural_training_sample(id INTEGER PRIMARY KEY,features_json TEXT);
            CREATE TABLE adaptive_parameter_state(id INTEGER PRIMARY KEY,payload_json TEXT);
            CREATE TABLE production_state(singleton INTEGER PRIMARY KEY,operating_mode TEXT,physical_writes_enabled INTEGER,actuator_ready INTEGER);
            INSERT INTO production_state VALUES (1,'shadow',0,0);
            INSERT INTO neural_training_sample VALUES (1,'{"learning":true}');
            INSERT INTO adaptive_parameter_state VALUES (1,'{"preserved":true}');
        """)
        for n in range(150):
            c.execute("INSERT INTO state_15m VALUES (?,?)", (str(n), "x" * 300))
        c.execute("DELETE FROM state_15m WHERE CAST(bucket_start AS INTEGER) < 140")


def test_cold_compaction_preserves_all_data_and_shrinks(tmp_path, monkeypatch):
    source = tmp_path / "energy_ai.db"
    _database(source)
    monkeypatch.setattr("app.db_compaction.MIN_FREE_PADDING_BYTES", 0)
    staged = prepare_restart_if_safe(
        database=source, production={"operating_mode": "shadow", "physical_writes_enabled": False, "actuator_ready": False}
    )
    assert staged["status"] == "restart_requested"
    outcome = compact_before_server_start(source)
    assert outcome["status"] == "success", outcome
    assert outcome["compacted_bytes"] <= outcome["previous_bytes"]
    assert not _paths(source)[0].exists()
    assert not _paths(source)[3].exists()
    with sqlite3.connect(source) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute("SELECT COUNT(*) FROM state_15m").fetchone()[0] == 10
        assert c.execute("SELECT COUNT(*) FROM neural_training_sample").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM adaptive_parameter_state").fetchone()[0] == 1
    assert status(source)["last_success_at"]


def test_active_control_never_stages_restart(tmp_path, monkeypatch):
    source = tmp_path / "energy_ai.db"
    _database(source)
    monkeypatch.setattr("app.db_compaction.MIN_FREE_PADDING_BYTES", 0)
    for production in (
        {"operating_mode": "active", "physical_writes_enabled": True, "actuator_ready": True},
        {"operating_mode": "shadow", "physical_writes_enabled": False, "actuator_ready": True},
        {"operating_mode": "paused", "physical_writes_enabled": True, "actuator_ready": False},
    ):
        result = prepare_restart_if_safe(database=source, production=production)
        assert result["status"] != "restart_requested"
    assert not _paths(source)[0].exists()


def test_interrupted_rename_restores_original(tmp_path):
    source = tmp_path / "energy_ai.db"
    _database(source)
    marker, _, _, backup = _paths(source)
    marker.write_text("{}", encoding="utf-8")
    os.replace(source, backup)
    result = compact_before_server_start(source)
    assert result["status"] == "interrupted_swap_recovered"
    with sqlite3.connect(source) as c:
        assert c.execute("SELECT COUNT(*) FROM neural_training_sample").fetchone()[0] == 1


def test_retired_model_cleanup_is_strictly_allowlisted(tmp_path):
    model = tmp_path / "models"
    model.mkdir()
    for name in ("neural_v1.joblib", "gradient_v1.joblib", "pv_model.joblib", "adaptive_model.json"):
        (model / name).write_bytes(b"test")
    (model / "neural_v1_versions").mkdir()
    (model / "neural_v1_versions" / "old.joblib").write_bytes(b"old")
    output = remove_retired_model_artifacts(model)
    assert set(output["retired_engines"]) == RETIRED_ENGINE_IDS
    assert not (model / "neural_v1.joblib").exists()
    assert not (model / "gradient_v1.joblib").exists()
    assert not (model / "neural_v1_versions").exists()
    assert (model / "pv_model.joblib").exists()
    assert (model / "adaptive_model.json").exists()


def test_retired_engines_unavailable_but_adaptive_kept():
    rows = {x["engine_id"]: x for x in registry_status()["engines"]}
    for engine in RETIRED_ENGINE_IDS:
        assert rows[engine]["available"] is False
        assert rows[engine]["learning_enabled"] is False
        assert engine not in registered_engine_ids()
    assert rows["adaptive_deterministic_v1"]["available"] is True


def test_no_live_retired_model_training_or_decisions():
    root = Path(__file__).resolve().parents[1] / "app"
    maint = (root / "runtime_maintenance.py").read_text(encoding="utf-8")
    runtime = (root / "runtime.py").read_text(encoding="utf-8")
    operator = (root / "runtime_operator.py").read_text(encoding="utf-8")
    assert "_neural_loop(cfg)" not in maint
    assert "_gradient_loop(cfg)" not in maint
    assert "NeuralV1Engine(core.cfg).decide" not in runtime
    assert "install_hybrid_runtime_patch(base.core.cfg)" not in operator
    assert "install_gradient_runtime_patch(base.core.cfg)" not in operator
    assert "install_retired_selector_guard()" in operator
    assert "app.cold_start_maintenance" in (root.parent / "run.sh").read_text()

def test_failed_integrity_validation_keeps_original(tmp_path, monkeypatch):
    from app import db_compaction as compaction

    source = tmp_path / "energy_ai.db"
    _database(source)
    original = source.read_bytes()
    marker, _, target, rollback = _paths(source)
    marker.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(compaction, "MIN_FREE_PADDING_BYTES", 0)

    def reject(_connection):
        raise RuntimeError("simulated validation failure")

    monkeypatch.setattr(compaction, "_integrity", reject)
    result = compact_before_server_start(source)
    assert result["status"] == "failed"
    assert source.read_bytes() == original
    assert not target.exists()
    assert not rollback.exists()


def test_interrupted_swap_rolls_back_compacted_candidate(tmp_path):
    import shutil

    source = tmp_path / "energy_ai.db"
    _database(source)
    marker, _, _, rollback = _paths(source)
    shutil.copy2(source, rollback)
    marker.write_text("{}", encoding="utf-8")
    with sqlite3.connect(source) as c:
        c.execute("DELETE FROM neural_training_sample")
    result = compact_before_server_start(source)
    assert result["status"] == "interrupted_swap_rolled_back"
    with sqlite3.connect(source) as c:
        assert c.execute("SELECT COUNT(*) FROM neural_training_sample").fetchone()[0] == 1
