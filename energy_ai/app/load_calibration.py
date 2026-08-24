from __future__ import annotations

import csv
import json
import math
import pickle
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import mean, median
from typing import Any
from zoneinfo import ZoneInfo

from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

from .training import DATASET_PATH, TRAINING_DIR

STOCKHOLM = ZoneInfo("Europe/Stockholm")
MODEL_PATH = TRAINING_DIR / "load_forecast_v1.pkl"
REPORT_PATH = TRAINING_DIR / "load_forecast_report.json"
MODEL_NAME = "load_profile_residual_gradient_boosting_v1"

FEATURE_NAMES = [
    "profile_baseline_kw",
    "hour_sin", "hour_cos",
    "dow_sin", "dow_cos",
    "doy_sin", "doy_cos",
    "is_weekend",
]


def _number(value: Any) -> float | None:
    if value in (None, "", "null", "None", "nan"):
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _parse_ts(value: str) -> datetime:
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _slot(ts: datetime) -> int:
    local = ts.astimezone(STOCKHOLM)
    return local.hour * 4 + local.minute // 15


def _build_profile(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_weekday_slot: dict[tuple[int, int], list[float]] = defaultdict(list)
    by_slot: dict[int, list[float]] = defaultdict(list)
    all_values: list[float] = []
    for r in rows:
        local = r["ts"].astimezone(STOCKHOLM)
        s = _slot(r["ts"])
        y = float(r["y"])
        by_weekday_slot[(local.weekday(), s)].append(y)
        by_slot[s].append(y)
        all_values.append(y)
    return {
        "weekday_slot": {f"{dow}:{slot}": median(vals) for (dow, slot), vals in by_weekday_slot.items()},
        "slot": {str(slot): median(vals) for slot, vals in by_slot.items()},
        "global": median(all_values) if all_values else 0.5,
    }


def profile_baseline(profile: dict[str, Any], ts: datetime) -> float:
    local = ts.astimezone(STOCKHOLM)
    s = _slot(ts)
    key = f"{local.weekday()}:{s}"
    if key in (profile.get("weekday_slot") or {}):
        return max(0.0, float(profile["weekday_slot"][key]))
    if str(s) in (profile.get("slot") or {}):
        return max(0.0, float(profile["slot"][str(s)]))
    return max(0.0, float(profile.get("global", 0.5)))


def feature_vector(profile: dict[str, Any], ts: datetime) -> list[float]:
    local = ts.astimezone(STOCKHOLM)
    hour = local.hour + local.minute / 60.0
    dow = local.weekday()
    doy = local.timetuple().tm_yday
    return [
        profile_baseline(profile, ts),
        math.sin(2 * math.pi * hour / 24.0),
        math.cos(2 * math.pi * hour / 24.0),
        math.sin(2 * math.pi * dow / 7.0),
        math.cos(2 * math.pi * dow / 7.0),
        math.sin(2 * math.pi * doy / 365.25),
        math.cos(2 * math.pi * doy / 365.25),
        1.0 if dow >= 5 else 0.0,
    ]


def _load_rows() -> list[dict[str, Any]]:
    if not DATASET_PATH.exists():
        raise RuntimeError(f"Training dataset not found: {DATASET_PATH}")
    out = []
    with DATASET_PATH.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            load = _number(row.get("load_power_kw"))
            if load is None or load < 0:
                continue
            try:
                ts = _parse_ts(row["timestamp_utc"])
            except Exception:
                continue
            out.append({"ts": ts, "y": float(load)})
    out.sort(key=lambda r: r["ts"])
    if len(out) < 1000:
        raise RuntimeError(f"Too few load rows for training: {len(out)}")
    return out


def _split(rows: list[dict[str, Any]]):
    last = rows[-1]["ts"]
    test_start = last - timedelta(days=30)
    val_start = test_start - timedelta(days=30)
    train = [r for r in rows if r["ts"] < val_start]
    val = [r for r in rows if val_start <= r["ts"] < test_start]
    test = [r for r in rows if r["ts"] >= test_start]
    if min(len(train), len(val), len(test)) < 200:
        n = len(rows); a = int(n * .70); b = int(n * .85)
        train, val, test = rows[:a], rows[a:b], rows[b:]
    periods = {
        "train_start": train[0]["ts"].isoformat(), "train_end": train[-1]["ts"].isoformat(),
        "validation_start": val[0]["ts"].isoformat(), "validation_end": val[-1]["ts"].isoformat(),
        "test_start": test[0]["ts"].isoformat(), "test_end": test[-1]["ts"].isoformat(),
    }
    return train, val, test, periods


def _metrics(y_true: list[float], y_pred: list[float]) -> dict[str, float]:
    mae = float(mean_absolute_error(y_true, y_pred))
    rmse = float(mean_squared_error(y_true, y_pred) ** 0.5)
    avg = float(mean(y_true)) if y_true else 0.0
    return {
        "mae_kw": round(mae, 4),
        "rmse_kw": round(rmse, 4),
        "r2": round(float(r2_score(y_true, y_pred)), 4),
        "mean_actual_kw": round(avg, 4),
        "mae_pct_of_mean": round(100.0 * mae / avg, 2) if avg > 0 else 0.0,
    }


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    pos = (len(values) - 1) * q
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    if lo == hi:
        return values[lo]
    f = pos - lo
    return values[lo] * (1 - f) + values[hi] * f


def _validation_weight(y_true: list[float], baseline: list[float], corrected: list[float]) -> tuple[float, dict[str, float]]:
    b = float(mean_absolute_error(y_true, baseline))
    c = float(mean_absolute_error(y_true, corrected))
    improvement = (b - c) / b if b > 1e-9 else 0.0
    # No correction if ML hurts; full correction after 10% validation improvement.
    weight = max(0.0, min(1.0, improvement / 0.10))
    return weight, {
        "baseline_mae_kw": round(b, 4),
        "corrected_mae_kw": round(c, 4),
        "relative_improvement": round(improvement, 4),
        "correction_weight": round(weight, 3),
    }


def train_load_model() -> dict[str, Any]:
    TRAINING_DIR.mkdir(parents=True, exist_ok=True)
    rows = _load_rows()
    train, val, test, periods = _split(rows)
    profile = _build_profile(train)

    x_train = [feature_vector(profile, r["ts"]) for r in train]
    baseline_train = [profile_baseline(profile, r["ts"]) for r in train]
    residual_train = [r["y"] - b for r, b in zip(train, baseline_train)]

    model = HistGradientBoostingRegressor(
        loss="squared_error", learning_rate=0.05, max_iter=250,
        max_leaf_nodes=31, min_samples_leaf=30, l2_regularization=0.3,
        random_state=42,
    )
    model.fit(x_train, residual_train)

    def corrected(part: list[dict[str, Any]], weight: float = 1.0):
        base = [profile_baseline(profile, r["ts"]) for r in part]
        corr = model.predict([feature_vector(profile, r["ts"]) for r in part])
        pred = [max(0.0, b + weight * float(c)) for b, c in zip(base, corr)]
        return base, pred

    val_base, val_full = corrected(val, 1.0)
    val_true = [r["y"] for r in val]
    weight, validation_gate = _validation_weight(val_true, val_base, val_full)
    _, val_pred = corrected(val, weight)
    test_base, test_pred = corrected(test, weight)
    test_true = [r["y"] for r in test]

    residuals = [abs(y - p) for y, p in zip(val_true + test_true, val_pred + test_pred)]
    uncertainty = {
        "p50_kw": round(_quantile(residuals, .50), 4),
        "p80_kw": round(_quantile(residuals, .80), 4),
        "p95_kw": round(_quantile(residuals, .95), 4),
    }
    report = {
        "ok": True,
        "model": MODEL_NAME,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "target": "house_load_kw",
        "baseline": "median by weekday + 15-minute slot; fallback slot median",
        "features": FEATURE_NAMES,
        "rows": {"all": len(rows), "train": len(train), "validation": len(val), "test": len(test)},
        "periods": periods,
        "validation_gate": validation_gate,
        "metrics": {
            "validation_hybrid": _metrics(val_true, val_pred),
            "validation_profile_baseline": _metrics(val_true, val_base),
            "test_hybrid": _metrics(test_true, test_pred),
            "test_profile_baseline": _metrics(test_true, test_base),
        },
        "uncertainty": uncertainty,
    }
    payload = {
        "model": model, "profile": profile, "report": report,
        "feature_names": FEATURE_NAMES, "correction_weight": weight,
        "model_version": 1,
    }
    with MODEL_PATH.open("wb") as f:
        pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def load_model() -> dict[str, Any] | None:
    if not MODEL_PATH.exists():
        return None
    with MODEL_PATH.open("rb") as f:
        payload = pickle.load(f)
    if not isinstance(payload, dict) or "model" not in payload or "profile" not in payload:
        raise RuntimeError("Invalid load forecast model payload")
    return payload


def model_status() -> dict[str, Any]:
    report = None
    if REPORT_PATH.exists():
        try:
            report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"model_exists": MODEL_PATH.exists(), "model_path": str(MODEL_PATH), "report_path": str(REPORT_PATH), "report": report}


def predict_load(payload: dict[str, Any], ts: datetime) -> tuple[float, float, dict[str, float]]:
    profile = payload["profile"]
    baseline = profile_baseline(profile, ts)
    correction = float(payload["model"].predict([feature_vector(profile, ts)])[0])
    weight = float(payload.get("correction_weight", 0.0))
    pred = max(0.0, baseline + weight * correction)
    p80 = float((((payload.get("report") or {}).get("uncertainty") or {}).get("p80_kw", 0.75)))
    return pred, p80, {
        "profile_baseline_kw": round(baseline, 4),
        "ml_residual_correction_kw": round(correction, 4),
        "correction_weight": round(weight, 3),
    }
