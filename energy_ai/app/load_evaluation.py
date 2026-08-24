from __future__ import annotations

import csv
import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .db import DB_PATH

EXPORT_PATH = Path("/data/training/load_online_forecast_matches.csv")
HORIZONS = {
    "15m": (15, 20),
    "1h": (60, 35),
    "3h": (180, 50),
}


def _parse(value: str) -> datetime:
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _init_tables() -> None:
    with sqlite3.connect(DB_PATH) as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS load_forecast_15m(
            generated_at TEXT NOT NULL,
            start_utc TEXT NOT NULL,
            base_forecast_kw REAL NOT NULL,
            ev_forecast_kw REAL NOT NULL,
            sauna_forecast_kw REAL NOT NULL,
            total_forecast_kw REAL NOT NULL,
            uncertainty_kw REAL NOT NULL,
            model TEXT NOT NULL,
            payload_json TEXT,
            PRIMARY KEY(generated_at,start_utc)
        );
        CREATE TABLE IF NOT EXISTS load_forecast_eval(
            start_utc TEXT NOT NULL,
            horizon_label TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            model TEXT NOT NULL,
            base_forecast_kw REAL NOT NULL,
            ev_forecast_kw REAL NOT NULL,
            sauna_forecast_kw REAL NOT NULL,
            total_forecast_kw REAL NOT NULL,
            actual_kw REAL NOT NULL,
            error_kw REAL NOT NULL,
            abs_error_kw REAL NOT NULL,
            lead_minutes REAL NOT NULL,
            payload_json TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY(start_utc,horizon_label)
        );
        ''')


def insert_load_forecast(forecast: dict[str, Any]) -> int:
    _init_tables()
    generated = str(forecast["generated_at"])
    model = str(forecast.get("model") or "unknown")
    rows = forecast.get("rows") or []
    with sqlite3.connect(DB_PATH) as c:
        c.executemany(
            '''INSERT OR REPLACE INTO load_forecast_15m(
               generated_at,start_utc,base_forecast_kw,ev_forecast_kw,sauna_forecast_kw,
               total_forecast_kw,uncertainty_kw,model,payload_json)
               VALUES (?,?,?,?,?,?,?,?,?)''',
            [(
                generated,
                r["start"],
                float(r.get("base_household_forecast_kw") or 0.0),
                float(r.get("ev_forecast_kw") or 0.0),
                float(r.get("sauna_forecast_kw") or 0.0),
                float(r.get("house_load_forecast_kw") or 0.0),
                float(r.get("house_load_uncertainty_kw") or 0.0),
                model,
                json.dumps(r, ensure_ascii=False),
            ) for r in rows],
        )
        cutoff = (datetime.now(timezone.utc) - timedelta(days=180)).isoformat()
        c.execute("DELETE FROM load_forecast_15m WHERE generated_at < ?", (cutoff,))
    return len(rows)


def latest_load_forecast(limit: int = 144) -> dict[str, Any]:
    _init_tables()
    limit = max(1, min(int(limit), 500))
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute("SELECT MAX(generated_at) FROM load_forecast_15m").fetchone()
        generated = row[0] if row else None
        if not generated:
            return {"generated_at": None, "rows": []}
        rows = c.execute(
            "SELECT payload_json FROM load_forecast_15m WHERE generated_at=? ORDER BY start_utc ASC LIMIT ?",
            (generated, limit),
        ).fetchall()
    out = []
    for (payload,) in rows:
        try: out.append(json.loads(payload))
        except Exception: pass
    return {"generated_at": generated, "rows": out}


def _actual_load(start_utc: str) -> float | None:
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute("SELECT payload_json FROM state_15m WHERE bucket_start=?", (start_utc,)).fetchone()
    if not row:
        return None
    try:
        p = json.loads(row[0]); v = (p.get("mean") or {}).get("house_load_kw")
        return None if v is None else float(v)
    except Exception:
        return None


def _candidate_for_target(target: datetime, desired_lead: int, tolerance: int):
    target_iso = target.isoformat()
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            "SELECT generated_at,start_utc,base_forecast_kw,ev_forecast_kw,sauna_forecast_kw,total_forecast_kw,uncertainty_kw,model,payload_json FROM load_forecast_15m WHERE start_utc=? AND generated_at<? ORDER BY generated_at ASC",
            (target_iso, target_iso),
        ).fetchall()
    best = None
    best_delta = None
    for row in rows:
        generated = _parse(row[0])
        lead = (target - generated).total_seconds() / 60.0
        delta = abs(lead - desired_lead)
        if delta <= tolerance and (best_delta is None or delta < best_delta):
            best = (*row, lead)
            best_delta = delta
    return best


def evaluate_matured_load_forecasts(lookback_days: int = 7) -> dict[str, Any]:
    _init_tables()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max(1, int(lookback_days)))
    latest_mature = now - timedelta(minutes=15)
    inserted = 0

    with sqlite3.connect(DB_PATH) as c:
        targets = [r[0] for r in c.execute(
            "SELECT bucket_start FROM state_15m WHERE bucket_start>=? AND bucket_start<=? ORDER BY bucket_start ASC",
            (cutoff.isoformat(), latest_mature.isoformat()),
        ).fetchall()]

    for target_iso in targets:
        target = _parse(target_iso)
        actual = _actual_load(target_iso)
        if actual is None:
            continue
        for label, (lead_target, tolerance) in HORIZONS.items():
            with sqlite3.connect(DB_PATH) as c:
                exists = c.execute("SELECT 1 FROM load_forecast_eval WHERE start_utc=? AND horizon_label=?", (target_iso, label)).fetchone()
            if exists:
                continue
            cand = _candidate_for_target(target, lead_target, tolerance)
            if not cand:
                continue
            generated, _, base, ev, sauna, total, uncertainty, model, payload_json, lead = cand
            error = actual - float(total)
            payload = {}
            try: payload = json.loads(payload_json) if payload_json else {}
            except Exception: pass
            payload.update({"uncertainty_kw": uncertainty})
            with sqlite3.connect(DB_PATH) as c:
                c.execute(
                    '''INSERT OR REPLACE INTO load_forecast_eval(
                       start_utc,horizon_label,generated_at,model,base_forecast_kw,ev_forecast_kw,
                       sauna_forecast_kw,total_forecast_kw,actual_kw,error_kw,abs_error_kw,lead_minutes,
                       payload_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (target_iso,label,generated,model,float(base),float(ev),float(sauna),float(total),float(actual),float(error),abs(float(error)),float(lead),json.dumps(payload,ensure_ascii=False),now.isoformat()),
                )
            inserted += 1

    _export_matches()
    return {"ok": True, "inserted": inserted, "lookback_days": lookback_days}


def _metric_rows(rows: list[tuple]) -> dict[str, float | int | None]:
    if not rows:
        return {"n": 0, "mae_kw": None, "rmse_kw": None, "bias_kw": None, "p80_abs_error_kw": None, "p95_abs_error_kw": None}
    errors = [float(r[0]) for r in rows]
    abs_errors = sorted(abs(e) for e in errors)
    def q(p: float) -> float:
        pos = (len(abs_errors)-1)*p; lo=int(math.floor(pos)); hi=int(math.ceil(pos))
        if lo == hi: return abs_errors[lo]
        f=pos-lo; return abs_errors[lo]*(1-f)+abs_errors[hi]*f
    return {
        "n": len(errors),
        "mae_kw": round(sum(abs(e) for e in errors)/len(errors),4),
        "rmse_kw": round(math.sqrt(sum(e*e for e in errors)/len(errors)),4),
        "bias_kw": round(sum(errors)/len(errors),4),
        "p80_abs_error_kw": round(q(.80),4),
        "p95_abs_error_kw": round(q(.95),4),
    }


def evaluation_report(days: int = 30) -> dict[str, Any]:
    _init_tables()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).isoformat()
    horizons = {}
    with sqlite3.connect(DB_PATH) as c:
        for label in HORIZONS:
            rows = c.execute("SELECT error_kw FROM load_forecast_eval WHERE horizon_label=? AND start_utc>=? ORDER BY start_utc", (label, cutoff)).fetchall()
            horizons[label] = _metric_rows(rows)
        coverage = c.execute("SELECT COUNT(*),MIN(start_utc),MAX(start_utc) FROM load_forecast_eval WHERE start_utc>=?", (cutoff,)).fetchone()
    return {
        "days": days,
        "horizons": horizons,
        "coverage": {"matches": int(coverage[0] or 0), "first": coverage[1], "last": coverage[2]},
        "export_path": str(EXPORT_PATH),
        "component_model": "base_household + ev + sauna",
        "promotion_policy": "collect_and_evaluate_only; never auto-promote without holdout improvement",
    }


def _export_matches() -> None:
    EXPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            "SELECT start_utc,horizon_label,generated_at,model,lead_minutes,base_forecast_kw,ev_forecast_kw,sauna_forecast_kw,total_forecast_kw,actual_kw,error_kw,abs_error_kw,payload_json FROM load_forecast_eval ORDER BY start_utc,horizon_label"
        ).fetchall()
    cols = ["start_utc","horizon_label","generated_at","model","lead_minutes","base_forecast_kw","ev_forecast_kw","sauna_forecast_kw","total_forecast_kw","actual_kw","error_kw","abs_error_kw","payload_json"]
    with EXPORT_PATH.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f); w.writerow(cols); w.writerows(rows)
