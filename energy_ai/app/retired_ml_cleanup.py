from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any

from .db import DB_PATH

RETIRED_ENGINE_IDS = ("neural_v1", "gradient_v1", "hybrid_v1")
MODEL_ROOT = Path("/data/models")

_RETIRED_FILES = (
    "neural_v1.joblib",
    "neural_v1.json",
    "neural_v1_qualification.json",
    "neural_auto_status.json",
    "gradient_v1.joblib",
    "gradient_v1.json",
    "gradient_v1_qualification.json",
)
_RETIRED_DIRS = (
    "neural_v1_versions",
    "gradient_v1_versions",
)


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _delete_engine_rows(connection: sqlite3.Connection, table: str) -> int:
    if not _table_exists(connection, table):
        return 0
    placeholders = ",".join("?" for _ in RETIRED_ENGINE_IDS)
    cursor = connection.execute(
        f"DELETE FROM {table} WHERE engine_id IN ({placeholders})",
        RETIRED_ENGINE_IDS,
    )
    return max(0, int(cursor.rowcount or 0))


def cleanup_retired_ml() -> dict[str, Any]:
    """Remove obsolete v1 learned-model artifacts and comparison state.

    The neural/gradient/hybrid architecture is intentionally retired. This
    migration removes generated artifacts and historical comparison/training
    state so future learned models can start from a clean, independently
    designed v2 data contract. The frozen deterministic baseline and adaptive
    deterministic learning state are never touched.
    """
    removed_files: list[str] = []
    removed_dirs: list[str] = []

    for name in _RETIRED_FILES:
        path = MODEL_ROOT / name
        try:
            if path.exists():
                path.unlink()
                removed_files.append(str(path))
        except OSError:
            pass

    for name in _RETIRED_DIRS:
        path = MODEL_ROOT / name
        try:
            if path.exists():
                shutil.rmtree(path)
                removed_dirs.append(str(path))
        except OSError:
            pass

    deleted_rows: dict[str, int] = {}
    selector_reset = False
    operator_reset = False

    try:
        with sqlite3.connect(DB_PATH, timeout=20) as connection:
            connection.execute("BEGIN IMMEDIATE")

            for table in (
                "engine_decision",
                "engine_daily_score",
                "engine_model_generation",
                "engine_model_disqualification",
                "engine_model_health_event",
            ):
                deleted_rows[table] = _delete_engine_rows(connection, table)

            if _table_exists(connection, "neural_training_sample"):
                cursor = connection.execute("DELETE FROM neural_training_sample")
                deleted_rows["neural_training_sample"] = max(0, int(cursor.rowcount or 0))

            if _table_exists(connection, "engine_selector_robust_state"):
                row = connection.execute(
                    "SELECT selected_engine_id FROM engine_selector_robust_state WHERE singleton=1"
                ).fetchone()
                if row and str(row[0]) in RETIRED_ENGINE_IDS:
                    connection.execute("DELETE FROM engine_selector_robust_state WHERE singleton=1")
                    selector_reset = True

            if _table_exists(connection, "engine_selector_state"):
                row = connection.execute(
                    "SELECT selected_engine_id FROM engine_selector_state WHERE singleton=1"
                ).fetchone()
                if row and str(row[0]) in RETIRED_ENGINE_IDS:
                    connection.execute("DELETE FROM engine_selector_state WHERE singleton=1")
                    selector_reset = True

            if _table_exists(connection, "engine_operator_selection"):
                row = connection.execute(
                    "SELECT selection FROM engine_operator_selection WHERE singleton=1"
                ).fetchone()
                if row and str(row[0]) in RETIRED_ENGINE_IDS:
                    connection.execute(
                        "UPDATE engine_operator_selection SET selection='auto' WHERE singleton=1"
                    )
                    operator_reset = True

            connection.commit()
    except sqlite3.Error:
        pass

    return {
        "retired_engine_ids": list(RETIRED_ENGINE_IDS),
        "removed_files": removed_files,
        "removed_directories": removed_dirs,
        "deleted_rows": deleted_rows,
        "selector_reset": selector_reset,
        "operator_reset": operator_reset,
    }
