from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib

from . import gradient_engine, gradient_training
from .neural_features import FEATURE_SCHEMA

CANDIDATE_STATE_PATH = Path("/data/models/gradient_v1_qualification.json")
POLICY_ID = "frozen_candidate_robust10_v1"
GRADIENT_ENGINE_ID = "gradient_v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _latest_meta() -> dict[str, Any]:
    return _read_json(gradient_training.MODEL_META_PATH)


def _version_model_path(model_id: str) -> Path:
    return gradient_training.MODEL_VERSIONS_DIR / f"{model_id}.joblib"


def _ensure_version_artifact(model_id: str) -> bool:
    target = _version_model_path(model_id)
    if target.exists():
        return True
    active = gradient_training.MODEL_PATH
    if not active.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    shutil.copy2(active, tmp)
    os.replace(tmp, target)
    return target.exists()


def _candidate_source_valid(candidate: dict[str, Any]) -> bool:
    model_id = candidate.get("model_id")
    return bool(
        model_id
        and candidate.get("feature_schema") == FEATURE_SCHEMA
        and candidate.get("shadow_ready")
        and _version_model_path(str(model_id)).exists()
    )


def _snapshot_latest(
    *,
    reason: str,
    previous: dict[str, Any] | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    latest = _latest_meta()
    if not latest:
        return {"ok": False, "status": "no_latest_model", "reason": reason}
    if not bool(latest.get("shadow_ready")):
        return {"ok": False, "status": "latest_model_not_shadow_ready", "reason": reason}
    if latest.get("feature_schema") != FEATURE_SCHEMA:
        return {"ok": False, "status": "latest_model_feature_schema_mismatch", "reason": reason}
    model_id = str(latest.get("model_id") or "")
    if not model_id or not _ensure_version_artifact(model_id):
        return {"ok": False, "status": "latest_version_artifact_missing", "reason": reason}

    prior = previous or _read_json(CANDIDATE_STATE_PATH)
    candidate = {
        **latest,
        "qualification_policy": POLICY_ID,
        "qualification_generation": int(prior.get("qualification_generation") or 0) + 1,
        "qualification_started_at": _now(),
        "qualification_frozen": True,
        "qualification_source_model_id": model_id,
        "qualification_source_model_revision": latest.get("model_revision"),
        "qualification_source_trained_at": latest.get("trained_at"),
        "qualification_source_training_samples": latest.get("samples"),
        "qualification_rotation_reason": str(reason),
        "qualification_rotation_details": details or {},
        "qualification_previous_model_id": prior.get("model_id"),
    }
    _atomic_write_json(CANDIDATE_STATE_PATH, candidate)
    return {"ok": True, "status": "candidate_snapshotted", "candidate": candidate}


def ensure_qualification_candidate() -> dict[str, Any]:
    candidate = _read_json(CANDIDATE_STATE_PATH)
    if _candidate_source_valid(candidate):
        return {"ok": True, "status": "candidate_frozen", "candidate": candidate}
    return _snapshot_latest(
        reason="initial_candidate_snapshot" if not candidate else "candidate_invalid_or_incompatible",
        previous=candidate or None,
    )


def load_qualification_model() -> tuple[Any, dict[str, Any]]:
    ensured = ensure_qualification_candidate()
    if not ensured.get("ok"):
        raise FileNotFoundError(f"no usable gradient qualification candidate: {ensured.get('status')}")
    meta = dict(ensured["candidate"])
    return joblib.load(_version_model_path(str(meta["model_id"]))), meta


def rotate_qualification_candidate(reason: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    current = _read_json(CANDIDATE_STATE_PATH)
    latest = _latest_meta()
    if not latest:
        return {"ok": False, "rotated": False, "status": "no_latest_model"}
    latest_id = str(latest.get("model_id") or "")
    current_id = str(current.get("model_id") or "")
    if latest_id and latest_id == current_id and _candidate_source_valid(current):
        return {
            "ok": True,
            "rotated": False,
            "status": "no_newer_latest_model",
            "candidate_model_id": current_id,
            "qualification_generation": current.get("qualification_generation"),
        }
    result = _snapshot_latest(reason=reason, previous=current or None, details=details)
    if not result.get("ok"):
        return {**result, "rotated": False}
    candidate = result["candidate"]
    return {
        "ok": True,
        "rotated": True,
        "status": "qualification_candidate_rotated",
        "candidate_model_id": candidate.get("model_id"),
        "qualification_generation": candidate.get("qualification_generation"),
        "qualification_started_at": candidate.get("qualification_started_at"),
        "previous_model_id": candidate.get("qualification_previous_model_id"),
        "reason": reason,
    }


def qualification_status() -> dict[str, Any]:
    latest = _latest_meta()
    ensured = ensure_qualification_candidate()
    candidate = dict(ensured.get("candidate") or {})
    latest_id = latest.get("model_id")
    candidate_id = candidate.get("model_id")
    return {
        "policy": POLICY_ID,
        "engine_id": GRADIENT_ENGINE_ID,
        "candidate_ready": bool(ensured.get("ok") and candidate_id),
        "candidate_status": ensured.get("status"),
        "qualification_generation": candidate.get("qualification_generation"),
        "qualification_started_at": candidate.get("qualification_started_at"),
        "candidate_model_id": candidate_id,
        "candidate_model_revision": candidate.get("model_revision"),
        "candidate_trained_at": candidate.get("trained_at"),
        "candidate_training_samples": candidate.get("samples"),
        "candidate_feature_schema": candidate.get("feature_schema"),
        "candidate_frozen": bool(candidate.get("qualification_frozen")),
        "latest_model_id": latest_id,
        "latest_model_revision": latest.get("model_revision"),
        "latest_trained_at": latest.get("trained_at"),
        "latest_training_samples": latest.get("samples"),
        "latest_differs_from_candidate": bool(latest_id and candidate_id and latest_id != candidate_id),
        "rotation_policy": "after completed failed robust10 qualification; never while gradient_v1 is incumbent",
    }


def install_qualification_candidate_runtime() -> dict[str, Any]:
    gradient_engine.load_model = load_qualification_model
    return qualification_status()
