from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app import model_selector as ms
from app import model_selector_robust as robust
from app import model_selector_robust_hardening as hardening
from app.model_selector_policy import install_selector_policy_patch
from app.model_selector_state import install_selector_state_patch

install_selector_state_patch()
install_selector_policy_patch()
robust.install_robust_selector_patch()
hardening.install_robust_selector_hardening()


def _cfg() -> dict:
    return {
        "policy": {
            "battery": {
                "capacity_kwh": 10.0,
                "hard_min_soc_pct": 5.0,
                "hard_max_soc_pct": 100.0,
                "preferred_min_soc_pct": 15.0,
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
            "battery_charge_efficiency": 0.95,
            "battery_discharge_efficiency": 0.95,
            "battery_degradation_ore_kwh": 5.0,
            "physical_grid_import_limit_kw": 20.0,
            "grid_export_limit_kw": 20.0,
            "soc_grid_step_kwh": 0.5,
        },
        "tariffs": {"enabled": False},
    }


@pytest.fixture
def selector_db(tmp_path, monkeypatch):
    path = tmp_path / "selector_hardening.db"
    monkeypatch.setattr(ms, "DB_PATH", path)
    robust._init_tables()
    return path


def test_cooldown_blocks_promotion_but_not_state_access(selector_db):
    cfg = _cfg()
    robust_state = robust._ensure_robust_state(cfg)
    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    with sqlite3.connect(selector_db) as c:
        c.execute(
            "UPDATE engine_selector_state SET cooldown_until=? WHERE singleton=1",
            (future,),
        )
    result = hardening.run_selection_policy(cfg)
    assert result["action"] == "hold"
    assert result["reason"] == "promotion_cooldown"
    assert result["state"]["selected_engine_id"] == ms.BASELINE_ENGINE_ID
    assert result["state"]["selected_model_key"] == robust.BASELINE_MODEL_KEY


def test_adaptive_generation_waits_for_post_disqualification_candidate(selector_db):
    cfg = _cfg()
    # Seed the current adaptive candidate before generation 1 is considered.
    with sqlite3.connect(selector_db) as c:
        c.execute(
            '''CREATE TABLE IF NOT EXISTS adaptive_parameter_state(
               state_id INTEGER PRIMARY KEY AUTOINCREMENT,
               created_at TEXT NOT NULL,role TEXT NOT NULL,source_run_id INTEGER,
               parameters_json TEXT NOT NULL,score_ore REAL)'''
        )
        c.execute(
            '''INSERT INTO adaptive_parameter_state(
               created_at,role,source_run_id,parameters_json,score_ore)
               VALUES (?,?,?,?,?)''',
            ("2026-08-27T10:00:00+00:00", "candidate", 1, json.dumps({}), None),
        )
        # Rebind generation 1 to the state that actually exists in this fixture.
        state_id = int(c.execute("SELECT MAX(state_id) FROM adaptive_parameter_state").fetchone()[0])
        c.execute(
            '''UPDATE engine_model_generation SET source_state_id=?
               WHERE engine_id=? AND generation=1''',
            (state_id, robust.ADAPTIVE_ENGINE_ID),
        )

    state = robust._ensure_robust_state(cfg)
    context = state["context_signature"]
    key1 = f"{robust.ADAPTIVE_ENGINE_ID}:generation-1"
    robust._disqualify_model(
        cfg,
        robust.ADAPTIVE_ENGINE_ID,
        key1,
        "test live failure",
        {"fault_type": "gross_action_out_of_bounds"},
    )

    # No candidate has been persisted after the fault, so generation 1 remains.
    same = robust._maybe_advance_adaptive_generation(context)
    assert same["generation"] == 1

    with sqlite3.connect(selector_db) as c:
        c.execute(
            '''INSERT INTO adaptive_parameter_state(
               created_at,role,source_run_id,parameters_json,score_ore)
               VALUES (?,?,?,?,?)''',
            ("2026-08-27T13:00:00+00:00", "candidate", 2, json.dumps({"pv_forecast_risk": 0.2}), None),
        )

    advanced = robust._maybe_advance_adaptive_generation(context)
    assert advanced["generation"] == 2
    assert robust._current_model_key(robust.ADAPTIVE_ENGINE_ID).endswith("generation-2")
