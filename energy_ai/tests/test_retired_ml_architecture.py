from __future__ import annotations

import sqlite3
from pathlib import Path

from app import engine_registry
from app import retired_ml_cleanup


RETIRED = {"neural_v1", "gradient_v1", "hybrid_v1"}


def test_retired_learned_models_are_not_registered():
    ids = {item["engine_id"] for item in engine_registry.registry_status()["engines"]}
    assert RETIRED.isdisjoint(ids)
    assert "deterministic_v35" in ids
    assert "adaptive_deterministic_v1" in ids


def test_runtime_does_not_install_or_shim_retired_model_paths():
    root = Path(__file__).resolve().parents[1]
    operator = (root / "app" / "runtime_operator.py").read_text(encoding="utf-8")
    runtime = (root / "app" / "runtime.py").read_text(encoding="utf-8")
    routes = (root / "app" / "runtime_routes.py").read_text(encoding="utf-8")
    combined = operator + runtime + routes
    for token in (
        "install_hybrid_runtime_patch",
        "install_gradient_runtime_patch",
        "install_qualification_candidate_runtime",
        "install_gradient_qualification_runtime",
        "install_gradient_selector_qualification",
        "NeuralV1Engine",
        "neural_runtime_status",
        "/engines/neural/",
    ):
        assert token not in combined


def test_maintenance_has_no_retired_ml_training_loops():
    root = Path(__file__).resolve().parents[1]
    source = (root / "app" / "runtime_maintenance.py").read_text(encoding="utf-8")
    assert "_neural_loop" not in source
    assert "_gradient_loop" not in source
    assert "neural_maintenance" not in source
    assert "gradient_maintenance" not in source


def test_cleanup_removes_retired_state_but_preserves_adaptive(tmp_path, monkeypatch):
    model_root = tmp_path / "models"
    model_root.mkdir()
    for name in retired_ml_cleanup._RETIRED_FILES:
        (model_root / name).write_text("obsolete", encoding="utf-8")
    for name in retired_ml_cleanup._RETIRED_DIRS:
        path = model_root / name
        path.mkdir()
        (path / "old.joblib").write_text("obsolete", encoding="utf-8")

    db_path = tmp_path / "energy.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE engine_decision(engine_id TEXT);
            CREATE TABLE engine_daily_score(engine_id TEXT);
            CREATE TABLE engine_model_generation(engine_id TEXT);
            CREATE TABLE engine_model_disqualification(engine_id TEXT);
            CREATE TABLE engine_model_health_event(engine_id TEXT);
            CREATE TABLE neural_training_sample(information_vintage_id TEXT);
            CREATE TABLE engine_selector_robust_state(
                singleton INTEGER PRIMARY KEY,
                selected_engine_id TEXT
            );
            CREATE TABLE engine_selector_state(
                singleton INTEGER PRIMARY KEY,
                selected_engine_id TEXT
            );
            CREATE TABLE engine_operator_selection(
                singleton INTEGER PRIMARY KEY,
                selection TEXT
            );
            """
        )
        for table in (
            "engine_decision",
            "engine_daily_score",
            "engine_model_generation",
            "engine_model_disqualification",
            "engine_model_health_event",
        ):
            connection.execute(f"INSERT INTO {table}(engine_id) VALUES ('neural_v1')")
            connection.execute(f"INSERT INTO {table}(engine_id) VALUES ('gradient_v1')")
            connection.execute(f"INSERT INTO {table}(engine_id) VALUES ('hybrid_v1')")
            connection.execute(f"INSERT INTO {table}(engine_id) VALUES ('adaptive_deterministic_v1')")
        connection.execute("INSERT INTO neural_training_sample VALUES ('old-training-row')")
        connection.execute("INSERT INTO engine_selector_robust_state VALUES (1, 'gradient_v1')")
        connection.execute("INSERT INTO engine_selector_state VALUES (1, 'neural_v1')")
        connection.execute("INSERT INTO engine_operator_selection VALUES (1, 'hybrid_v1')")

    monkeypatch.setattr(retired_ml_cleanup, "MODEL_ROOT", model_root)
    monkeypatch.setattr(retired_ml_cleanup, "DB_PATH", db_path)

    result = retired_ml_cleanup.cleanup_retired_ml()

    assert result["selector_reset"] is True
    assert result["operator_reset"] is True
    assert not any((model_root / name).exists() for name in retired_ml_cleanup._RETIRED_FILES)
    assert not any((model_root / name).exists() for name in retired_ml_cleanup._RETIRED_DIRS)

    with sqlite3.connect(db_path) as connection:
        for table in (
            "engine_decision",
            "engine_daily_score",
            "engine_model_generation",
            "engine_model_disqualification",
            "engine_model_health_event",
        ):
            ids = {row[0] for row in connection.execute(f"SELECT engine_id FROM {table}")}
            assert ids == {"adaptive_deterministic_v1"}
        assert connection.execute("SELECT COUNT(*) FROM neural_training_sample").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM engine_selector_robust_state").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM engine_selector_state").fetchone()[0] == 0
        assert connection.execute("SELECT selection FROM engine_operator_selection").fetchone()[0] == "auto"
