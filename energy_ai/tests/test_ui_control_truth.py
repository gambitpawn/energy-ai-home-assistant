from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.db import DB_PATH
from app.ui_control_truth import decision_summary


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _setup_tables() -> None:
    with sqlite3.connect(DB_PATH, timeout=20) as c:
        c.executescript(
            '''
            CREATE TABLE IF NOT EXISTS engine_control_selection(
                information_vintage_id TEXT PRIMARY KEY,
                decision_start TEXT NOT NULL,
                created_at TEXT NOT NULL,
                routed_engine_id TEXT,
                decision_id TEXT,
                requested_action_kw REAL,
                fallback_used INTEGER NOT NULL,
                reason TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS actuator_command(
                command_id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                source TEXT NOT NULL,
                source_id TEXT,
                engine_id TEXT,
                decision_start TEXT,
                valid_until TEXT,
                requested_action_kw REAL,
                safe_action_kw REAL,
                physical_write INTEGER NOT NULL,
                status TEXT NOT NULL,
                reason TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS engine_decision(
                decision_id TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL
            );
            '''
        )


def test_active_overview_uses_effective_actuator_target_and_routed_future_selection():
    _setup_tables()
    now = datetime.now(timezone.utc)
    current_start = now.replace(second=0, microsecond=0) - timedelta(minutes=5)
    current_end = current_start + timedelta(minutes=15)
    next_start = current_start + timedelta(minutes=15)

    with sqlite3.connect(DB_PATH, timeout=20) as c:
        c.execute(
            '''INSERT INTO engine_control_selection(
               information_vintage_id,decision_start,created_at,routed_engine_id,
               decision_id,requested_action_kw,fallback_used,reason,payload_json)
               VALUES (?,?,?,?,?,?,?,?,?)''',
            (
                "current-vintage",
                _iso(current_start),
                _iso(now - timedelta(seconds=2)),
                "deterministic_refined_v1",
                "current-decision",
                -0.400842,
                0,
                "manual_engine_available",
                "{}",
            ),
        )
        c.execute(
            '''INSERT INTO engine_control_selection(
               information_vintage_id,decision_start,created_at,routed_engine_id,
               decision_id,requested_action_kw,fallback_used,reason,payload_json)
               VALUES (?,?,?,?,?,?,?,?,?)''',
            (
                "future-vintage",
                _iso(next_start),
                _iso(now),
                "deterministic_refined_v1",
                "future-decision",
                -1.131789,
                0,
                "manual_engine_available",
                "{}",
            ),
        )
        c.execute(
            '''INSERT INTO actuator_command(
               created_at,source,source_id,engine_id,decision_start,valid_until,
               requested_action_kw,safe_action_kw,physical_write,status,reason,payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
            (
                _iso(now),
                "selector_quarter_control",
                "current-vintage",
                "deterministic_refined_v1",
                _iso(current_start),
                _iso(current_end),
                -0.400842,
                -0.3714,
                1,
                "held_existing",
                "within_min_action_change",
                json.dumps({"write_skipped": True}),
            ),
        )

    with patch(
        "app.ui_control_truth.production_status",
        return_value={"operating_mode": "active", "physical_writes_enabled": True},
    ):
        result = decision_summary()

    assert result["source"] == "control_truth_v1"
    assert result["current"]["engine_id"] == "deterministic_refined_v1"
    assert result["current"]["action_kw"] == -0.3714
    assert result["current"]["requested_action_kw"] == -0.400842
    assert result["current"]["reason_code"] == "effective_actuator_target"
    assert result["next"]["action_kw"] == -1.131789
    assert result["next"]["reason_code"] == "routed_future_decision"


def test_shadow_overview_uses_routed_selector_not_base_optimizer_plan():
    _setup_tables()
    now = datetime.now(timezone.utc)
    start = now.replace(second=0, microsecond=0) - timedelta(minutes=5)
    with sqlite3.connect(DB_PATH, timeout=20) as c:
        c.execute(
            '''INSERT INTO engine_control_selection(
               information_vintage_id,decision_start,created_at,routed_engine_id,
               decision_id,requested_action_kw,fallback_used,reason,payload_json)
               VALUES (?,?,?,?,?,?,?,?,?)''',
            (
                "shadow-vintage",
                _iso(start),
                _iso(now),
                "deterministic_refined_v1",
                "shadow-decision",
                -0.42,
                0,
                "manual_engine_available",
                "{}",
            ),
        )

    with patch(
        "app.ui_control_truth.production_status",
        return_value={"operating_mode": "shadow", "physical_writes_enabled": False},
    ):
        result = decision_summary()

    assert result["current"]["action_kw"] == -0.42
    assert result["current"]["engine_id"] == "deterministic_refined_v1"
    assert result["current"]["source"] == "engine_control_selection"
