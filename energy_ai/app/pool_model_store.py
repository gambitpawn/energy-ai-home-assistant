from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from . import db as db_module
from .pool_control_contract import PoolThermalModelState


VALID_STATUSES = {"candidate", "approved", "rejected", "superseded"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_pool_model_store() -> None:
    with db_module.connect_db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS pool_thermal_model_state(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL,
                model_kind TEXT NOT NULL,
                trained_at TEXT NOT NULL,
                training_window_start TEXT,
                training_window_end TEXT,
                parameters_json TEXT NOT NULL,
                sample_count_off INTEGER NOT NULL,
                sample_count_on INTEGER NOT NULL,
                confidence REAL NOT NULL,
                metrics_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_pool_thermal_model_status_created
                ON pool_thermal_model_state(status, created_at DESC);
            """
        )


def insert_pool_model_state(
    state: PoolThermalModelState,
    *,
    status: str = "candidate",
) -> int:
    status = str(status).strip().lower()
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid pool model status: {status}")
    init_pool_model_store()
    params = {
        "base_loss_c_per_hour": state.base_loss_c_per_hour,
        "ambient_loss_coefficient": state.ambient_loss_coefficient,
        "heat_gain_c_per_kwh": state.heat_gain_c_per_kwh,
        "effective_power_kw": state.effective_power_kw,
    }
    with db_module.connect_db() as c:
        cur = c.execute(
            """
            INSERT INTO pool_thermal_model_state(
                created_at,status,model_kind,trained_at,
                training_window_start,training_window_end,parameters_json,
                sample_count_off,sample_count_on,confidence,metrics_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                _now(),
                status,
                state.model_kind,
                state.trained_at,
                state.training_window_start,
                state.training_window_end,
                json.dumps(params, ensure_ascii=False, separators=(",", ":")),
                int(state.sample_count_off),
                int(state.sample_count_on),
                float(state.confidence),
                json.dumps(state.metrics, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        return int(cur.lastrowid)


def _row_to_dict(row: tuple[Any, ...] | None) -> dict[str, Any] | None:
    if not row:
        return None
    (
        model_id,
        created_at,
        status,
        model_kind,
        trained_at,
        training_window_start,
        training_window_end,
        parameters_json,
        sample_count_off,
        sample_count_on,
        confidence,
        metrics_json,
    ) = row
    try:
        parameters = json.loads(parameters_json or "{}")
    except Exception:
        parameters = {}
    try:
        metrics = json.loads(metrics_json or "{}")
    except Exception:
        metrics = {}
    return {
        "id": int(model_id),
        "created_at": str(created_at),
        "status": str(status),
        "model_kind": str(model_kind),
        "trained_at": str(trained_at),
        "training_window_start": training_window_start,
        "training_window_end": training_window_end,
        "sample_count_off": int(sample_count_off),
        "sample_count_on": int(sample_count_on),
        "confidence": float(confidence),
        "metrics": metrics,
        **parameters,
    }


def latest_pool_model_state(*, status: str | None = None) -> dict[str, Any] | None:
    init_pool_model_store()
    query = """
        SELECT id,created_at,status,model_kind,trained_at,
               training_window_start,training_window_end,parameters_json,
               sample_count_off,sample_count_on,confidence,metrics_json
        FROM pool_thermal_model_state
    """
    args: tuple[Any, ...] = ()
    if status is not None:
        status = str(status).strip().lower()
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid pool model status: {status}")
        query += " WHERE status=?"
        args = (status,)
    query += " ORDER BY id DESC LIMIT 1"
    with db_module.connect_db() as c:
        row = c.execute(query, args).fetchone()
    return _row_to_dict(row)


def approve_pool_model_state(model_id: int) -> dict[str, Any]:
    init_pool_model_store()
    with db_module.connect_db() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute(
            "SELECT id,status FROM pool_thermal_model_state WHERE id=?",
            (int(model_id),),
        ).fetchone()
        if not row:
            raise ValueError(f"pool model id not found: {model_id}")
        if str(row[1]) == "rejected":
            raise ValueError("rejected pool model cannot be approved")
        c.execute(
            "UPDATE pool_thermal_model_state SET status='superseded' WHERE status='approved' AND id<>?",
            (int(model_id),),
        )
        c.execute(
            "UPDATE pool_thermal_model_state SET status='approved' WHERE id=?",
            (int(model_id),),
        )
    approved = latest_pool_model_state(status="approved")
    if approved is None:
        raise RuntimeError("pool model approval was not persisted")
    return approved
