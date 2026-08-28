from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np

from app import hybrid_engine
from app.engine_contract import EngineInput
from app.engine_registry import descriptor
from app.hybrid_engine import HybridV1Engine, NeuralActionPrior, solve_hybrid_from_rows


def _cfg():
    return {
        "policy": {
            "battery": {
                "capacity_kwh": 20.0,
                "hard_min_soc_pct": 5.0,
                "hard_max_soc_pct": 100.0,
                "preferred_min_soc_pct": 5.0,
                "preferred_max_soc_pct": 100.0,
                "normal_reserve_soc_pct": 5.0,
                "high_uncertainty_reserve_soc_pct": 5.0,
            },
            "economics": {
                "import_overhead_ore_kwh": 0.0,
                "export_overhead_ore_kwh": 0.0,
                "minimum_arbitrage_margin_ore_kwh": 0.0,
            },
        },
        "optimizer": {
            "battery_max_charge_kw": 8.0,
            "battery_max_discharge_kw": 8.0,
            "battery_charge_efficiency": 0.95,
            "battery_discharge_efficiency": 0.95,
            "battery_degradation_ore_kwh": 0.0,
            "physical_grid_import_limit_kw": 20.0,
            "grid_export_limit_kw": 20.0,
            "soc_grid_step_kwh": 0.5,
            "terminal_soc_tolerance_pct": 3.0,
            "terminal_soc_tiebreak_ore_per_kwh": 0.0,
            "reserve_uncertainty_full_scale_kw": 3.0,
            "reserve_critical_soc_pct": 5.0,
            "reserve_critical_penalty_ore_per_kwh_hour": 0.0,
            "reserve_preferred_penalty_ore_per_kwh_hour": 0.0,
            "reserve_target_penalty_ore_per_kwh_hour": 0.0,
            "preferred_max_excess_penalty_ore_per_kwh_hour": 0.0,
            "unknown_price_energy_coverage_fraction": 0.35,
            "unknown_price_risk_premium_ore_kwh": 0.0,
            "unknown_price_default_continuation_value_ore_kwh": 0.0,
        },
        "tariffs": {"enabled": False},
    }


def _rows(first_price=0.0, second_price=0.0):
    start = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    return [
        {
            "start": start.isoformat(),
            "load_kw": 2.0,
            "pv_kw": 2.0,
            "load_uncertainty_kw": 0.0,
            "pv_uncertainty_kw": 0.0,
            "price_known": True,
            "price_ore_kwh": float(first_price),
        },
        {
            "start": (start + timedelta(minutes=15)).isoformat(),
            "load_kw": 2.0,
            "pv_kw": 2.0,
            "load_uncertainty_kw": 0.0,
            "pv_uncertainty_kw": 0.0,
            "price_known": True,
            "price_ore_kwh": float(second_price),
        },
    ]


def _prior(*, top=2.0, strength=20.0):
    probabilities = {2.0: 0.90, 0.0: 0.05, -2.0: 0.05}
    if top == -2.0:
        probabilities = {-2.0: 0.90, 0.0: 0.05, 2.0: 0.05}
    return NeuralActionPrior(
        probabilities=probabilities,
        top_action_kw=top,
        confidence=0.90,
        normalized_confidence=0.90,
        prior_strength_ore=strength,
        neural_model={"model_id": "neural-test", "shadow_ready": True},
    )


def test_registry_marks_hybrid_as_available_learning_challenger():
    item = descriptor("hybrid_v1")
    assert item.family == "hybrid"
    assert item.available is True
    assert item.learning_enabled is True
    assert item.baseline is False


def test_neural_prior_can_change_first_action_without_leaving_dp_constraints():
    solved = solve_hybrid_from_rows(
        _cfg(),
        _rows(first_price=200.0, second_price=0.0),
        50.0,
        _prior(top=-2.0, strength=1000.0),
        # This test isolates the neural-guidance mechanism. The separate test
        # below verifies that the production regret guard rejects this change.
        max_backbone_regret_ore=1000.0,
    )
    assert solved["accepted_neural_guidance"] is True
    assert solved["neural_changed_first_action"] is True
    assert solved["first_action_kw"] == solved["guided_action_kw"]
    assert solved["first_action_kw"] != solved["backbone_action_kw"]
    assert abs(solved["first_action_kw"]) <= 8.0
    assert solved["backbone_regret_ore"] <= solved["max_backbone_regret_ore"] + 1e-9


def test_backbone_regret_guard_rejects_economically_bad_neural_path():
    solved = solve_hybrid_from_rows(
        _cfg(),
        _rows(first_price=200.0, second_price=0.0),
        50.0,
        _prior(top=-2.0, strength=1000.0),
        max_backbone_regret_ore=0.01,
    )
    assert solved["guided_action_kw"] != solved["backbone_action_kw"]
    assert solved["accepted_neural_guidance"] is False
    assert solved["first_action_kw"] == solved["backbone_action_kw"]
    assert solved["rejection_reason"] == "guided_path_exceeded_backbone_regret_guard"


class _FakeModel:
    classes_ = np.asarray([-2.0, 0.0, 2.0])

    def predict(self, x):
        return np.asarray([2.0])

    def predict_proba(self, x):
        return np.asarray([[0.05, 0.05, 0.90]])


def test_engine_decision_uses_same_information_vintage_and_stable_model_identity(monkeypatch):
    monkeypatch.setattr(hybrid_engine, "vectorize", lambda engine_input: [0.0])
    monkeypatch.setattr(
        hybrid_engine,
        "load_model",
        lambda: (
            _FakeModel(),
            {
                "shadow_ready": True,
                "model_id": "neural-model-abc",
                "model_revision": "rev-7",
                "trained_at": "2026-08-28T10:00:00+00:00",
                "samples": 500,
                "label_source": "perfect_information_policy_teacher_v2",
            },
        ),
    )
    rows = _rows()
    engine_input = EngineInput(
        generated_at="2026-08-28T11:59:30+00:00",
        decision_start=rows[0]["start"],
        initial_soc_pct=50.0,
        interval_minutes=15,
        horizon_rows=tuple(rows),
        constraints={},
        objective={},
        source={"kind": "test"},
    )

    decision = HybridV1Engine(_cfg()).decide(engine_input)
    assert decision.engine_id == "hybrid_v1"
    assert decision.family == "hybrid"
    assert decision.information_vintage_id == engine_input.information_vintage_id
    assert decision.decision_start == engine_input.decision_start
    assert -8.0 <= decision.requested_action_kw <= 8.0
    assert 5.0 <= decision.expected_soc_pct <= 100.0
    assert decision.model["model_id"].startswith("hybrid_v1:")
    assert decision.model["neural_model_id"] == "neural-model-abc"
    assert decision.model["qualification_required"] == "robust10_v1"
