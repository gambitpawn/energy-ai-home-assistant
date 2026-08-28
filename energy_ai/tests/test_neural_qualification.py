from __future__ import annotations

import json
from pathlib import Path

import joblib

from app import neural_qualification as nq
from app import neural_training
from app.model_selector_robust_hardening import _learned_candidate_rotation_ready
from app.neural_features import FEATURE_SCHEMA


def _write_latest(tmp_path: Path, model_id: str, revision: int, samples: int) -> None:
    model_dir = tmp_path / "models"
    versions = model_dir / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    meta = {
        "engine_id": "neural_v1",
        "model_id": model_id,
        "model_revision": revision,
        "model_version": "1",
        "model_kind": "test",
        "feature_schema": FEATURE_SCHEMA,
        "shadow_ready": True,
        "trained_at": f"2026-08-{20 + revision:02d}T10:00:00+00:00",
        "samples": samples,
        "label_source": "test_teacher",
    }
    (model_dir / "neural_v1.json").write_text(json.dumps(meta), encoding="utf-8")
    joblib.dump({"model_id": model_id}, versions / f"{model_id}.joblib")


def _patch_paths(monkeypatch, tmp_path: Path) -> None:
    model_dir = tmp_path / "models"
    monkeypatch.setattr(neural_training, "MODEL_META_PATH", model_dir / "neural_v1.json")
    monkeypatch.setattr(neural_training, "MODEL_PATH", model_dir / "neural_v1.joblib")
    monkeypatch.setattr(neural_training, "MODEL_VERSIONS_DIR", model_dir / "versions")
    monkeypatch.setattr(nq, "CANDIDATE_STATE_PATH", model_dir / "neural_v1_qualification.json")


def test_latest_training_can_advance_without_changing_frozen_candidate(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    _write_latest(tmp_path, "neural_v1-r0001", 1, 100)

    first = nq.ensure_qualification_candidate()
    assert first["ok"] is True
    assert first["candidate"]["model_id"] == "neural_v1-r0001"
    assert first["candidate"]["qualification_generation"] == 1

    _write_latest(tmp_path, "neural_v1-r0002", 2, 196)
    still_frozen = nq.ensure_qualification_candidate()
    status = nq.qualification_status()

    assert still_frozen["candidate"]["model_id"] == "neural_v1-r0001"
    assert status["latest_model_id"] == "neural_v1-r0002"
    assert status["latest_differs_from_candidate"] is True

    model, meta = nq.load_qualification_model()
    assert model["model_id"] == "neural_v1-r0001"
    assert meta["model_id"] == "neural_v1-r0001"


def test_failed_qualification_rotates_to_latest_model(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    _write_latest(tmp_path, "neural_v1-r0001", 1, 100)
    nq.ensure_qualification_candidate()
    _write_latest(tmp_path, "neural_v1-r0003", 3, 300)

    rotated = nq.rotate_qualification_candidate("completed_robust10_without_promotion")
    assert rotated["rotated"] is True
    assert rotated["candidate_model_id"] == "neural_v1-r0003"
    assert rotated["previous_model_id"] == "neural_v1-r0001"
    assert rotated["qualification_generation"] == 2


def test_rotation_gate_waits_for_all_present_learned_engines_to_complete_ten_days():
    neural = {"challenger_engine_id": "neural_v1", "paired_days": 10, "eligible": False}
    hybrid_nine = {"challenger_engine_id": "hybrid_v1", "paired_days": 9, "eligible": False}
    hybrid_ten = {"challenger_engine_id": "hybrid_v1", "paired_days": 10, "eligible": False}

    assert _learned_candidate_rotation_ready([neural, hybrid_nine]) is False
    assert _learned_candidate_rotation_ready([neural, hybrid_ten]) is True
    assert _learned_candidate_rotation_ready([{**neural, "eligible": True}, hybrid_ten]) is False


def test_runtime_patch_points_neural_and_hybrid_at_same_candidate_loader(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, tmp_path)
    _write_latest(tmp_path, "neural_v1-r0001", 1, 100)
    result = nq.install_qualification_candidate_runtime()

    assert result["candidate_ready"] is True
    assert nq.neural_engine.load_model is nq.load_qualification_model
    assert nq.hybrid_engine.load_model is nq.load_qualification_model
