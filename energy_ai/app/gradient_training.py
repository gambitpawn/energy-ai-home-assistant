from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import accuracy_score, mean_absolute_error

from . import neural_training
from .neural_features import FEATURE_NAMES, FEATURE_SCHEMA, feature_metadata
from .neural_training_v2 import _load_samples, sample_count

MODEL_DIR = Path("/data/models")
MODEL_PATH = MODEL_DIR / "gradient_v1.joblib"
MODEL_META_PATH = MODEL_DIR / "gradient_v1.json"
MODEL_VERSIONS_DIR = MODEL_DIR / "gradient_v1_versions"
ENGINE_ID = "gradient_v1"
MODEL_KIND = "sklearn_hist_gradient_boosting_classifier"
MODEL_VERSION = "1"
MIN_SHADOW_SAMPLES = 64
MIN_VALIDATION_SAMPLES = 12
LARGE_DATASET_THRESHOLD = 1000
DAILY_INTERVAL = timedelta(days=1)
WEEKLY_INTERVAL = timedelta(days=7)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _read_meta() -> dict[str, Any]:
    if not MODEL_META_PATH.exists():
        return {}
    try:
        return json.loads(MODEL_META_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _direction(value: float) -> int:
    return -1 if value < -0.5 else (1 if value > 0.5 else 0)


def train_model(trigger: str = "manual") -> dict[str, Any]:
    x, y, starts = _load_samples()
    n = len(y)
    if n < MIN_SHADOW_SAMPLES:
        return {
            "ok": False,
            "status": "insufficient_training_samples",
            "samples": n,
            "minimum_samples": MIN_SHADOW_SAMPLES,
            "shadow_ready": False,
        }
    classes = sorted({float(v) for v in y.tolist()})
    if len(classes) < 2:
        return {
            "ok": False,
            "status": "insufficient_action_diversity",
            "samples": n,
            "classes": classes,
            "shadow_ready": False,
        }

    split = max(1, int(round(n * 0.8)))
    split = min(split, n - MIN_VALIDATION_SAMPLES) if n > MIN_VALIDATION_SAMPLES else n - 1
    if split <= 0 or n - split < 1:
        return {
            "ok": False,
            "status": "insufficient_validation_samples",
            "samples": n,
            "shadow_ready": False,
        }
    x_train, x_val = x[:split], x[split:]
    y_train, y_val = y[:split], y[split:]
    train_classes = sorted({float(v) for v in y_train.tolist()})
    if len(train_classes) < 2:
        return {
            "ok": False,
            "status": "insufficient_train_action_diversity",
            "samples": n,
            "train_samples": len(y_train),
            "train_classes": train_classes,
            "shadow_ready": False,
        }

    model = HistGradientBoostingClassifier(
        learning_rate=0.06,
        max_iter=180,
        max_leaf_nodes=15,
        min_samples_leaf=8,
        l2_regularization=1.0,
        early_stopping=False,
        random_state=3517,
    )
    model.fit(x_train, y_train)
    pred = model.predict(x_val)
    accuracy = float(accuracy_score(y_val, pred))
    mae = float(mean_absolute_error(y_val, pred))
    direction_accuracy = sum(
        _direction(float(actual)) == _direction(float(predicted))
        for actual, predicted in zip(y_val, pred)
    ) / max(1, len(y_val))

    previous = _read_meta()
    revision = int(previous.get("model_revision") or 0) + 1
    model_id = f"{ENGINE_ID}-r{revision:04d}"
    trained_at = datetime.now(timezone.utc).isoformat()
    meta = {
        "engine_id": ENGINE_ID,
        "model_version": MODEL_VERSION,
        "model_revision": revision,
        "model_id": model_id,
        "model_kind": MODEL_KIND,
        "feature_schema": FEATURE_SCHEMA,
        "feature_count": len(FEATURE_NAMES),
        "label_source": neural_training.LABEL_SOURCE,
        "training_dataset": "shared_perfect_information_policy_teacher_samples",
        "trained_at": trained_at,
        "training_trigger": str(trigger),
        "samples": n,
        "train_samples": len(y_train),
        "validation_samples": len(y_val),
        "training_first": starts[0],
        "training_last": starts[-1],
        "observed_classes_kw": classes,
        "train_classes_kw": train_classes,
        "validation_accuracy": round(accuracy, 4),
        "validation_action_mae_kw": round(mae, 4),
        "validation_direction_accuracy": round(direction_accuracy, 4),
        "shadow_ready": True,
        "active_eligible": False,
        "active_eligibility_reason": "requires robust10_v1 head-to-head evidence",
        "qualification_required": "robust10_v1",
        "architecture": {
            "classifier": True,
            "algorithm": "hist_gradient_boosting",
            "learning_rate": 0.06,
            "max_iter": 180,
            "max_leaf_nodes": 15,
            "min_samples_leaf": 8,
            "l2_regularization": 1.0,
        },
    }

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    MODEL_VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
    version_model = MODEL_VERSIONS_DIR / f"{model_id}.joblib"
    version_meta = MODEL_VERSIONS_DIR / f"{model_id}.json"
    joblib.dump(model, version_model)
    _atomic_write_text(version_meta, json.dumps(meta, ensure_ascii=False, indent=2))

    active_tmp = MODEL_PATH.with_suffix(".joblib.tmp")
    shutil.copy2(version_model, active_tmp)
    os.replace(active_tmp, MODEL_PATH)
    _atomic_write_text(MODEL_META_PATH, json.dumps(meta, ensure_ascii=False, indent=2))
    return {"ok": True, "status": "trained", **meta}


def load_model() -> tuple[Any, dict[str, Any]]:
    if not MODEL_PATH.exists() or not MODEL_META_PATH.exists():
        raise FileNotFoundError("gradient_v1 model is not trained")
    model = joblib.load(MODEL_PATH)
    meta = _read_meta()
    if meta.get("feature_schema") != FEATURE_SCHEMA:
        raise RuntimeError("gradient model feature schema mismatch")
    return model, meta


def model_status() -> dict[str, Any]:
    result = {
        "engine_id": ENGINE_ID,
        "model_exists": MODEL_PATH.exists() and MODEL_META_PATH.exists(),
        "samples": sample_count(),
        "minimum_shadow_samples": MIN_SHADOW_SAMPLES,
        "feature": feature_metadata(),
        "label_source": neural_training.LABEL_SOURCE,
        "training_dataset": "shared_perfect_information_policy_teacher_samples",
        "shadow_ready": False,
        "active_eligible": False,
    }
    if MODEL_META_PATH.exists():
        try:
            result.update(_read_meta())
        except Exception as exc:
            result["model_meta_error"] = repr(exc)
    return result


def retraining_policy_status(now: datetime | None = None) -> dict[str, Any]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    current_samples = sample_count()
    meta = _read_meta()
    model_exists = MODEL_PATH.exists() and bool(meta)
    model_samples = int(meta.get("samples") or 0) if model_exists else 0
    trained_at = _utc(meta.get("trained_at")) if model_exists else None
    large = current_samples >= LARGE_DATASET_THRESHOLD
    interval = WEEKLY_INTERVAL if large else DAILY_INTERVAL
    next_eligible = None if trained_at is None else trained_at + interval
    new_samples = max(0, current_samples - model_samples)

    if current_samples < MIN_SHADOW_SAMPLES:
        due, reason = False, "minimum_training_samples_not_reached"
    elif not model_exists:
        due, reason = True, "first_model_ready_to_train"
    elif new_samples <= 0:
        due, reason = False, "no_new_samples_since_active_model"
    elif next_eligible is not None and now < next_eligible:
        due, reason = False, "cadence_interval_not_elapsed"
    else:
        due, reason = True, "new_samples_and_cadence_elapsed"

    return {
        "automatic_retraining_enabled": True,
        "dataset_samples": current_samples,
        "dataset_tier": "large" if large else "growing",
        "cadence": "weekly" if large else "daily",
        "minimum_interval_hours": int(interval.total_seconds() // 3600),
        "active_model_exists": model_exists,
        "active_model_id": meta.get("model_id"),
        "active_model_revision": meta.get("model_revision"),
        "active_model_training_samples": model_samples,
        "new_samples_since_active_model": new_samples,
        "last_trained_at": None if trained_at is None else trained_at.isoformat(),
        "next_retraining_eligible_at": None if next_eligible is None else next_eligible.isoformat(),
        "retraining_due": due,
        "reason": reason,
    }


def automatic_maintenance_once(cfg: dict[str, Any]) -> dict[str, Any]:
    policy_before = retraining_policy_status()
    if policy_before.get("retraining_due"):
        training = train_model(trigger="automatic")
    else:
        training = {
            "ok": True,
            "status": "not_due",
            "reason": policy_before.get("reason"),
            "shadow_ready": bool(model_status().get("shadow_ready")),
        }
    return {
        "last_maintenance_at": datetime.now(timezone.utc).isoformat(),
        "shared_training_samples": sample_count(),
        "policy_before": policy_before,
        "training": training,
        "policy_after": retraining_policy_status(),
    }
