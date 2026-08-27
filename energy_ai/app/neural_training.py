from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from sklearn.metrics import accuracy_score, mean_absolute_error
from sklearn.neural_network import MLPClassifier
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from .app_comparison import _actual_rows
from .db import DB_PATH
from .engine_contract import EngineInput, input_from_optimizer_plan
from .neural_features import FEATURE_NAMES, FEATURE_SCHEMA, feature_metadata, vectorize
from .optimizer_evaluation import DECISION_GRACE_SECONDS, MAX_PLAN_AGE_MINUTES
from .optimizer_v35_replay import solve_v35_from_rows

MODEL_DIR = Path("/data/models")
MODEL_PATH = MODEL_DIR / "neural_v1.joblib"
MODEL_META_PATH = MODEL_DIR / "neural_v1.json"
ENGINE_ID = "neural_v1"
MODEL_KIND = "sklearn_mlp_classifier"
LABEL_SOURCE = "perfect_information_v35_teacher_v1"
ACTION_CLASSES_KW = tuple(float(x) for x in range(-8, 9))
MIN_SHADOW_SAMPLES = 64
MIN_VALIDATION_SAMPLES = 12


def _utc(value: str) -> datetime:
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _init_tables() -> None:
    with sqlite3.connect(DB_PATH) as c:
        c.executescript(
            '''
            CREATE TABLE IF NOT EXISTS neural_training_sample(
                information_vintage_id TEXT PRIMARY KEY,
                decision_start TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                label_source TEXT NOT NULL,
                teacher_action_kw REAL NOT NULL,
                label_action_kw REAL NOT NULL,
                feature_schema TEXT NOT NULL,
                features_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_neural_sample_start
                ON neural_training_sample(decision_start);
            '''
        )


def _round_action(value: float) -> float:
    value = max(min(float(value), max(ACTION_CLASSES_KW)), min(ACTION_CLASSES_KW))
    return min(ACTION_CLASSES_KW, key=lambda x: abs(x - value))


def _engine_input_from_payload(payload: dict[str, Any]) -> EngineInput:
    return EngineInput(
        generated_at=str(payload["generated_at"]),
        decision_start=str(payload["decision_start"]),
        initial_soc_pct=float(payload["initial_soc_pct"]),
        interval_minutes=int(payload.get("interval_minutes") or 15),
        horizon_rows=tuple(payload.get("horizon_rows") or ()),
        constraints=dict(payload.get("constraints") or {}),
        objective=dict(payload.get("objective") or {}),
        source=dict(payload.get("source") or {}),
        information_vintage_id=str(payload.get("information_vintage_id") or ""),
    )


def _candidate_inputs(cfg: dict[str, Any], limit: int = 1000) -> tuple[list[EngineInput], dict[str, Any]]:
    """Return one canonical information vintage per 15-minute decision opportunity.

    Multiple manual/restart refreshes may create several vintages for the same
    decision_start. Training on all of them would overweight that interval and can
    leak near-duplicates across chronological train/validation splits. We therefore
    use the freshest vintage inside the same -30m/+180s live timing window.
    """
    _init_tables()
    seen_vintages: set[str] = set()
    raw_candidates: list[EngineInput] = []
    contract_rows = 0
    legacy_rows = 0
    with sqlite3.connect(DB_PATH) as c:
        try:
            rows = c.execute(
                "SELECT payload_json FROM engine_information_vintage ORDER BY decision_start DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        for (raw,) in rows:
            try:
                item = _engine_input_from_payload(json.loads(raw))
            except Exception:
                continue
            contract_rows += 1
            if item.information_vintage_id not in seen_vintages:
                seen_vintages.add(item.information_vintage_id)
                raw_candidates.append(item)

        try:
            legacy = c.execute(
                "SELECT payload_json FROM optimizer_plan_summary ORDER BY generated_at DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        except sqlite3.OperationalError:
            legacy = []
        for (raw,) in legacy:
            try:
                plan = json.loads(raw)
                if str(plan.get("planner")) != "deterministic_battery_dp_v3_5":
                    continue
                item = input_from_optimizer_plan(plan, cfg)
            except Exception:
                continue
            legacy_rows += 1
            if item.information_vintage_id not in seen_vintages:
                seen_vintages.add(item.information_vintage_id)
                raw_candidates.append(item)

    canonical: dict[str, EngineInput] = {}
    timing_rejected = 0
    for item in raw_candidates:
        start = _utc(item.decision_start)
        generated = _utc(item.generated_at)
        lag = (generated - start).total_seconds()
        if lag > DECISION_GRACE_SECONDS or lag < -MAX_PLAN_AGE_MINUTES * 60:
            timing_rejected += 1
            continue
        key = start.isoformat()
        current = canonical.get(key)
        if current is None or generated > _utc(current.generated_at):
            canonical[key] = item

    candidates = sorted(canonical.values(), key=lambda x: _utc(x.decision_start))
    return candidates, {
        "contract_rows_seen": contract_rows,
        "legacy_v35_rows_seen": legacy_rows,
        "unique_information_vintages_seen": len(raw_candidates),
        "timing_rejected_vintages": timing_rejected,
        "canonical_decision_intervals": len(candidates),
        "duplicate_vintages_removed": max(0, len(raw_candidates) - timing_rejected - len(candidates)),
        "canonical_rule": "freshest information vintage per decision_start within -30m/+180s live timing window",
    }


def _latest_actual_start() -> datetime | None:
    try:
        with sqlite3.connect(DB_PATH) as c:
            row = c.execute("SELECT MAX(bucket_start) FROM state_15m").fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or not row[0]:
        return None
    try:
        return _utc(str(row[0]))
    except Exception:
        return None


def _required_actual_until(engine_input: EngineInput) -> datetime:
    last = _utc(engine_input.horizon_rows[-1]["start"])
    return last + timedelta(minutes=int(engine_input.interval_minutes))


def training_maturity_status(cfg: dict[str, Any], candidate_limit: int = 2000) -> dict[str, Any]:
    candidates, candidate_diag = _candidate_inputs(cfg, candidate_limit)
    latest = _latest_actual_start()
    latest_complete_until = None if latest is None else latest + timedelta(minutes=15)
    existing: set[str]
    _init_tables()
    with sqlite3.connect(DB_PATH) as c:
        existing = {r[0] for r in c.execute("SELECT information_vintage_id FROM neural_training_sample").fetchall()}

    pending = [c for c in candidates if c.information_vintage_id not in existing]
    mature = []
    immature = []
    for item in pending:
        required_until = _required_actual_until(item)
        if latest_complete_until is not None and required_until <= latest_complete_until:
            mature.append(item)
        else:
            immature.append(item)

    return {
        "engine_id": ENGINE_ID,
        "label_source": LABEL_SOURCE,
        "candidate_diagnostics": candidate_diag,
        "training_samples_existing": len(existing),
        "pending_canonical_candidates": len(pending),
        "chronologically_mature_pending_candidates": len(mature),
        "not_yet_mature_pending_candidates": len(immature),
        "first_canonical_decision_start": candidates[0].decision_start if candidates else None,
        "last_canonical_decision_start": candidates[-1].decision_start if candidates else None,
        "earliest_required_actual_until": _required_actual_until(candidates[0]).isoformat() if candidates else None,
        "latest_available_actual_start": None if latest is None else latest.isoformat(),
        "latest_available_actual_complete_until": None if latest_complete_until is None else latest_complete_until.isoformat(),
        "next_pending_required_actual_until": _required_actual_until(immature[0]).isoformat() if immature else None,
        "note": "chronological maturity only; a mature candidate can still fail teacher construction if one or more required load/PV/price intervals are missing",
    }


def _perfect_information_teacher(cfg: dict[str, Any], engine_input: EngineInput) -> tuple[float, dict[str, Any]] | None:
    horizon = list(engine_input.horizon_rows)
    if not horizon:
        return None
    start = _utc(horizon[0]["start"])
    last = _utc(horizon[-1]["start"])
    end = last + timedelta(minutes=engine_input.interval_minutes)
    actual, data = _actual_rows(start, end)
    actual_map = {_utc(r["start"]): r for r in actual}
    if len(actual_map) != len(horizon):
        return None

    injected: list[dict[str, Any]] = []
    for row in horizon:
        stamp = _utc(row["start"])
        observed = actual_map.get(stamp)
        if observed is None:
            return None
        item = dict(row)
        item["load_kw"] = float(observed["load_kw"])
        item["pv_kw"] = float(observed["pv_kw"])
        item["load_uncertainty_kw"] = 0.0
        item["pv_uncertainty_kw"] = 0.0
        item["price_known"] = True
        item["price_ore_kwh"] = float(observed["price_ore_kwh"])
        injected.append(item)

    solved = solve_v35_from_rows(cfg, injected, float(engine_input.initial_soc_pct))
    return float(solved["first_action_kw"]), {
        "actual_coverage_fraction": data.get("actual_coverage_fraction"),
        "teacher_terminal_soc_pct": solved.get("terminal_soc_pct"),
        "teacher_objective_cost_ore": solved.get("objective_cost_ore"),
    }


def build_training_samples(cfg: dict[str, Any], max_new: int = 32, candidate_limit: int = 1500) -> dict[str, Any]:
    _init_tables()
    max_new = max(1, min(int(max_new), 256))
    with sqlite3.connect(DB_PATH) as c:
        existing = {r[0] for r in c.execute("SELECT information_vintage_id FROM neural_training_sample").fetchall()}

    candidates, candidate_diag = _candidate_inputs(cfg, candidate_limit)
    latest_actual = _latest_actual_start()
    latest_complete_until = None if latest_actual is None else latest_actual + timedelta(minutes=15)
    created = 0
    not_yet_mature = 0
    coverage_gaps = 0
    failures = 0
    now = datetime.now(timezone.utc).isoformat()
    first_pending_required_until: datetime | None = None

    for engine_input in candidates:
        if created >= max_new:
            break
        if engine_input.information_vintage_id in existing:
            continue
        required_until = _required_actual_until(engine_input)
        if latest_complete_until is None or required_until > latest_complete_until:
            not_yet_mature += 1
            if first_pending_required_until is None:
                first_pending_required_until = required_until
            continue
        try:
            teacher = _perfect_information_teacher(cfg, engine_input)
            if teacher is None:
                coverage_gaps += 1
                continue
            teacher_action, _diag = teacher
            label = _round_action(teacher_action)
            features = vectorize(engine_input)
            with sqlite3.connect(DB_PATH) as c:
                c.execute(
                    '''INSERT OR REPLACE INTO neural_training_sample(
                       information_vintage_id,decision_start,generated_at,label_source,
                       teacher_action_kw,label_action_kw,feature_schema,features_json,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?)''',
                    (
                        engine_input.information_vintage_id,
                        engine_input.decision_start,
                        engine_input.generated_at,
                        LABEL_SOURCE,
                        teacher_action,
                        label,
                        FEATURE_SCHEMA,
                        json.dumps(features, separators=(",", ":")),
                        now,
                    ),
                )
            existing.add(engine_input.information_vintage_id)
            created += 1
        except Exception:
            failures += 1

    return {
        "ok": True,
        "label_source": LABEL_SOURCE,
        "created_samples": created,
        "not_yet_mature_candidates": not_yet_mature,
        "mature_but_incomplete_coverage_candidates": coverage_gaps,
        "failures": failures,
        "total_samples": sample_count(),
        "feature_schema": FEATURE_SCHEMA,
        "action_classes_kw": list(ACTION_CLASSES_KW),
        "candidate_diagnostics": candidate_diag,
        "maturity": {
            "latest_available_actual_start": None if latest_actual is None else latest_actual.isoformat(),
            "latest_available_actual_complete_until": None if latest_complete_until is None else latest_complete_until.isoformat(),
            "next_pending_required_actual_until": None if first_pending_required_until is None else first_pending_required_until.isoformat(),
        },
    }


def sample_count() -> int:
    _init_tables()
    with sqlite3.connect(DB_PATH) as c:
        return int(c.execute("SELECT COUNT(*) FROM neural_training_sample").fetchone()[0])


def _load_samples() -> tuple[np.ndarray, np.ndarray, list[str]]:
    _init_tables()
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            "SELECT decision_start,label_action_kw,features_json FROM neural_training_sample ORDER BY decision_start"
        ).fetchall()
    if not rows:
        return np.empty((0, len(FEATURE_NAMES))), np.empty((0,)), []
    x = np.asarray([json.loads(r[2]) for r in rows], dtype=float)
    y = np.asarray([float(r[1]) for r in rows], dtype=float)
    starts = [str(r[0]) for r in rows]
    return x, y, starts


def train_model() -> dict[str, Any]:
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
        return {"ok": False, "status": "insufficient_validation_samples", "samples": n, "shadow_ready": False}
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

    pipeline = Pipeline([
        ("scale", StandardScaler()),
        ("mlp", MLPClassifier(
            hidden_layer_sizes=(64, 32),
            activation="relu",
            solver="adam",
            alpha=0.001,
            batch_size=min(32, max(8, split // 4)),
            learning_rate_init=0.001,
            max_iter=500,
            early_stopping=False,
            random_state=3501,
        )),
    ])
    pipeline.fit(x_train, y_train)
    pred = pipeline.predict(x_val)
    accuracy = float(accuracy_score(y_val, pred))
    mae = float(mean_absolute_error(y_val, pred))
    direction = lambda v: -1 if v < -0.5 else (1 if v > 0.5 else 0)
    direction_accuracy = sum(direction(a) == direction(b) for a, b in zip(y_val, pred)) / max(1, len(y_val))

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(pipeline, MODEL_PATH)
    meta = {
        "engine_id": ENGINE_ID,
        "model_version": "1",
        "model_kind": MODEL_KIND,
        "feature_schema": FEATURE_SCHEMA,
        "feature_count": len(FEATURE_NAMES),
        "label_source": LABEL_SOURCE,
        "action_classes_kw": list(ACTION_CLASSES_KW),
        "trained_at": datetime.now(timezone.utc).isoformat(),
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
        "active_eligibility_reason": "requires multi-day closed-loop head-to-head evidence against deterministic_v35",
        "architecture": {"hidden_layers": [64, 32], "activation": "relu", "classifier": True},
    }
    MODEL_META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "status": "trained", **meta}


def load_model() -> tuple[Any, dict[str, Any]]:
    if not MODEL_PATH.exists() or not MODEL_META_PATH.exists():
        raise FileNotFoundError("neural_v1 model is not trained")
    model = joblib.load(MODEL_PATH)
    meta = json.loads(MODEL_META_PATH.read_text(encoding="utf-8"))
    if meta.get("feature_schema") != FEATURE_SCHEMA:
        raise RuntimeError("neural model feature schema mismatch")
    return model, meta


def model_status() -> dict[str, Any]:
    result = {
        "engine_id": ENGINE_ID,
        "model_exists": MODEL_PATH.exists() and MODEL_META_PATH.exists(),
        "samples": sample_count(),
        "minimum_shadow_samples": MIN_SHADOW_SAMPLES,
        "feature": feature_metadata(),
        "label_source": LABEL_SOURCE,
        "shadow_ready": False,
        "active_eligible": False,
    }
    if MODEL_META_PATH.exists():
        try:
            result.update(json.loads(MODEL_META_PATH.read_text(encoding="utf-8")))
        except Exception as exc:
            result["model_metadata_error"] = repr(exc)
    return result
