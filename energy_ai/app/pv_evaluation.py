from __future__ import annotations

import csv
import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

from .db import DB_PATH
from .training import TRAINING_DIR

STOCKHOLM = ZoneInfo("Europe/Stockholm")
ONLINE_MATCHES_PATH = TRAINING_DIR / "pv_online_forecast_matches.csv"
HORIZONS = {
    "15m": (15.0, 20.0),
    "1h": (60.0, 35.0),
    "3h": (180.0, 50.0),
}


def _dt(value: str) -> datetime:
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _actual_pv(payload_json: str) -> float | None:
    try:
        payload = json.loads(payload_json)
        value = (payload.get("mean") or {}).get("pv_power_kw")
        return None if value is None else float(value)
    except Exception:
        return None


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    pos = (len(values) - 1) * q
    lo = int(math.floor(pos)); hi = int(math.ceil(pos))
    if lo == hi:
        return values[lo]
    f = pos - lo
    return values[lo] * (1.0 - f) + values[hi] * f


def _choose_forecast(candidates: list[dict[str, Any]], start: datetime, target_lead: float, tolerance: float) -> dict[str, Any] | None:
    best = None; best_delta = None
    for row in candidates:
        generated = _dt(row["generated_at"])
        lead = (start - generated).total_seconds() / 60.0
        if lead < 0:
            continue
        delta = abs(lead - target_lead)
        if delta <= tolerance and (best_delta is None or delta < best_delta):
            best = dict(row); best["lead_minutes"] = lead; best_delta = delta
    return best


def evaluate_matured_forecasts(lookback_days: int = 7) -> dict[str, Any]:
    """Evaluate forecasts only after actual 15-minute buckets exist.

    Each horizon uses a forecast snapshot that genuinely existed before the target
    interval, avoiding hindsight leakage from later weather/forecast revisions.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, lookback_days))).isoformat()
    created_at = datetime.now(timezone.utc).isoformat()
    inserted = 0
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        actual_rows = c.execute(
            "SELECT bucket_start,payload_json FROM state_15m WHERE bucket_start>=? ORDER BY bucket_start ASC",
            (cutoff,),
        ).fetchall()
        for actual_row in actual_rows:
            actual_kw = _actual_pv(actual_row["payload_json"])
            if actual_kw is None:
                continue
            start = _dt(actual_row["bucket_start"])
            if start + timedelta(minutes=15) > datetime.now(timezone.utc):
                continue
            candidates = c.execute(
                "SELECT generated_at,start_utc,forecast_kw,uncertainty_kw,irradiance_w_m2,cloud_cover_pct,temperature_c,model,payload_json FROM pv_forecast_15m WHERE start_utc=? AND generated_at<=? AND generated_at>=?",
                (actual_row["bucket_start"], start.isoformat(), (start - timedelta(hours=4)).isoformat()),
            ).fetchall()
            candidate_dicts = [dict(r) for r in candidates]
            for label, (target_lead, tolerance) in HORIZONS.items():
                exists = c.execute("SELECT 1 FROM pv_forecast_eval WHERE start_utc=? AND horizon_label=?", (actual_row["bucket_start"], label)).fetchone()
                if exists:
                    continue
                chosen = _choose_forecast(candidate_dicts, start, target_lead, tolerance)
                if not chosen:
                    continue
                error = actual_kw - float(chosen["forecast_kw"])
                payload = {}
                try:
                    payload = json.loads(chosen.get("payload_json") or "{}")
                except Exception:
                    pass
                payload.update({
                    "irradiance_w_m2": chosen.get("irradiance_w_m2"),
                    "cloud_cover_pct": chosen.get("cloud_cover_pct"),
                    "temperature_c": chosen.get("temperature_c"),
                    "uncertainty_kw": chosen.get("uncertainty_kw"),
                })
                c.execute(
                    "INSERT OR IGNORE INTO pv_forecast_eval(start_utc,horizon_label,generated_at,model,forecast_kw,actual_kw,error_kw,abs_error_kw,lead_minutes,payload_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (actual_row["bucket_start"], label, chosen["generated_at"], chosen["model"], float(chosen["forecast_kw"]), actual_kw, error, abs(error), float(chosen["lead_minutes"]), json.dumps(payload, ensure_ascii=False), created_at),
                )
                inserted += c.total_changes > 0
        _evaluate_completed_days(c, created_at)
    _export_online_matches()
    return {"ok": True, "point_evaluations_inserted": int(inserted), "report": evaluation_report()}


def _evaluate_completed_days(c: sqlite3.Connection, created_at: str) -> None:
    today = datetime.now(timezone.utc).astimezone(STOCKHOLM).date()
    rows = c.execute(
        "SELECT generated_at,local_date,remaining_energy_kwh,p80_kwh,p95_kwh,model FROM pv_remaining_day_forecast WHERE local_date<? ORDER BY generated_at ASC",
        (today.isoformat(),),
    ).fetchall()
    for r in rows:
        if c.execute("SELECT 1 FROM pv_day_eval WHERE generated_at=?", (r["generated_at"],)).fetchone():
            continue
        generated = _dt(r["generated_at"])
        local_date = generated.astimezone(STOCKHOLM).date()
        day_start = datetime.combine(local_date, datetime.min.time(), tzinfo=STOCKHOLM).astimezone(timezone.utc)
        day_end = day_start + timedelta(days=1)
        actual_rows = c.execute(
            "SELECT bucket_start,payload_json FROM state_15m WHERE bucket_start>=? AND bucket_start<? ORDER BY bucket_start ASC",
            (generated.isoformat(), day_end.isoformat()),
        ).fetchall()
        values = [v for row in actual_rows if (v := _actual_pv(row["payload_json"])) is not None]
        if len(values) < 4:
            continue
        actual_remaining = sum(max(0.0, v) * 0.25 for v in values)
        forecast_remaining = float(r["remaining_energy_kwh"])
        error = actual_remaining - forecast_remaining
        c.execute(
            "INSERT OR IGNORE INTO pv_day_eval(generated_at,local_date,model,forecast_remaining_kwh,actual_remaining_kwh,error_kwh,abs_error_kwh,p80_kwh,p95_kwh,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (r["generated_at"], local_date.isoformat(), r["model"], forecast_remaining, actual_remaining, error, abs(error), r["p80_kwh"], r["p95_kwh"], created_at),
        )


def _metric_block(rows: list[sqlite3.Row], error_key: str, actual_key: str) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    errors = [float(r[error_key]) for r in rows]
    actuals = [float(r[actual_key]) for r in rows]
    mae = mean(abs(e) for e in errors)
    rmse = math.sqrt(mean(e * e for e in errors))
    bias = mean(errors)
    return {
        "n": len(rows),
        "mae": round(mae, 4),
        "rmse": round(rmse, 4),
        "bias": round(bias, 4),
        "p80_abs_error": round(float(_quantile([abs(e) for e in errors], 0.80) or 0.0), 4),
        "p95_abs_error": round(float(_quantile([abs(e) for e in errors], 0.95) or 0.0), 4),
        "mean_actual": round(mean(actuals), 4),
    }


def evaluation_report(days: int = 30) -> dict[str, Any]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        point = {}
        for label in HORIZONS:
            rows = c.execute("SELECT * FROM pv_forecast_eval WHERE horizon_label=? AND start_utc>=? ORDER BY start_utc", (label, cutoff)).fetchall()
            point[label] = _metric_block(rows, "error_kw", "actual_kw")
        day_rows = c.execute("SELECT * FROM pv_day_eval WHERE generated_at>=? ORDER BY generated_at", (cutoff,)).fetchall()
        daily = _metric_block(day_rows, "error_kwh", "actual_remaining_kwh")
        if day_rows:
            p80_covered = [r for r in day_rows if r["p80_kwh"] is not None]
            p95_covered = [r for r in day_rows if r["p95_kwh"] is not None]
            daily["p80_coverage"] = round(sum(abs(float(r["error_kwh"])) <= float(r["p80_kwh"]) for r in p80_covered) / len(p80_covered), 3) if p80_covered else None
            daily["p95_coverage"] = round(sum(abs(float(r["error_kwh"])) <= float(r["p95_kwh"]) for r in p95_covered) / len(p95_covered), 3) if p95_covered else None
        models = [r[0] for r in c.execute("SELECT DISTINCT model FROM pv_forecast_eval WHERE start_utc>=? ORDER BY model", (cutoff,)).fetchall()]
    return {
        "window_days": days,
        "models_seen": models,
        "power_forecast": point,
        "remaining_day_energy": daily,
        "online_matches_csv": str(ONLINE_MATCHES_PATH),
        "promotion_policy": "collect_and_evaluate_only; never auto-promote without holdout improvement",
    }


def _export_online_matches() -> None:
    TRAINING_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("SELECT * FROM pv_forecast_eval WHERE horizon_label='15m' ORDER BY start_utc ASC").fetchall()
    fields = [
        "start_utc","generated_at","model","lead_minutes","forecast_kw","actual_kw","error_kw",
        "irradiance_w_m2","cloud_cover_pct","temperature_c","solar_elevation_deg",
        "daily_max_solar_elevation_deg","daylight_hours","solar_regime","ml_weight",
        "physical_baseline_kw","ml_residual_correction_kw",
    ]
    with ONLINE_MATCHES_PATH.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
        for row in rows:
            try: payload = json.loads(row["payload_json"] or "{}")
            except Exception: payload = {}
            out = {k: row[k] if k in row.keys() else payload.get(k) for k in fields}
            for k in fields:
                if k not in row.keys(): out[k] = payload.get(k)
            w.writerow(out)
