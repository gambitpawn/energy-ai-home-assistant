from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

from app import engine_registry
from app import retired_ml_cleanup


RETIRED = {"neural_v1", "gradient_v1", "hybrid_v1"}
RETIRED_MODULES = {
    "neural_auto",
    "neural_engine",
    "neural_features",
    "neural_qualification",
    "neural_teacher_v2",
    "neural_training",
    "neural_training_v2",
    "gradient_engine",
    "gradient_qualification",
    "gradient_runtime",
    "gradient_selector_qualification",
    "gradient_training",
    "hybrid_engine",
    "hybrid_runtime",
    "price_economics_neural_compat",
    "ui_gradient",
}
RETIRED_RUNTIME_SYMBOLS = {
    "install_hybrid_runtime_patch",
    "install_gradient_runtime_patch",
    "install_qualification_candidate_runtime",
    "install_gradient_qualification_runtime",
    "install_gradient_selector_qualification",
    "NeuralV1Engine",
    "neural_runtime_status",
    "/engines/neural/",
}


def test_retired_learned_models_are_not_registered():
    ids = {item["engine_id"] for item in engine_registry.registry_status()["engines"]}
    assert RETIRED.isdisjoint(ids)
    assert "deterministic_v35" in ids
    assert "adaptive_deterministic_v1" in ids


def _retired_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                leaf = alias.name.rsplit(".", 1)[-1]
                if leaf in RETIRED_MODULES:
                    found.add(leaf)
        elif isinstance(node, ast.ImportFrom):
            module_leaf = (node.module or "").rsplit(".", 1)[-1]
            if module_leaf in RETIRED_MODULES:
                found.add(module_leaf)
            for alias in node.names:
                if alias.name in RETIRED_MODULES:
                    found.add(alias.name)
    return sorted(found)


def test_repository_has_no_retired_engine_or_module_references_outside_cleanup_contract():
    root = Path(__file__).resolve().parents[1]
    violations: list[str] = []

    paths = [*sorted((root / "app").glob("*.py")), *sorted((root / "tests").glob("test_*.py"))]
    for path in paths:
        if path == Path(__file__) or path.name == "retired_ml_cleanup.py":
            continue
        source = path.read_text(encoding="utf-8")
        rel = path.relative_to(root)

        retired_ids = sorted(token for token in RETIRED if token in source)
        if retired_ids:
            violations.append(f"{rel}: retired engine ids: {', '.join(retired_ids)}")

        retired_imports = _retired_imports(path)
        if retired_imports:
            violations.append(f"{rel}: retired module imports: {', '.join(retired_imports)}")

        if path.parent.name == "app":
            retired_symbols = sorted(token for token in RETIRED_RUNTIME_SYMBOLS if token in source)
            if retired_symbols:
                violations.append(f"{rel}: retired runtime symbols: {', '.join(retired_symbols)}")

    assert violations == [], "Retired ML references remain:\n" + "\n".join(violations)


def test_retired_source_modules_are_physically_absent():
    root = Path(__file__).resolve().parents[1] / "app"
    retired_module_files = {f"{name}.py" for name in RETIRED_MODULES}
    present = sorted(path.name for path in root.iterdir() if path.name in retired_module_files)
    assert present == []


def test_runtime_does_not_install_or_shim_retired_model_paths():
    root = Path(__file__).resolve().parents[1]
    operator = (root / "app" / "runtime_operator.py").read_text(encoding="utf-8")
    runtime = (root / "app" / "runtime.py").read_text(encoding="utf-8")
    routes = (root / "app" / "runtime_routes.py").read_text(encoding="utf-8")
    combined = operator + runtime + routes
    for token in RETIRED_RUNTIME_SYMBOLS:
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
