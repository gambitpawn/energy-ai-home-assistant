from __future__ import annotations

import csv
import json
import math
import pickle
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

from .solar_geometry import solar_features
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
    "solar_elevation_deg",
    "daily_max_solar_elevation_deg",
    "daylight_hours",
    "solar_azimuth_sin",
    "solar_azimuth_cos",
    "physical_baseline_kw",
]


def _number(value: Any) -> float | None:
    if value in (None, "", "null", "None", "nan"):
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _physical_baseline(capacity_kw: float, gti_w_m2: float) -> float:
    return min(capacity_kw, max(0.0, capacity_kw * gti_w_m2 / 1000.0))


def feature_vector(
    timestamp: datetime,
    gti_w_m2: float,
    temperature_c: float | None,
    cloud_cover_pct: float | None,
    capacity_kw: float,
    latitude_deg: float,
    longitude_deg: float,
) -> list[float]:
    ts = timestamp.astimezone(STOCKHOLM)
    hour = ts.hour + ts.minute / 60.0
    doy = ts.timetuple().tm_yday
    hour_angle = 2.0 * math.pi * hour / 24.0
    doy_angle = 2.0 * math.pi * doy / 365.25
    baseline = _physical_baseline(capacity_kw, gti_w_m2)
    solar = solar_features(timestamp, latitude_deg, longitude_deg)
    return [
        float(gti_w_m2),
        float(temperature_c if temperature_c is not None else 15.0),
        float(cloud_cover_pct if cloud_cover_pct is not None else 50.0),
        math.sin(hour_angle),
        math.cos(hour_angle),
        math.sin(doy_angle),
        math.cos(doy_angle),
        float(solar["solar_elevation_deg"]),
        float(solar["daily_max_solar_elevation_deg"]),
        float(solar["daylight_hours"]),
        float(solar["solar_azimuth_sin"]),
        float(solar["solar_azimuth_cos"]),
        baseline,
    ]


def _load_rows(capacity_kw: float, latitude_deg: float, longitude_deg: float) -> list[dict[str, Any]]:
    if not DATASET_PATH.exists():
        raise RuntimeError(f"Training dataset not found: {DATASET_PATH}")
    rows: list[dict[str, Any]] = []
    with DATASET_PATH.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            pv = _number(row.get("pv_power_kw"))
            gti = _number(row.get("gti_w_m2"))
            if pv is None or gti is None or gti < 5.0:
                continue
            try:
                ts = datetime.fromisoformat(str(row["timestamp_utc"]).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            temp = _number(row.get("temperature_c"))
            cloud = _number(row.get("cloud_cover_pct"))
            solar = solar_features(ts, latitude_deg, longitude_deg)
            x = feature_vector(ts, gti, temp, cloud, capacity_kw, latitude_deg, longitude_deg)
            rows.append({
                "ts": ts.astimezone(timezone.utc),
                "x": x,
                "y": max(0.0, pv),
                "gti": gti,
                "solar": solar,
            })
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


def _rolling_mean_abs(residuals: list[float], window: int) -> list[float]:
    if len(residuals) < window:
        return []
    return [abs(sum(residuals[i - window + 1:i + 1]) / window) for i in range(window - 1, len(residuals))]


def _daily_energy_residuals(rows: list[dict[str, Any]], predictions: list[float]) -> list[float]:
    by_day: dict[str, float] = defaultdict(float)
    for row, pred in zip(rows, predictions):
        day = row["ts"].astimezone(STOCKHOLM).date().isoformat()
        by_day[day] += (row["y"] - pred) * 0.25
    return [abs(v) for v in by_day.values()]


def _domain(rows: list[dict[str, Any]]) -> dict[str, float]:
    elevation = [float(r["solar"]["solar_elevation_deg"]) for r in rows]
    daily_max = [float(r["solar"]["daily_max_solar_elevation_deg"]) for r in rows]
    daylight = [float(r["solar"]["daylight_hours"]) for r in rows]
    return {
        "solar_elevation_min_deg": round(min(elevation), 4),
        "solar_elevation_max_deg": round(max(elevation), 4),
        "daily_max_solar_elevation_min_deg": round(min(daily_max), 4),
        "daily_max_solar_elevation_max_deg": round(max(daily_max), 4),
        "daylight_hours_min": round(min(daylight), 4),
        "daylight_hours_max": round(max(daylight), 4),
    }


def _ml_weight(domain: dict[str, float], solar: dict[str, float]) -> float:
    """Blend toward physics when seasonal solar geometry is outside training support."""
    daily_max = float(solar["daily_max_solar_elevation_deg"])
    low = float(domain.get("daily_max_solar_elevation_min_deg", daily_max))
    high = float(domain.get("daily_max_solar_elevation_max_deg", daily_max))
    if low <= daily_max <= high:
        return 1.0
    distance = low - daily_max if daily_max < low else daily_max - high
    if distance <= 3.0:
        return 0.75
    if distance <= 8.0:
        return 0.50
    if distance <= 15.0:
        return 0.25
    return 0.10


def train_pv_model(capacity_kw: float = 10.0, latitude_deg: float | None = None, longitude_deg: float | None = None) -> dict[str, Any]:
    if latitude_deg is None or longitude_deg is None:
        raise RuntimeError("PV calibration requires latitude and longitude")
    latitude_deg = float(latitude_deg)
    longitude_deg = float(longitude_deg)
    TRAINING_DIR.mkdir(parents=True, exist_ok=True)
    rows = _load_rows(capacity_kw, latitude_deg, longitude_deg)
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
    model.fit([r["x"] for r in train], [r["y"] for r in train])

    def predict_raw(part):
        raw = model.predict([r["x"] for r in part])
        return [max(0.0, min(capacity_kw * 1.2, float(v))) for v in raw]

    validation_pred = predict_raw(validation)
    test_pred = predict_raw(test)
    validation_true = [r["y"] for r in validation]
    test_true = [r["y"] for r in test]
    baseline_test = [_physical_baseline(capacity_kw, r["gti"]) for r in test]

    combined_rows = validation + test
    combined_pred = validation_pred + test_pred
    residual_signed = [r["y"] - p for r, p in zip(combined_rows, combined_pred)]
    residual_15m = [abs(v) for v in residual_signed]
    residual_1h = _rolling_mean_abs(residual_signed, 4)
    residual_3h = _rolling_mean_abs(residual_signed, 12)
    daily_energy = _daily_energy_residuals(combined_rows, combined_pred)

    uncertainty = {
        "residual_15m": {
            "p50_kw": round(_quantile(residual_15m, 0.50), 4),
            "p80_kw": round(_quantile(residual_15m, 0.80), 4),
            "p95_kw": round(_quantile(residual_15m, 0.95), 4),
        },
        "residual_1h_mean": {
            "p50_kw": round(_quantile(residual_1h, 0.50), 4),
            "p80_kw": round(_quantile(residual_1h, 0.80), 4),
            "p95_kw": round(_quantile(residual_1h, 0.95), 4),
        },
        "residual_3h_mean": {
            "p50_kw": round(_quantile(residual_3h, 0.50), 4),
            "p80_kw": round(_quantile(residual_3h, 0.80), 4),
            "p95_kw": round(_quantile(residual_3h, 0.95), 4),
        },
        "daily_energy_residual": {
            "p50_kwh": round(_quantile(daily_energy, 0.50), 4),
            "p80_kwh": round(_quantile(daily_energy, 0.80), 4),
            "p95_kwh": round(_quantile(daily_energy, 0.95), 4),
            "days": len(daily_energy),
        },
    }
    domain = _domain(train)
    report = {
        "ok": True,
        "model": "pv_hist_gradient_boosting_v3_guarded",
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "capacity_kw": capacity_kw,
        "site": {"latitude": latitude_deg, "longitude": longitude_deg},
        "features": FEATURE_NAMES,
        "rows": {"all_daylight": len(rows), "train": len(train), "validation": len(validation), "test": len(test)},
        "periods": periods,
        "training_domain": domain,
        "extrapolation_guard": {
            "basis": "daily_max_solar_elevation_deg",
            "inside_domain_ml_weight": 1.0,
            "outside_weights": {"0-3deg": 0.75, "3-8deg": 0.50, "8-15deg": 0.25, ">15deg": 0.10},
        },
        "metrics": {
            "validation_ml": _metrics(validation_true, validation_pred, capacity_kw),
            "test_ml": _metrics(test_true, test_pred, capacity_kw),
            "test_physical_baseline": _metrics(test_true, baseline_test, capacity_kw),
        },
        "uncertainty": uncertainty,
    }
    payload = {
        "model": model,
        "report": report,
        "feature_names": FEATURE_NAMES,
        "capacity_kw": capacity_kw,
        "latitude_deg": latitude_deg,
        "longitude_deg": longitude_deg,
        "training_domain": domain,
        "model_version": 3,
    }
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


def predict_calibrated(
    payload: dict[str, Any],
    timestamp: datetime,
    gti_w_m2: float,
    temperature_c: float | None,
    cloud_cover_pct: float | None,
) -> tuple[float, float, dict[str, Any]]:
    capacity_kw = float(payload.get("capacity_kw", 10.0))
    latitude_deg = payload.get("latitude_deg")
    longitude_deg = payload.get("longitude_deg")
    if latitude_deg is None or longitude_deg is None:
        raise RuntimeError("Stored PV calibration model lacks solar-geometry site coordinates; retrain model")
    if int(payload.get("model_version", 0)) < 3:
        raise RuntimeError("Stored PV model predates v3 extrapolation guard; retrain model")
    if gti_w_m2 < 2.0:
        return 0.0, 0.0, {"ml_weight": 0.0, "extrapolation_guard_active": False}

    latitude_deg = float(latitude_deg)
    longitude_deg = float(longitude_deg)
    solar = solar_features(timestamp, latitude_deg, longitude_deg)
    if solar["solar_elevation_deg"] <= -1.0:
        return 0.0, 0.0, {"ml_weight": 0.0, "extrapolation_guard_active": False}

    baseline = _physical_baseline(capacity_kw, gti_w_m2)
    x = feature_vector(timestamp, gti_w_m2, temperature_c, cloud_cover_pct, capacity_kw, latitude_deg, longitude_deg)
    ml_pred = float(payload["model"].predict([x])[0])
    ml_pred = max(0.0, min(capacity_kw * 1.2, ml_pred))
    domain = payload.get("training_domain") or {}
    weight = _ml_weight(domain, solar)
    pred = weight * ml_pred + (1.0 - weight) * baseline
    pred = max(0.0, min(capacity_kw * 1.2, pred))

    report = payload.get("report") or {}
    u = report.get("uncertainty") or {}
    p80 = float((u.get("residual_15m") or {}).get("p80_kw", 0.8))
    cloud_fraction = max(0.0, min(1.0, (cloud_cover_pct or 0.0) / 100.0))
    # Increase point uncertainty as we leave the training domain, but do not
    # propagate that local penalty directly into whole-day energy uncertainty.
    domain_penalty = 1.0 + (1.0 - weight) * 0.75
    uncertainty = max(0.10, p80 * (0.75 + 0.5 * cloud_fraction) * domain_penalty)
    meta = {
        "ml_weight": round(weight, 3),
        "extrapolation_guard_active": weight < 0.999,
        "ml_prediction_kw": round(ml_pred, 4),
        "physical_baseline_kw": round(baseline, 4),
    }
    return pred, min(capacity_kw, uncertainty), meta


def daily_energy_uncertainty(payload: dict[str, Any], confidence: str = "p80") -> float:
    report = payload.get("report") or {}
    daily = ((report.get("uncertainty") or {}).get("daily_energy_residual") or {})
    return float(daily.get(f"{confidence}_kwh", daily.get("p80_kwh", 2.0)))
