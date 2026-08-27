from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

import numpy as np

from .db import DB_PATH
from .engine_contract import EngineInput
from .engine_input_v2 import input_from_optimizer_plan_v2
from .neural_features import FEATURE_NAMES, FEATURE_SCHEMA, vectorize
from . import neural_training as v1


def sample_count() -> int:
    v1._init_tables()
    with sqlite3.connect(DB_PATH) as c:
        return int(c.execute("SELECT COUNT(*) FROM neural_training_sample WHERE feature_schema=?", (FEATURE_SCHEMA,)).fetchone()[0])


def _load_samples() -> tuple[np.ndarray, np.ndarray, list[str]]:
    v1._init_tables()
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            "SELECT decision_start,label_action_kw,features_json FROM neural_training_sample WHERE feature_schema=? ORDER BY decision_start",
            (FEATURE_SCHEMA,),
        ).fetchall()
    if not rows:
        return np.empty((0, len(FEATURE_NAMES))), np.empty((0,)), []
    x = np.asarray([json.loads(r[2]) for r in rows], dtype=float)
    y = np.asarray([float(r[1]) for r in rows], dtype=float)
    starts = [str(r[0]) for r in rows]
    if x.shape[1] != len(FEATURE_NAMES):
        raise RuntimeError(f"v2 feature width mismatch: {x.shape[1]} != {len(FEATURE_NAMES)}")
    return x, y, starts


def _candidate_inputs(cfg: dict[str, Any], limit: int = 1000) -> tuple[list[EngineInput], dict[str, Any]]:
    v1._init_tables()
    seen: set[str] = set()
    raw: list[EngineInput] = []
    contract_seen = 0
    contract_v2 = 0
    legacy_seen = 0
    with sqlite3.connect(DB_PATH) as c:
        try:
            rows = c.execute("SELECT payload_json FROM engine_information_vintage ORDER BY decision_start DESC LIMIT ?", (max(1, int(limit)),)).fetchall()
        except sqlite3.OperationalError:
            rows = []
        for (payload_raw,) in rows:
            contract_seen += 1
            try:
                payload = json.loads(payload_raw)
                if ((payload.get("source") or {}).get("input_profile")) != "generalized_installation_tariff_v2":
                    continue
                item = v1._engine_input_from_payload(payload)
            except Exception:
                continue
            contract_v2 += 1
            if item.information_vintage_id not in seen:
                seen.add(item.information_vintage_id); raw.append(item)

        try:
            rows = c.execute("SELECT payload_json FROM optimizer_plan_summary ORDER BY generated_at DESC LIMIT ?", (max(1, int(limit)),)).fetchall()
        except sqlite3.OperationalError:
            rows = []
        for (payload_raw,) in rows:
            try:
                plan = json.loads(payload_raw)
                if str(plan.get("planner")) != "deterministic_battery_dp_v3_5":
                    continue
                item = input_from_optimizer_plan_v2(plan, cfg)
            except Exception:
                continue
            legacy_seen += 1
            if item.information_vintage_id not in seen:
                seen.add(item.information_vintage_id); raw.append(item)

    canonical: dict[str, EngineInput] = {}
    timing_rejected = 0
    for item in raw:
        start = v1._utc(item.decision_start); generated = v1._utc(item.generated_at)
        lag = (generated - start).total_seconds()
        if lag > v1.DECISION_GRACE_SECONDS or lag < -v1.MAX_PLAN_AGE_MINUTES * 60:
            timing_rejected += 1; continue
        key = start.isoformat(); current = canonical.get(key)
        if current is None or generated > v1._utc(current.generated_at):
            canonical[key] = item
    candidates = sorted(canonical.values(), key=lambda x: v1._utc(x.decision_start))
    return candidates, {
        "contract_rows_seen": contract_seen,
        "contract_v2_rows_used": contract_v2,
        "legacy_v35_rows_seen": legacy_seen,
        "unique_v2_information_vintages_seen": len(raw),
        "timing_rejected_vintages": timing_rejected,
        "canonical_decision_intervals": len(candidates),
        "duplicate_vintages_removed": max(0, len(raw) - timing_rejected - len(candidates)),
        "canonical_rule": "freshest generalized v2 information vintage per decision_start within -30m/+180s live timing window",
    }


def build_training_samples(cfg: dict[str, Any], max_new: int = 32, candidate_limit: int = 1500) -> dict[str, Any]:
    v1._init_tables(); max_new=max(1,min(int(max_new),256))
    with sqlite3.connect(DB_PATH) as c:
        existing={r[0] for r in c.execute("SELECT information_vintage_id FROM neural_training_sample WHERE feature_schema=?",(FEATURE_SCHEMA,)).fetchall()}
    candidates, diag=_candidate_inputs(cfg,candidate_limit)
    latest=v1._latest_actual_start(); complete=None if latest is None else latest + v1.timedelta(minutes=15)
    created=not_yet=coverage=failures=0; next_required=None; now=datetime.now(timezone.utc).isoformat()
    for item in candidates:
        if created>=max_new: break
        if item.information_vintage_id in existing: continue
        required=v1._required_actual_until(item)
        if complete is None or required>complete:
            not_yet+=1; next_required=next_required or required; continue
        try:
            teacher=v1._perfect_information_teacher(cfg,item)
            if teacher is None: coverage+=1; continue
            teacher_action,_=teacher; label=v1._round_action(teacher_action); features=vectorize(item)
            with sqlite3.connect(DB_PATH) as c:
                c.execute('''INSERT OR REPLACE INTO neural_training_sample(information_vintage_id,decision_start,generated_at,label_source,teacher_action_kw,label_action_kw,feature_schema,features_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)''',
                          (item.information_vintage_id,item.decision_start,item.generated_at,v1.LABEL_SOURCE,teacher_action,label,FEATURE_SCHEMA,json.dumps(features,separators=(",",":")),now))
            existing.add(item.information_vintage_id); created+=1
        except Exception:
            failures+=1
    return {"ok":True,"label_source":v1.LABEL_SOURCE,"created_samples":created,"not_yet_mature_candidates":not_yet,
            "mature_but_incomplete_coverage_candidates":coverage,"failures":failures,"total_samples":sample_count(),
            "feature_schema":FEATURE_SCHEMA,"feature_count":len(FEATURE_NAMES),"action_classes_kw":list(v1.ACTION_CLASSES_KW),
            "candidate_diagnostics":diag,"maturity":{"latest_available_actual_start":None if latest is None else latest.isoformat(),
            "latest_available_actual_complete_until":None if complete is None else complete.isoformat(),
            "next_pending_required_actual_until":None if next_required is None else next_required.isoformat()}}


def training_maturity_status(cfg: dict[str, Any], candidate_limit: int = 2000) -> dict[str, Any]:
    candidates,diag=_candidate_inputs(cfg,candidate_limit); latest=v1._latest_actual_start(); complete=None if latest is None else latest+v1.timedelta(minutes=15)
    with sqlite3.connect(DB_PATH) as c:
        existing={r[0] for r in c.execute("SELECT information_vintage_id FROM neural_training_sample WHERE feature_schema=?",(FEATURE_SCHEMA,)).fetchall()}
        legacy_count=int(c.execute("SELECT COUNT(*) FROM neural_training_sample WHERE feature_schema<>?",(FEATURE_SCHEMA,)).fetchone()[0])
    pending=[x for x in candidates if x.information_vintage_id not in existing]; mature=[]; immature=[]
    for item in pending:
        (mature if complete is not None and v1._required_actual_until(item)<=complete else immature).append(item)
    return {"engine_id":v1.ENGINE_ID,"feature_schema":FEATURE_SCHEMA,"feature_count":len(FEATURE_NAMES),"label_source":v1.LABEL_SOURCE,
            "candidate_diagnostics":diag,"training_samples_existing":len(existing),"legacy_schema_samples_ignored":legacy_count,
            "pending_canonical_candidates":len(pending),"chronologically_mature_pending_candidates":len(mature),"not_yet_mature_pending_candidates":len(immature),
            "first_canonical_decision_start":candidates[0].decision_start if candidates else None,"last_canonical_decision_start":candidates[-1].decision_start if candidates else None,
            "earliest_required_actual_until":v1._required_actual_until(candidates[0]).isoformat() if candidates else None,
            "latest_available_actual_start":None if latest is None else latest.isoformat(),"latest_available_actual_complete_until":None if complete is None else complete.isoformat(),
            "next_pending_required_actual_until":v1._required_actual_until(immature[0]).isoformat() if immature else None,
            "note":"Only current feature-schema samples count toward neural v2 training; older schemas remain stored for audit."}


def install_into_v1_module() -> None:
    v1.sample_count = sample_count
    v1._load_samples = _load_samples
    v1._candidate_inputs = _candidate_inputs
    v1.build_training_samples = build_training_samples
    v1.training_maturity_status = training_maturity_status
