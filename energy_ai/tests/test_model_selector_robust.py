from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

import pytest

from app import model_selector as ms
from app import model_selector_robust as robust
from app.model_selector_policy import install_selector_policy_patch
from app.model_selector_state import install_selector_state_patch

install_selector_state_patch()
install_selector_policy_patch()
robust.install_robust_selector_patch()

ACTIVE_GENERIC_CHALLENGER = "deterministic_refined_v1"


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
    path = tmp_path / "selector_robust.db"
    monkeypatch.setattr(ms, "DB_PATH", path)
    robust._init_tables()
    return path


def _insert_score(path, context, day, engine_id, model_key, regret, *, p90=None, clamp=0.0):
    payload = {
        "local_date": day,
        "engine_id": engine_id,
        "context_signature": context,
        "intervals": 96,
        "mean_regret_ore": float(regret),
        "median_regret_ore": float(regret),
        "p90_regret_ore": float(regret if p90 is None else p90),
        "clamp_rate": float(clamp),
        "model_key": model_key,
        "model_revision_consistent": True,
        "promotion_eligible_model_revision": True,
        "selector_policy_version": robust.POLICY_VERSION,
    }
    with sqlite3.connect(path) as c:
        c.execute(
            '''INSERT OR REPLACE INTO engine_daily_score(
               local_date,engine_id,context_signature,intervals,mean_regret_ore,
               p90_regret_ore,clamp_rate,payload_json,created_at)
               VALUES (?,?,?,?,?,?,?,?,?)''',
            (
                day,
                engine_id,
                context,
                96,
                float(regret),
                float(payload["p90_regret_ore"]),
                float(clamp),
                json.dumps(payload),
                "2026-08-01T00:00:00+00:00",
            ),
        )


def _set_epoch_start(path, day):
    with sqlite3.connect(path) as c:
        c.execute(
            "UPDATE engine_selector_state SET evaluation_start_date=? WHERE singleton=1",
            (day,),
        )


def test_ten_days_and_seven_wins_are_required(selector_db):
    cfg = _cfg()
    state = ms.ensure_selector_state(cfg)
    context = state["context_signature"]
    _set_epoch_start(selector_db, "2026-08-01")
    challenger = ACTIVE_GENERIC_CHALLENGER
    challenger_key = f"{challenger}:model-12"
    start = date(2026, 8, 1)

    for n in range(9):
        day = (start + timedelta(days=n)).isoformat()
        _insert_score(selector_db, context, day, ms.BASELINE_ENGINE_ID, robust.BASELINE_MODEL_KEY, 10.0, p90=12.0)
        _insert_score(selector_db, context, day, challenger, challenger_key, 7.0, p90=9.0)
    gate = robust._robust_promotion_gate(
        context, challenger, challenger_key, ms.BASELINE_ENGINE_ID, robust.BASELINE_MODEL_KEY
    )
    assert gate["eligible"] is False
    assert gate["paired_days"] == 9

    day = (start + timedelta(days=9)).isoformat()
    _insert_score(selector_db, context, day, ms.BASELINE_ENGINE_ID, robust.BASELINE_MODEL_KEY, 10.0, p90=12.0)
    _insert_score(selector_db, context, day, challenger, challenger_key, 9.0, p90=10.0)
    gate = robust._robust_promotion_gate(
        context, challenger, challenger_key, ms.BASELINE_ENGINE_ID, robust.BASELINE_MODEL_KEY
    )
    assert gate["eligible"] is True
    assert gate["paired_days"] == 10
    assert gate["win_days"] >= 7


def test_six_wins_out_of_ten_cannot_promote(selector_db):
    cfg = _cfg()
    context = ms.ensure_selector_state(cfg)["context_signature"]
    _set_epoch_start(selector_db, "2026-08-01")
    challenger = "adaptive_deterministic_v1"
    challenger_key = "adaptive_deterministic_v1:generation-1"
    start = date(2026, 8, 1)
    for n in range(10):
        day = (start + timedelta(days=n)).isoformat()
        _insert_score(selector_db, context, day, ms.BASELINE_ENGINE_ID, robust.BASELINE_MODEL_KEY, 10.0, p90=12.0)
        challenger_regret = 6.0 if n < 6 else 10.5
        _insert_score(selector_db, context, day, challenger, challenger_key, challenger_regret, p90=11.0)
    gate = robust._robust_promotion_gate(
        context, challenger, challenger_key, ms.BASELINE_ENGINE_ID, robust.BASELINE_MODEL_KEY
    )
    assert gate["eligible"] is False
    assert gate["win_days"] == 6
    assert gate["gates"]["wins_at_least_seven_days"] is False


def test_gross_invalid_output_trips_circuit_breaker_immediately(selector_db):
    cfg = _cfg()
    context = ms.ensure_selector_state(cfg)["context_signature"]
    key = f"{ACTIVE_GENERIC_CHALLENGER}:model-12"
    robust._health_event(
        context,
        "2026-08-27T12:00:00+00:00",
        ACTIVE_GENERIC_CHALLENGER,
        key,
        "fault",
        "gross_action_out_of_bounds",
        True,
        {"requested_action_kw": 99.0},
    )
    breaker = robust._circuit_breaker_reason(
        context, ACTIVE_GENERIC_CHALLENGER, key, "gross_action_out_of_bounds"
    )
    assert breaker is not None
    assert breaker["reason"] == "gross invalid model output"


def test_three_consecutive_faults_disqualify(selector_db):
    cfg = _cfg()
    context = ms.ensure_selector_state(cfg)["context_signature"]
    key = "adaptive_deterministic_v1:generation-1"
    for minute in (0, 15, 30):
        robust._health_event(
            context,
            f"2026-08-27T12:{minute:02d}:00+00:00",
            robust.ADAPTIVE_ENGINE_ID,
            key,
            "fault",
            "missing_decision",
            True,
            {},
        )
    breaker = robust._circuit_breaker_reason(context, robust.ADAPTIVE_ENGINE_ID, key, "missing_decision")
    assert breaker is not None
    assert breaker["consecutive_faults"] == 3


def test_new_model_revision_does_not_inherit_old_revision_quarantine(selector_db):
    cfg = _cfg()
    state = robust._ensure_robust_state(cfg)
    context = state["context_signature"]
    old_key = f"{ACTIVE_GENERIC_CHALLENGER}:model-12"
    new_key = f"{ACTIVE_GENERIC_CHALLENGER}:model-13"
    robust._disqualify_model(
        cfg,
        ACTIVE_GENERIC_CHALLENGER,
        old_key,
        "test fault",
        {"fault_type": "gross_action_out_of_bounds"},
    )
    old_status = robust._disqualification_status(context, ACTIVE_GENERIC_CHALLENGER, old_key)
    new_status = robust._disqualification_status(context, ACTIVE_GENERIC_CHALLENGER, new_key)
    assert old_status["quarantine_active"] is True
    assert new_status["quarantine_active"] is False
    assert new_status["disqualified_before"] is False


def test_model_key_changes_with_model_revision(selector_db):
    a = robust._engine_model_key(
        ACTIVE_GENERIC_CHALLENGER,
        {"model": {"model_id": "r12", "model_revision": 12}},
        "2026-08-27T12:00:00+00:00",
    )
    b = robust._engine_model_key(
        ACTIVE_GENERIC_CHALLENGER,
        {"model": {"model_id": "r13", "model_revision": 13}},
        "2026-08-27T12:15:00+00:00",
    )
    assert a != b
