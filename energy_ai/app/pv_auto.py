from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .db import DB_PATH
from .pv_calibration import MODEL_PATH, REPORT_PATH, load_model, train_pv_model
from .training import TRAINING_DIR, build_dataset, ensure_training_dir

STOCKHOLM = ZoneInfo("Europe/Stockholm")
STATE_PATH = TRAINING_DIR / "pv_auto_retraining_state.json"
RUNTIME_ACTUAL_PATH = TRAINING_DIR / "runtime_pv_actuals.csv"
RUNTIME_ENV_PATH = TRAINING_DIR / "runtime_pv_forecast_environment.csv"
MIN_NEW_DAYLIGHT_ROWS = 32
MIN_BASELINE_IMPROVEMENT = 0.02
MAX_PRIOR_REPORT_MAE_RATIO = 1.10
LOOKBACK_DAYS = 180


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(value: dict[str, Any]) -> None:
    ensure_training_dir()
    STATE_PATH.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _payload_mean(payload_json: str, key: str) -> float | None:
    try:
        payload = json.loads(payload_json)
        value = (payload.get("mean") or {}).get(key)
        return None if value is None else float(value)
    except Exception:
        return None


def _runtime_rows() -> list[dict[str, Any]]:
    """Pair actual 15-minute PV with the latest forecast vintage available before that interval."""
    cutoff = (datetime.now(timezone.utc).timestamp() - LOOKBACK_DAYS * 86400.0)
    cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as c:
        states = c.execute(
            "SELECT bucket_start,payload_json FROM state_15m WHERE bucket_start>=? ORDER BY bucket_start ASC",
            (cutoff_iso,),
        ).fetchall()
        rows: list[dict[str, Any]] = []
        for start_utc, payload_json in states:
            actual = _payload_mean(payload_json, "pv_power_kw")
            if actual is None:
                continue
            forecast = c.execute(
                '''
                SELECT generated_at,irradiance_w_m2,cloud_cover_pct,temperature_c,model
                FROM pv_forecast_15m
                WHERE start_utc=? AND generated_at<=?
                ORDER BY generated_at DESC LIMIT 1
                ''',
                (start_utc, start_utc),
            ).fetchone()
            if forecast is None:
                continue
            try:
                stamp = datetime.fromisoformat(str(start_utc).replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            rows.append({
                "timestamp": stamp.astimezone(timezone.utc),
                "pv_power_kw": max(0.0, float(actual)),
                "gti_w_m2": max(0.0, float(forecast[1] or 0.0)),
                "cloud_cover_pct": None if forecast[2] is None else float(forecast[2]),
                "temperature_c": None if forecast[3] is None else float(forecast[3]),
                "forecast_generated_at": forecast[0],
                "forecast_model": forecast[4],
            })
    return rows


def materialize_runtime_training_files() -> dict[str, Any]:
    ensure_training_dir()
    rows = _runtime_rows()
    with RUNTIME_ACTUAL_PATH.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "time", "utc_offset", "pv_power_kw", "meter_power_kw", "load_power_kw"])
        writer.writeheader()
        for row in rows:
            local = row["timestamp"].astimezone(STOCKHOLM)
            offset = local.strftime("%z")
            offset = f"UTC{offset[:3]}:{offset[3:]}" if offset else "UTC+00:00"
            writer.writerow({"date": local.date().isoformat(), "time": local.time().replace(tzinfo=None).isoformat(), "utc_offset": offset,
                             "pv_power_kw": row["pv_power_kw"], "meter_power_kw": "", "load_power_kw": ""})
    with RUNTIME_ENV_PATH.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["time", "global_tilted_irradiance", "cloud_cover", "temperature_2m"])
        writer.writeheader()
        for row in rows:
            writer.writerow({"time": row["timestamp"].isoformat(), "global_tilted_irradiance": row["gti_w_m2"],
                             "cloud_cover": row["cloud_cover_pct"], "temperature_2m": row["temperature_c"]})
    daylight = [r for r in rows if r["gti_w_m2"] >= 5.0 and r["pv_power_kw"] >= 0.0]
    dates = sorted({r["timestamp"].astimezone(STOCKHOLM).date().isoformat() for r in daylight})
    return {
        "rows": len(rows), "daylight_rows": len(daylight), "dates": dates,
        "latest_timestamp": rows[-1]["timestamp"].isoformat() if rows else None,
        "actual_path": str(RUNTIME_ACTUAL_PATH), "environment_path": str(RUNTIME_ENV_PATH),
        "feature_vintage": "latest_forecast_generated_at_at_or_before_interval_start",
    }


def _report_mae(report: dict[str, Any] | None) -> float | None:
    try:
        return float(report["metrics"]["test_hybrid"]["mae_kw"])
    except Exception:
        return None


def _baseline_mae(report: dict[str, Any] | None) -> float | None:
    try:
        return float(report["metrics"]["test_physical_baseline"]["mae_kw"])
    except Exception:
        return None


def automatic_pv_retraining_once(*, force: bool = False) -> dict[str, Any]:
    """Retrain at most once per newly observed local day and promote only a validated candidate."""
    existing = load_model()
    if not existing:
        return {"ok": True, "status": "not_ready", "reason": "no_existing_calibrated_model"}
    lat = existing.get("latitude_deg"); lon = existing.get("longitude_deg"); capacity = existing.get("capacity_kw")
    if lat is None or lon is None or capacity is None:
        return {"ok": True, "status": "not_ready", "reason": "existing_model_missing_site_metadata"}

    runtime = materialize_runtime_training_files()
    daylight_rows = int(runtime["daylight_rows"])
    latest_timestamp = runtime.get("latest_timestamp")
    state = _state()
    previous_source = state.get("last_source_timestamp")
    if not force:
        if daylight_rows < MIN_NEW_DAYLIGHT_ROWS:
            return {"ok": True, "status": "waiting_for_data", "runtime": runtime, "minimum_new_daylight_rows": MIN_NEW_DAYLIGHT_ROWS}
        if latest_timestamp and previous_source and latest_timestamp <= previous_source:
            return {"ok": True, "status": "not_due", "reason": "no_new_runtime_training_rows", "runtime": runtime, "state": state}
        if state.get("last_attempt_local_date") and runtime.get("dates") and state["last_attempt_local_date"] == runtime["dates"][-1]:
            return {"ok": True, "status": "not_due", "reason": "already_attempted_latest_local_day", "runtime": runtime, "state": state}

    dataset = build_dataset()
    old_model = MODEL_PATH.read_bytes() if MODEL_PATH.exists() else None
    old_report_bytes = REPORT_PATH.read_bytes() if REPORT_PATH.exists() else None
    old_report = existing.get("report") if isinstance(existing, dict) else None
    old_mae = _report_mae(old_report)
    try:
        candidate_report = train_pv_model(float(capacity), float(lat), float(lon))
        candidate_mae = _report_mae(candidate_report)
        physical_mae = _baseline_mae(candidate_report)
        baseline_improvement = None if candidate_mae is None or physical_mae in (None, 0.0) else (physical_mae - candidate_mae) / physical_mae
        reasons: list[str] = []
        if candidate_mae is None or physical_mae is None:
            reasons.append("missing_candidate_metrics")
        elif baseline_improvement is None or baseline_improvement < MIN_BASELINE_IMPROVEMENT:
            reasons.append("insufficient_improvement_over_physical_baseline")
        if old_mae is not None and candidate_mae is not None and candidate_mae > old_mae * MAX_PRIOR_REPORT_MAE_RATIO:
            reasons.append("candidate_mae_materially_worse_than_prior_report")
        promoted = not reasons
        if not promoted:
            if old_model is not None:
                MODEL_PATH.write_bytes(old_model)
            if old_report_bytes is not None:
                REPORT_PATH.write_bytes(old_report_bytes)
        state = {
            "updated_at": _now(),
            "last_attempt_local_date": runtime["dates"][-1] if runtime.get("dates") else None,
            "last_source_timestamp": latest_timestamp,
            "last_status": "promoted" if promoted else "rejected",
            "promoted": promoted,
            "candidate_mae_kw": candidate_mae,
            "physical_baseline_mae_kw": physical_mae,
            "prior_report_mae_kw": old_mae,
            "baseline_relative_improvement": baseline_improvement,
            "rejection_reasons": reasons,
            "runtime_rows": runtime,
            "dataset": dataset,
            "candidate_report": candidate_report,
        }
        _save_state(state)
        return {"ok": True, "status": "promoted" if promoted else "rejected", "state": state}
    except Exception as exc:
        if old_model is not None:
            MODEL_PATH.write_bytes(old_model)
        if old_report_bytes is not None:
            REPORT_PATH.write_bytes(old_report_bytes)
        state.update({"updated_at": _now(), "last_status": "failed", "error": repr(exc)})
        _save_state(state)
        return {"ok": False, "status": "failed", "error": repr(exc), "state": state}


def pv_auto_status() -> dict[str, Any]:
    state = _state()
    try:
        runtime = materialize_runtime_training_files()
    except Exception as exc:
        runtime = {"error": repr(exc)}
    return {
        "enabled": True,
        "mode": "automatic_daily_challenger_retrain_with_validation_gate",
        "minimum_new_daylight_rows": MIN_NEW_DAYLIGHT_ROWS,
        "minimum_relative_improvement_over_physical_baseline": MIN_BASELINE_IMPROVEMENT,
        "maximum_candidate_to_prior_report_mae_ratio": MAX_PRIOR_REPORT_MAE_RATIO,
        "runtime_training": runtime,
        "state": state,
    }
