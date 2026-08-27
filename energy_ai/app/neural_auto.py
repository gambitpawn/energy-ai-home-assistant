from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .neural_training import build_training_samples, model_status, sample_count, train_model

AUTO_STATUS_PATH = Path("/data/models/neural_auto_status.json")
LARGE_DATASET_THRESHOLD = 1000
DAILY_INTERVAL = timedelta(days=1)
WEEKLY_INTERVAL = timedelta(days=7)
AUTO_SAMPLE_MAX_NEW = 256
AUTO_CANDIDATE_LIMIT = 5000


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


def _write_status(payload: dict[str, Any]) -> None:
    AUTO_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = AUTO_STATUS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(AUTO_STATUS_PATH)


def _read_status() -> dict[str, Any]:
    if not AUTO_STATUS_PATH.exists():
        return {}
    try:
        return json.loads(AUTO_STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def retraining_policy_status(now: datetime | None = None) -> dict[str, Any]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    current_samples = sample_count()
    meta = model_status()
    model_exists = bool(meta.get("model_exists"))
    model_samples = int(meta.get("samples") or 0) if model_exists else 0
    # model_status merges live sample_count before model metadata. The persisted
    # training sample count is therefore read from model metadata explicitly below.
    try:
        if model_exists:
            raw = json.loads(Path("/data/models/neural_v1.json").read_text(encoding="utf-8"))
            model_samples = int(raw.get("samples") or 0)
            trained_at = _utc(raw.get("trained_at"))
            model_id = raw.get("model_id")
            model_revision = raw.get("model_revision")
        else:
            trained_at = None
            model_id = None
            model_revision = None
    except Exception:
        trained_at = _utc(meta.get("trained_at"))
        model_id = meta.get("model_id")
        model_revision = meta.get("model_revision")

    large = current_samples >= LARGE_DATASET_THRESHOLD
    interval = WEEKLY_INTERVAL if large else DAILY_INTERVAL
    cadence = "weekly" if large else "daily"
    next_eligible = None if trained_at is None else trained_at + interval
    new_samples = max(0, current_samples - model_samples)

    if current_samples < 64:
        due = False
        reason = "minimum_training_samples_not_reached"
    elif not model_exists:
        due = True
        reason = "first_model_ready_to_train"
    elif new_samples <= 0:
        due = False
        reason = "no_new_samples_since_active_model"
    elif next_eligible is not None and now < next_eligible:
        due = False
        reason = "cadence_interval_not_elapsed"
    else:
        due = True
        reason = "new_samples_and_cadence_elapsed"

    return {
        "automatic_retraining_enabled": True,
        "dataset_samples": current_samples,
        "large_dataset_threshold": LARGE_DATASET_THRESHOLD,
        "dataset_tier": "large" if large else "growing",
        "cadence": cadence,
        "minimum_interval_hours": int(interval.total_seconds() // 3600),
        "active_model_exists": model_exists,
        "active_model_id": model_id,
        "active_model_revision": model_revision,
        "active_model_training_samples": model_samples,
        "new_samples_since_active_model": new_samples,
        "last_trained_at": None if trained_at is None else trained_at.isoformat(),
        "next_retraining_eligible_at": None if next_eligible is None else next_eligible.isoformat(),
        "retraining_due": due,
        "reason": reason,
    }


def automatic_maintenance_once(cfg: dict[str, Any]) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    samples = build_training_samples(cfg, AUTO_SAMPLE_MAX_NEW, AUTO_CANDIDATE_LIMIT)
    policy_before = retraining_policy_status(started)
    training: dict[str, Any]
    if policy_before.get("retraining_due"):
        training = train_model(trigger="automatic")
    else:
        training = {
            "ok": True,
            "status": "not_due",
            "reason": policy_before.get("reason"),
            "shadow_ready": bool(model_status().get("shadow_ready")),
        }
    policy_after = retraining_policy_status(datetime.now(timezone.utc))
    result = {
        "last_maintenance_at": datetime.now(timezone.utc).isoformat(),
        "samples": samples,
        "policy_before": policy_before,
        "training": training,
        "policy_after": policy_after,
    }
    _write_status(result)
    return result


def automatic_status() -> dict[str, Any]:
    return {
        "policy": retraining_policy_status(),
        "last_maintenance": _read_status(),
        "sample_collection": {
            "automatic": True,
            "maintenance_interval_hours": 1,
            "max_new_samples_per_run": AUTO_SAMPLE_MAX_NEW,
            "candidate_limit": AUTO_CANDIDATE_LIMIT,
        },
        "retraining_policy": {
            "growing_dataset": "at most once per 24 hours",
            "large_dataset": "at most once per 7 days",
            "large_dataset_threshold_samples": LARGE_DATASET_THRESHOLD,
            "requires_new_samples": True,
            "first_model": "train as soon as minimum model requirements are met",
        },
    }
