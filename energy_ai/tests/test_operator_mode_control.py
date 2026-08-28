from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI

from app import actuator_release_state, operator_mode_control, production_state


def _init_selector_db(path):
    with sqlite3.connect(path) as c:
        c.execute(
            '''CREATE TABLE engine_control_selection(
               information_vintage_id TEXT PRIMARY KEY,
               decision_start TEXT NOT NULL,
               created_at TEXT NOT NULL,
               configured_selected_engine_id TEXT NOT NULL,
               routed_engine_id TEXT,
               decision_id TEXT,
               requested_action_kw REAL,
               fallback_used INTEGER NOT NULL,
               reason TEXT NOT NULL,
               payload_json TEXT NOT NULL
            )'''
        )


def _insert_selection(path, *, start: datetime, action: float, vintage: str):
    payload = {
        "information_vintage_id": vintage,
        "decision_start": start.isoformat(),
        "configured_selected_engine_id": "deterministic_v35",
        "routed_engine_id": "deterministic_v35",
        "decision_id": f"decision-{vintage}",
        "requested_action_kw": action,
        "fallback_used": False,
        "reason": "deterministic_v35_selected",
    }
    with sqlite3.connect(path) as c:
        c.execute(
            '''INSERT INTO engine_control_selection(
               information_vintage_id,decision_start,created_at,configured_selected_engine_id,
               routed_engine_id,decision_id,requested_action_kw,fallback_used,reason,payload_json
               ) VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (
                vintage,
                start.isoformat(),
                datetime.now(timezone.utc).isoformat(),
                "deterministic_v35",
                "deterministic_v35",
                payload["decision_id"],
                action,
                0,
                payload["reason"],
                json.dumps(payload),
            ),
        )
    return payload


def _candidate_from_selection(selection):
    if not selection:
        return None
    start = datetime.fromisoformat(selection["decision_start"])
    return {
        "source": "selector_quarter_control",
        "source_id": selection["information_vintage_id"],
        "engine_id": selection["routed_engine_id"],
        "decision_start": start.isoformat(),
        "valid_until": (start + timedelta(minutes=15)).isoformat(),
        "requested_action_kw": float(selection["requested_action_kw"]),
    }


def _route(app: FastAPI, path: str):
    return next(r.endpoint for r in app.router.routes if getattr(r, "path", None) == path)


class FakeAdapter:
    async def safe_release(self):
        return {"released": True}


class FakeActuator:
    def __init__(self):
        self.arm_calls = 0
        self.disarm_calls = 0
        self.queued = []

    async def preflight(self):
        return {"ok": True}

    async def zero_handshake_and_arm(self):
        self.arm_calls += 1
        production_state.mark_actuator_ready(True, detail="test_arm")
        return {"ok": True, "stage": "armed"}

    async def process_candidate(self, candidate):
        self.queued.append(dict(candidate))
        return {
            "status": "pending_decision_start",
            "physical_write_performed": False,
            "decision_start": candidate["decision_start"],
        }

    async def disarm(self, reason="manual"):
        self.disarm_calls += 1
        production_state.set_mode("shadow", reason=reason)
        production_state.mark_actuator_ready(False, detail=reason)
        return {"ok": True}

    async def fail_safe(self, reason, payload=None):
        production_state.mark_actuator_ready(False, detail=reason)
        production_state.set_mode("paused", reason=reason)
        return {"ok": False, "status": "fail_safe"}


class FakeTiming:
    def __init__(self):
        self.activated_candidate = None

    def status(self):
        return {"policy": "strict_decision_start_v1", "pending_count": 0}

    async def activate_with(self, candidate, activate):
        self.activated_candidate = dict(candidate)
        await activate()
        return {
            "status": "acknowledged",
            "physical_write_performed": True,
            "safe_action_kw": float(candidate["requested_action_kw"]),
            "physical_target_kw": float(candidate["requested_action_kw"]),
        }


class FakeSelector:
    def __init__(self, latest=None):
        self.latest = latest

    def latest_control_selection(self):
        return self.latest


def _install(tmp_path, monkeypatch, latest=None):
    db = tmp_path / "operator.db"
    _init_selector_db(db)
    monkeypatch.setattr(operator_mode_control, "DB_PATH", db)
    monkeypatch.setattr(production_state, "DB_PATH", db)
    monkeypatch.setattr(actuator_release_state, "DB_PATH", db)

    actuator = FakeActuator()
    timing = FakeTiming()
    app = FastAPI()
    operator_mode_control.install_operator_mode_control(
        app=app,
        core=object(),
        actuator=actuator,
        adapter=FakeAdapter(),
        timing_scheduler=timing,
        selector_module=FakeSelector(latest),
        candidate_from_selection=_candidate_from_selection,
    )
    return db, app, actuator, timing


def test_selection_at_or_before_ignores_future_decision(tmp_path, monkeypatch):
    db = tmp_path / "selection.db"
    _init_selector_db(db)
    monkeypatch.setattr(operator_mode_control, "DB_PATH", db)
    now = datetime(2026, 8, 28, 14, 32, tzinfo=timezone.utc)
    current = _insert_selection(db, start=now.replace(minute=30), action=1.25, vintage="current")
    _insert_selection(db, start=now.replace(minute=45), action=-2.0, vintage="future")

    selected = operator_mode_control.selection_at_or_before(now)
    assert selected["information_vintage_id"] == current["information_vintage_id"]
    assert selected["requested_action_kw"] == 1.25


def test_zero_hold_starts_immediately_and_ends_at_next_quarter():
    now = datetime(2026, 8, 28, 14, 32, 17, tzinfo=timezone.utc)
    candidate = operator_mode_control.zero_hold_candidate(now)
    assert candidate["requested_action_kw"] == 0.0
    assert datetime.fromisoformat(candidate["decision_start"]) == now
    assert datetime.fromisoformat(candidate["valid_until"]) == datetime(2026, 8, 28, 14, 45, tzinfo=timezone.utc)


def test_active_uses_current_interval_without_waiting(tmp_path, monkeypatch):
    db, app, actuator, timing = _install(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    current_start = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    _insert_selection(db, start=current_start, action=1.4, vintage="current")

    result = asyncio.run(_route(app, "/control/operator-mode/active")())

    assert result["selected_mode"] == "active"
    assert result["activation_candidate_source"] == "selector_current_interval"
    assert actuator.arm_calls == 1
    assert timing.activated_candidate["requested_action_kw"] == 1.4
    assert production_state.status()["physical_writes_enabled"] is True


def test_active_with_only_future_decision_enters_zero_hold_and_queues_future(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    current_start = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    future_start = current_start + timedelta(minutes=15)
    future_selection = {
        "information_vintage_id": "future",
        "decision_start": future_start.isoformat(),
        "routed_engine_id": "deterministic_v35",
        "requested_action_kw": -1.8,
    }
    db, app, actuator, timing = _install(tmp_path, monkeypatch, latest=future_selection)

    result = asyncio.run(_route(app, "/control/operator-mode/active")())

    assert result["selected_mode"] == "active"
    assert result["activation_candidate_source"] == "zero_hold_until_next_quarter"
    assert timing.activated_candidate["requested_action_kw"] == 0.0
    assert actuator.queued and actuator.queued[-1]["requested_action_kw"] == -1.8
    assert production_state.status()["physical_writes_enabled"] is True


def test_shadow_is_one_click_safe_disarm(tmp_path, monkeypatch):
    _, app, actuator, _ = _install(tmp_path, monkeypatch)
    production_state.mark_actuator_ready(True, detail="test")
    production_state.set_mode("active", reason="test")

    result = asyncio.run(_route(app, "/control/operator-mode/shadow")())

    assert result["selected_mode"] == "shadow"
    assert actuator.disarm_calls == 1
    assert production_state.status()["physical_writes_enabled"] is False


def test_parameters_ui_has_shadow_active_control():
    html = operator_mode_control.OPERATOR_MODE_EXTENSION
    assert "Operating mode" in html
    assert 'data-mode="shadow"' in html
    assert 'data-mode="active"' in html
    assert "control/operator-mode/active" not in html  # mode is interpolated, not hard-coded twice
    assert "control/operator-mode/${mode}" in html
