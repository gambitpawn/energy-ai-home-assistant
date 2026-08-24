from __future__ import annotations

import csv
import json
import math
import pickle
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

from .training import DATASET_PATH, TRAINING_DIR

MODEL_PATH = TRAINING_DIR / "pv_calibration_v1.pkl"
REPORT_PATH = TRAINING_DIR / "pv_calibration_report.json"
STOCKHOLM = ZoneInfo("Europe/Stockholm")
FEATURE_NAMES = [
    "gti_w_m2",
    "temperature_c",
    "cloud_cover_pct",
    "hour_sin",
    "hour_cos",
    "doy_sin",
    "doy_cos",
    "physical_baseline_kw",
]


def _number(value: Any) -> float | None:
    if value in (None, "", "null", "None", "nan"):
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def feature_vector(timestamp: datetime, gti_w_m2: float, temperature_c: float | None, cloud_cover_pct: float | None, capacity_kw: float) -> list[float]:
    ts = timestamp.astimezone(STOCKHOLM)
    hour = ts.hour + ts.minute / 60.0
    doy = ts.timetuple().tm_yday
    hour_angle = 2.0 * math.pi * hour / 24.0
    doy_angle = 2.0 * math.pi * doy / 365.25
    baseline = min(capacity_kw, max(0.0, capacity_kw * gti_w_m2 / 1000.0))
    return [
        float(gti_w_m2),
        float(temperature_c if temperature_c is not None else 15.0),
        float(cloud_cover_pct if cloud_cover_pct is not None else 50.0),
        math.sin(hour_angle),
        math.cos(hour_angle),
        math.sin(doy_angle),
        math.cos(doy_angle),
        baseline,
    ]


def _load_rows(capacity_kw: float) -> list[dict[str, Any]]:
    if not DATASET_PATH.exists():
        raise RuntimeError(f"Training dataset not found: {DATASET_PATH}")
    rows: list[dict[str, Any]] = []
    with DATASET_PATH.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            pv = _number(row.get("pv_power_kw"))
            gti = _number(row.get("gti_w_m2"))
            if pv is None or gti is None:
                continue
            # Train on daylight observations. Night-time is deterministically zeroed in inference.
            if gti < 5.0:
                continue
            try:
                ts = datetime.fromisoformat(str(row["timestamp_utc"]).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            temp = _number(row.get("temperature_c"))
            cloud = _number(row.get("cloud_cover_pct"))
            x = feature_vector(ts, gti, temp, cloud, capacity_kw)
            rows.append({"ts": ts.astimezone(timezone.utc), "x": x, "y": max(0.0, pv), "gti": gti})
    rows.sort(key=lambda r: r["ts"])
    if len(rows) < 500:
        raise RuntimeError(f"Too few daylight PV+GTI rows for training: {len(rows)}")
    return rows


def _split(rows: list[dict[str, Any]]) -> tuple[list, list, list, dict[str, str]]:
    last_ts = rows[-1]["ts"]
    test_start = last_ts - timedelta(days=30)
    validation_start = test_start - timedelta(days=30)
    train = [r for r in rows if r["ts"] < validation_start]
    validation = [r for r in rows if validation_start <= r["ts"] < test_start]
    test = [r for r in rows if r["ts"] >= test_start]
    if min(len(train), len(validation), len(test)) < 100:
        n = len(rows)
        a = int(n * 0.70)
        b = int(n * 0.85)
        train, validation, test = rows[:a], rows[a:b], rows[b:]
    periods = {
        "train_start": train[0]["ts"].isoformat(), "train_end": train[-1]["ts"].isoformat(),
        "validation_start": validation[0]["ts"].isoformat(), "validation_end": validation[-1]["ts"].isoformat(),
        "test_start": test[0]["ts"].isoformat(), "test_end": test[-1]["ts"].isoformat(),
    }
    return train, validation, test, periods


def _metrics(y_true: list[float], y_pred: list[float], capacity_kw: float) -> dict[str, float]:
    mae = float(mean_absolute_error(y_true, y_pred))
    rmse = float(mean_squared_error(y_true, y_pred) ** 0.5)
    return {
        "mae_kw": round(mae, 4),
        "rmse_kw": round(rmse, 4),
        "nmae_capacity_pct": round(100.0 * mae / capacity_kw, 3),
        "r2": round(float(r2_score(y_true, y_pred)), 4),
        "mean_actual_kw": round(float(mean(y_true)), 4),
    }


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    pos = (len(values) - 1) * q
    lo = int(math.floor(pos)); hi = int(math.ceil(pos))
    if lo == hi:
        return values[lo]
    frac = pos - lo
    return values[lo] * (1.0 - frac) + values[hi] * frac


def train_pv_model(capacity_kw: float = 10.0) -> dict[str, Any]:
    TRAINING_DIR.mkdir(parents=True, exist_ok=True)
    rows = _load_rows(capacity_kw)
    train, validation, test, periods = _split(rows)
    model = HistGradientBoostingRegressor(
        loss="squared_error",
        learning_rate=0.05,
        max_iter=300,
        max_leaf_nodes=31,
        min_samples_leaf=20,
        l2_regularization=0.2,
        random_state=42,
    )
    x_train = [r["x"] for r in train]
    y_train = [r["y"] for r in train]
    model.fit(x_train, y_train)

    def predict(part):
        raw = model.predict([r["x"] for r in part])
        return [max(0.0, min(capacity_kw * 1.2, float(v))) for v in raw]

    validation_pred = predict(validation)
    test_pred = predict(test)
    test_true = [r["y"] for r in test]
    validation_true = [r["y"] for r in validation]
    baseline_test = [min(capacity_kw, capacity_kw * r["gti"] / 1000.0) for r in test]

    residuals = [abs(a - p) for a, p in zip(validation_true + test_true, validation_pred + test_pred)]
    uncertainty = {
        "absolute_residual_p50_kw": round(_quantile(residuals, 0.50), 4),
        "absolute_residual_p80_kw": round(_quantile(residuals, 0.80), 4),
        "absolute_residual_p95_kw": round(_quantile(residuals, 0.95), 4),
    }
    report = {
        "ok": True,
        "model": "pv_hist_gradient_boosting_v1",
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "capacity_kw": capacity_kw,
        "features": FEATURE_NAMES,
        "rows": {"all_daylight": len(rows), "train": len(train), "validation": len(validation), "test": len(test)},
        "periods": periods,
        "metrics": {
            "validation_ml": _metrics(validation_true, validation_pred, capacity_kw),
            "test_ml": _metrics(test_true, test_pred, capacity_kw),
            "test_physical_baseline": _metrics(test_true, baseline_test, capacity_kw),
        },
        "uncertainty": uncertainty,
    }
    payload = {"model": model, "report": report, "feature_names": FEATURE_NAMES, "capacity_kw": capacity_kw}
    with MODEL_PATH.open("wb") as f:
        pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def model_status() -> dict[str, Any]:
    report = None
    if REPORT_PATH.exists():
        try:
            report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
        except Exception:
            report = None
    return {"model_exists": MODEL_PATH.exists(), "model_path": str(MODEL_PATH), "report_path": str(REPORT_PATH), "report": report}


def load_model() -> dict[str, Any] | None:
    if not MODEL_PATH.exists():
        return None
    with MODEL_PATH.open("rb") as f:
        payload = pickle.load(f)
    if not isinstance(payload, dict) or "model" not in payload:
        raise RuntimeError("Invalid PV calibration model payload")
    return payload


def predict_calibrated(payload: dict[str, Any], timestamp: datetime, gti_w_m2: float, temperature_c: float | None, cloud_cover_pct: float | None) -> tuple[float, float]:
    capacity_kw = float(payload.get("capacity_kw", 10.0))
    if gti_w_m2 < 2.0:
        return 0.0, 0.0
    x = feature_vector(timestamp, gti_w_m2, temperature_c, cloud_cover_pct, capacity_kw)
    pred = float(payload["model"].predict([x])[0])
    pred = max(0.0, min(capacity_kw * 1.2, pred))
    report = payload.get("report") or {}
    p80 = float((report.get("uncertainty") or {}).get("absolute_residual_p80_kw", 0.5))
    cloud_fraction = max(0.0, min(1.0, (cloud_cover_pct or 0.0) / 100.0))
    uncertainty = max(0.10, p80 * (0.75 + 0.5 * cloud_fraction))
    return pred, min(capacity_kw, uncertainty)
