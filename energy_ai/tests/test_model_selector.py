from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

import pytest

from app import model_selector as ms


def _cfg() -> dict:
    return {
        "policy": {
            "battery": {
                "capacity_kwh": 10.0,
                "hard_min_soc_pct": 0.0,
                "hard_max_soc_pct": 100.0,
                "preferred_min_soc_pct": 10.0,
                "preferred_max_soc_pct": 90.0,
            },
            "economics": {
                "pricing_model": "spot_linked_grid_v1",
                "import_fixed_including_energy_tax_ore_kwh": 36.0,
                "import_spot_percentage": 6.86,
                "export_fixed_compensation_ore_kwh": 2.84,
                "export_spot_percentage": 6.05,
                "minimum_arbitrage_margin_ore_kwh": 20.0,
            },
        },
        "optimizer": {
            "battery_max_charge_kw": 4.0,
            "battery_max_discharge_kw": 4.0,
            "battery_charge_efficiency": 1.0,
            "battery_discharge_efficiency": 1.0,
            "battery_degradation_ore_kwh": 5.0,
            "physical_grid_import_limit_kw": 20.0,
            "grid_export_limit_kw": 20.0,
            "soc_grid_step_kwh": 0.5,
        },
        "tariffs": {"enabled": False},
    }


@pytest.fixture
def selector_db(tmp_path, monkeypatch):
    path = tmp_path / "selector.db"
    monkeypatch.setattr(ms, "DB_PATH", path)
    ms._init_tables()
    return path


def _insert_score(path, context, day, engine, mean_regret, *, p90=None, clamp=0.0):
    payload = {
        "local_date": day,
        "engine_id": engine,
        "context_signature": context,
        "intervals": 96,
        "mean_regret_ore": float(mean_regret),
        "median_regret_ore": float(mean_regret),
        "p90_regret_ore": float(mean_regret if p90 is None else p90),
        "clamp_rate": float(clamp),
    }
    with sqlite3.connect(path) as c:
        c.execute(
            '''INSERT OR REPLACE INTO engine_daily_score(
               local_date,engine_id,context_signature,intervals,mean_regret_ore,
               p90_regret_ore,clamp_rate,payload_json,created_at)
               VALUES (?,?,?,?,?,?,?,?,?)''',
            (
                day,
                engine,
                context,
                96,
                float(mean_regret),
                float(payload["p90_regret_ore"]),
                float(clamp),
                json.dumps(payload),
                "2026-08-01T00:00:00+00:00",
            ),
        )


def test_sustained_challenger_is_promoted(selector_db):
    cfg = _cfg()
    state = ms.ensure_selector_state(cfg)
    context = state["context_signature"]
    start = date(2026, 8, 1)
    for n in range(ms.MIN_PROMOTION_DAYS):
        day = (start + timedelta(days=n)).isoformat()
        _insert_score(selector_db, context, day, ms.BASELINE_ENGINE_ID, 10.0, p90=12.0)
        _insert_score(selector_db, context, day, "adaptive_deterministic_v1", 8.0, p90=10.0)

    gate = ms._promotion_gate(context, "adaptive_deterministic_v1", ms.BASELINE_ENGINE_ID)
    assert gate["eligible"] is True
    result = ms.run_selection_policy(cfg)
    assert result["action"] == "promote"
    assert result["state"]["selected_engine_id"] == "adaptive_deterministic_v1"


def test_tail_regression_blocks_promotion(selector_db):
    cfg = _cfg()
    context = ms.ensure_selector_state(cfg)["context_signature"]
    start = date(2026, 8, 1)
    for n in range(ms.MIN_PROMOTION_DAYS):
        day = (start + timedelta(days=n)).isoformat()
        _insert_score(selector_db, context, day, ms.BASELINE_ENGINE_ID, 10.0, p90=10.0)
        # Strong average on most days but severe bad-day tail.
        challenger = 4.0 if n < ms.MIN_PROMOTION_DAYS - 2 else 30.0
        _insert_score(selector_db, context, day, "adaptive_deterministic_v1", challenger, p90=challenger)

    gate = ms._promotion_gate(context, "adaptive_deterministic_v1", ms.BASELINE_ENGINE_ID)
    assert gate["eligible"] is False
    assert gate["gates"]["tail_not_materially_worse"] is False


def test_performance_rollback_to_frozen_baseline(selector_db):
    cfg = _cfg()
    state = ms.ensure_selector_state(cfg)
    context = state["context_signature"]
    with sqlite3.connect(selector_db) as c:
        c.execute(
            "UPDATE engine_selector_state SET selected_engine_id=?,cooldown_until=NULL WHERE singleton=1",
            ("adaptive_deterministic_v1",),
        )
    start = date(2026, 8, 20)
    for n in range(ms.MIN_ROLLBACK_DAYS):
        day = (start + timedelta(days=n)).isoformat()
        _insert_score(selector_db, context, day, ms.BASELINE_ENGINE_ID, 8.0)
        _insert_score(selector_db, context, day, "adaptive_deterministic_v1", 12.0)

    gate = ms._rollback_gate(context, "adaptive_deterministic_v1")
    assert gate["eligible"] is True
    result = ms.run_selection_policy(cfg)
    assert result["action"] == "rollback"
    assert result["state"]["selected_engine_id"] == ms.BASELINE_ENGINE_ID


def test_context_change_resets_selection_without_sqlite_lock(selector_db):
    cfg = _cfg()
    first = ms.ensure_selector_state(cfg)
    with sqlite3.connect(selector_db) as c:
        c.execute(
            "UPDATE engine_selector_state SET selected_engine_id=? WHERE singleton=1",
            ("adaptive_deterministic_v1",),
        )
    changed = _cfg()
    changed["policy"]["economics"]["import_spot_percentage"] = 7.5
    second = ms.ensure_selector_state(changed)
    assert second["context_signature"] != first["context_signature"]
    assert second["selected_engine_id"] == ms.BASELINE_ENGINE_ID


def test_missing_selected_decision_routes_to_baseline(selector_db):
    cfg = _cfg()
    ms.ensure_selector_state(cfg)
    with sqlite3.connect(selector_db) as c:
        c.execute(
            "UPDATE engine_selector_state SET selected_engine_id=? WHERE singleton=1",
            ("neural_v1",),
        )
        c.execute(
            '''CREATE TABLE engine_decision(
               decision_id TEXT PRIMARY KEY,information_vintage_id TEXT NOT NULL,
               engine_id TEXT NOT NULL,status TEXT NOT NULL,requested_action_kw REAL NOT NULL,
               payload_json TEXT NOT NULL)'''
        )
        c.execute(
            "INSERT INTO engine_decision VALUES (?,?,?,?,?,?)",
            (
                "d1",
                "v1",
                ms.BASELINE_ENGINE_ID,
                "ok",
                1.25,
                json.dumps({"engine_id": ms.BASELINE_ENGINE_ID, "requested_action_kw": 1.25}),
            ),
        )
    routed = ms.route_selected_decision(cfg, "v1", "2026-08-27T12:00:00+00:00")
    assert routed["configured_selected_engine_id"] == "neural_v1"
    assert routed["routed_engine_id"] == ms.BASELINE_ENGINE_ID
    assert routed["fallback_used"] is True


def test_external_oracle_values_future_energy(selector_db):
    cfg = _cfg()
    cfg["optimizer"]["battery_degradation_ore_kwh"] = 0.0
    rows = [
        {"load_kw": 1.0, "pv_kw": 0.0, "price_ore_kwh": 10.0},
        {"load_kw": 1.0, "pv_kw": 0.0, "price_ore_kwh": 10.0},
        {"load_kw": 1.0, "pv_kw": 0.0, "price_ore_kwh": 150.0},
        {"load_kw": 1.0, "pv_kw": 0.0, "price_ore_kwh": 150.0},
    ]
    result = ms._oracle_first_action_values(rows, cfg, 50.0)
    assert result["first_action_values"]
    assert result["oracle_action_kw"] <= 0.0
    assert result["terminal_reference_ore_kwh"] > 0.0
