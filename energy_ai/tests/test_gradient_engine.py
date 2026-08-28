from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import joblib
import numpy as np

from app import gradient_engine, gradient_qualification, gradient_training
from app.engine_contract import EngineInput
from app.engine_registry import descriptor
from app.neural_features import FEATURE_NAMES, FEATURE_SCHEMA


def _engine_input() -> EngineInput:
    start = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(8):
        rows.append({
            "start": (start + timedelta(minutes=15 * i)).isoformat(),
            "load_kw": 2.0,
            "base_load_kw": 2.0,
            "component_forecast_kw": {},
            "load_uncertainty_kw": 0.4,
            "pv_kw": 1.0,
            "pv_uncertainty_kw": 0.3,
            "price_known": True,
            "price_ore_kwh": 100.0 + i,
        })
    return EngineInput(
        generated_at="2026-08-28T11:59:00+00:00",
        decision_start=rows[0]["start"],
        initial_soc_pct=50.0,
        interval_minutes=15,
        horizon_rows=tuple(rows),
        constraints={},
        objective={},
        source={"kind": "test", "input_profile": "generalized_installation_tariff_v2"},
    )


def test_registry_marks_gradient_as_trainable_available_challenger():
    item = descriptor("gradient_v1")
    assert item.available is True
    assert item.trainable is True
    assert item.learning_enabled is True
    assert item.baseline is False


def test_gradient_training_uses_shared_feature_width_and_temporal_split(tmp_path, monkeypatch):
    rng = np.random.default_rng(3517)
    n = 90
    x = rng.normal(size=(n, len(FEATURE_NAMES)))
    # Deliberately easy multi-class relation with all classes represented through
    # both chronological train and validation partitions.
    y = np.asarray([-2.0, 0.0, 2.0] * 30, dtype=float)
    starts = [f"2026-08-{1 + i // 24:02d}T{(i % 24):02d}:00:00+00:00" for i in range(n)]
    monkeypatch.setattr(gradient_training, "_load_samples", lambda: (x, y, starts))
    monkeypatch.setattr(gradient_training, "MODEL_DIR", tmp_path)
    monkeypatch.setattr(gradient_training, "MODEL_PATH", tmp_path / "gradient_v1.joblib")
    monkeypatch.setattr(gradient_training, "MODEL_META_PATH", tmp_path / "gradient_v1.json")
    monkeypatch.setattr(gradient_training, "MODEL_VERSIONS_DIR", tmp_path / "versions")

    result = gradient_training.train_model("test")
    assert result["ok"] is True
    assert result["model_kind"] == "sklearn_hist_gradient_boosting_classifier"
    assert result["feature_schema"] == FEATURE_SCHEMA
    assert result["samples"] == n
    assert result["train_samples"] < n
    assert result["validation_samples"] >= 12
    assert (tmp_path / "gradient_v1.joblib").exists()
    assert (tmp_path / "versions" / f"{result['model_id']}.joblib").exists()


def test_gradient_engine_returns_same_vintage_and_probability_diagnostics(monkeypatch):
    class FakeModel:
        classes_ = np.asarray([-2.0, 0.0, 2.0])

        def predict(self, x):
            return np.asarray([2.0])

        def predict_proba(self, x):
            return np.asarray([[0.05, 0.10, 0.85]])

    monkeypatch.setattr(gradient_engine, "vectorize", lambda value: [0.0] * len(FEATURE_NAMES))
    monkeypatch.setattr(
        gradient_engine,
        "load_model",
        lambda: (
            FakeModel(),
            {
                "model_kind": "sklearn_hist_gradient_boosting_classifier",
                "model_version": "1",
                "model_revision": 3,
                "model_id": "gradient_v1-r0003",
                "trained_at": "2026-08-28T10:00:00+00:00",
                "samples": 500,
                "label_source": "perfect_information_policy_teacher_v2",
                "validation_accuracy": 0.7,
                "validation_action_mae_kw": 1.2,
                "validation_direction_accuracy": 0.8,
                "shadow_ready": True,
            },
        ),
    )
    cfg = {
        "policy": {"battery": {"capacity_kwh": 20.0}},
        "optimizer": {
            "battery_charge_efficiency": 0.95,
            "battery_discharge_efficiency": 0.95,
        },
    }
    engine_input = _engine_input()
    decision = gradient_engine.GradientV1Engine(cfg).decide(engine_input)
    assert decision.engine_id == "gradient_v1"
    assert decision.information_vintage_id == engine_input.information_vintage_id
    assert decision.requested_action_kw == 2.0
    assert decision.diagnostics["classification_confidence"] == 0.85
    assert decision.model["model_id"] == "gradient_v1-r0003"
    assert decision.model["qualification_required"] == "robust10_v1"


def test_gradient_qualification_stays_frozen_while_latest_retrains(tmp_path, monkeypatch):
    model_path = tmp_path / "gradient_v1.joblib"
    meta_path = tmp_path / "gradient_v1.json"
    versions = tmp_path / "versions"
    state_path = tmp_path / "gradient_v1_qualification.json"
    versions.mkdir()

    monkeypatch.setattr(gradient_training, "MODEL_PATH", model_path)
    monkeypatch.setattr(gradient_training, "MODEL_META_PATH", meta_path)
    monkeypatch.setattr(gradient_training, "MODEL_VERSIONS_DIR", versions)
    monkeypatch.setattr(gradient_qualification, "CANDIDATE_STATE_PATH", state_path)

    def publish(model_id: str, revision: int):
        meta = {
            "model_id": model_id,
            "model_revision": revision,
            "model_version": "1",
            "model_kind": "sklearn_hist_gradient_boosting_classifier",
            "feature_schema": FEATURE_SCHEMA,
            "shadow_ready": True,
            "trained_at": f"2026-08-{20 + revision:02d}T00:00:00+00:00",
            "samples": 100 + revision,
        }
        joblib.dump({"revision": revision}, model_path)
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        return meta

    publish("gradient_v1-r0001", 1)
    first = gradient_qualification.ensure_qualification_candidate()
    assert first["candidate"]["model_id"] == "gradient_v1-r0001"

    publish("gradient_v1-r0002", 2)
    status = gradient_qualification.qualification_status()
    assert status["candidate_model_id"] == "gradient_v1-r0001"
    assert status["latest_model_id"] == "gradient_v1-r0002"
    assert status["latest_differs_from_candidate"] is True

    rotation = gradient_qualification.rotate_qualification_candidate("test_completed_window")
    assert rotation["rotated"] is True
    assert gradient_qualification.qualification_status()["candidate_model_id"] == "gradient_v1-r0002"
