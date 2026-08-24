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
    "gti_w_m2", "temperature_c", "cloud_cover_pct", "hour_sin", "hour_cos",
    "doy_sin", "doy_cos", "solar_elevation_deg", "daily_max_solar_elevation_deg",
    "daylight_hours", "solar_azimuth_sin", "solar_azimuth_cos", "physical_baseline_kw",
]
REGIMES = [(0.0, 5.0, "0-5"), (5.0, 15.0, "5-15"), (15.0, 30.0, "15-30"),
           (30.0, 45.0, "30-45"), (45.0, 90.0, ">45")]


def _number(value: Any) -> float | None:
    if value in (None, "", "null", "None", "nan"):
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _physical_baseline(capacity_kw: float, gti_w_m2: float) -> float:
    return min(capacity_kw, max(0.0, capacity_kw * gti_w_m2 / 1000.0))


def feature_vector(timestamp: datetime, gti_w_m2: float, temperature_c: float | None,
                   cloud_cover_pct: float | None, capacity_kw: float,
                   latitude_deg: float, longitude_deg: float) -> list[float]:
    ts = timestamp.astimezone(STOCKHOLM)
    hour = ts.hour + ts.minute / 60.0
    doy = ts.timetuple().tm_yday
    solar = solar_features(timestamp, latitude_deg, longitude_deg)
    return [
        float(gti_w_m2), float(temperature_c if temperature_c is not None else 15.0),
        float(cloud_cover_pct if cloud_cover_pct is not None else 50.0),
        math.sin(2 * math.pi * hour / 24.0), math.cos(2 * math.pi * hour / 24.0),
        math.sin(2 * math.pi * doy / 365.25), math.cos(2 * math.pi * doy / 365.25),
        float(solar["solar_elevation_deg"]), float(solar["daily_max_solar_elevation_deg"]),
        float(solar["daylight_hours"]), float(solar["solar_azimuth_sin"]),
        float(solar["solar_azimuth_cos"]), _physical_baseline(capacity_kw, gti_w_m2),
    ]


def _load_rows(capacity_kw: float, lat: float, lon: float) -> list[dict[str, Any]]:
    if not DATASET_PATH.exists():
        raise RuntimeError(f"Training dataset not found: {DATASET_PATH}")
    rows = []
    with DATASET_PATH.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            pv = _number(row.get("pv_power_kw")); gti = _number(row.get("gti_w_m2"))
            if pv is None or gti is None or gti < 5.0:
                continue
            try:
                ts = datetime.fromisoformat(str(row["timestamp_utc"]).replace("Z", "+00:00"))
                if ts.tzinfo is None: ts = ts.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            solar = solar_features(ts, lat, lon)
            if float(solar["solar_elevation_deg"]) <= 0.0:
                continue
            temp = _number(row.get("temperature_c")); cloud = _number(row.get("cloud_cover_pct"))
            baseline = _physical_baseline(capacity_kw, gti)
            rows.append({"ts": ts.astimezone(timezone.utc), "x": feature_vector(ts, gti, temp, cloud, capacity_kw, lat, lon),
                         "y": max(0.0, pv), "gti": gti, "baseline": baseline, "solar": solar})
    rows.sort(key=lambda r: r["ts"])
    if len(rows) < 500: raise RuntimeError(f"Too few daylight PV+GTI rows for training: {len(rows)}")
    return rows


def _split(rows):
    last_ts = rows[-1]["ts"]; test_start = last_ts - timedelta(days=30); validation_start = test_start - timedelta(days=30)
    train = [r for r in rows if r["ts"] < validation_start]
    validation = [r for r in rows if validation_start <= r["ts"] < test_start]
    test = [r for r in rows if r["ts"] >= test_start]
    if min(len(train), len(validation), len(test)) < 100:
        n = len(rows); a = int(n * .70); b = int(n * .85); train, validation, test = rows[:a], rows[a:b], rows[b:]
    periods = {"train_start": train[0]["ts"].isoformat(), "train_end": train[-1]["ts"].isoformat(),
               "validation_start": validation[0]["ts"].isoformat(), "validation_end": validation[-1]["ts"].isoformat(),
               "test_start": test[0]["ts"].isoformat(), "test_end": test[-1]["ts"].isoformat()}
    return train, validation, test, periods


def _metrics(y_true, y_pred, capacity_kw):
    mae = float(mean_absolute_error(y_true, y_pred)); rmse = float(mean_squared_error(y_true, y_pred) ** .5)
    return {"mae_kw": round(mae, 4), "rmse_kw": round(rmse, 4), "nmae_capacity_pct": round(100 * mae / capacity_kw, 3),
            "r2": round(float(r2_score(y_true, y_pred)), 4), "mean_actual_kw": round(float(mean(y_true)), 4)}


def _quantile(values, q):
    if not values: return 0.0
    values = sorted(values); pos = (len(values)-1)*q; lo = int(math.floor(pos)); hi = int(math.ceil(pos))
    if lo == hi: return values[lo]
    f = pos-lo; return values[lo]*(1-f)+values[hi]*f


def _rolling_mean_abs(residuals, window):
    if len(residuals) < window: return []
    return [abs(sum(residuals[i-window+1:i+1])/window) for i in range(window-1, len(residuals))]


def _daily_stats(rows, predictions):
    by_day = defaultdict(lambda: {"actual": 0.0, "pred": 0.0})
    for row, pred in zip(rows, predictions):
        d = row["ts"].astimezone(STOCKHOLM).date().isoformat(); by_day[d]["actual"] += row["y"]*.25; by_day[d]["pred"] += pred*.25
    return [{"actual_kwh": v["actual"], "pred_kwh": v["pred"], "abs_error_kwh": abs(v["actual"]-v["pred"])} for v in by_day.values()]


def _domain(rows):
    e = [float(r["solar"]["solar_elevation_deg"]) for r in rows]; d = [float(r["solar"]["daily_max_solar_elevation_deg"]) for r in rows]
    h = [float(r["solar"]["daylight_hours"]) for r in rows]
    return {"solar_elevation_min_deg": round(min(e),4), "solar_elevation_max_deg": round(max(e),4),
            "daily_max_solar_elevation_min_deg": round(min(d),4), "daily_max_solar_elevation_max_deg": round(max(d),4),
            "daylight_hours_min": round(min(h),4), "daylight_hours_max": round(max(h),4)}


def _seasonal_weight(domain, solar):
    x = float(solar["daily_max_solar_elevation_deg"]); lo = float(domain.get("daily_max_solar_elevation_min_deg", x)); hi = float(domain.get("daily_max_solar_elevation_max_deg", x))
    if lo <= x <= hi: return 1.0
    dist = lo-x if x < lo else x-hi
    if dist <= 3: return .75
    if dist <= 8: return .50
    if dist <= 15: return .25
    return .10


def _regime_name(elevation):
    for lo, hi, name in REGIMES:
        if lo <= elevation < hi: return name
    return ">45"


def _validation_regime_weights(validation, correction_pred):
    result = {}
    for lo, hi, name in REGIMES:
        idx = [i for i,r in enumerate(validation) if lo <= float(r["solar"]["solar_elevation_deg"]) < hi]
        if len(idx) < 40:
            result[name] = {"weight": 0.0, "rows": len(idx), "reason": "insufficient_validation_rows"}; continue
        actual = [validation[i]["y"] for i in idx]; baseline = [validation[i]["baseline"] for i in idx]
        corrected = [max(0.0, baseline[j] + correction_pred[idx[j]]) for j in range(len(idx))]
        base_mae = float(mean_absolute_error(actual, baseline)); corr_mae = float(mean_absolute_error(actual, corrected))
        improvement = 0.0 if base_mae <= 1e-9 else (base_mae-corr_mae)/base_mae
        # 15% validation MAE improvement earns full correction; no improvement earns zero.
        weight = max(0.0, min(1.0, improvement/.15))
        result[name] = {"weight": round(weight,3), "rows": len(idx), "baseline_mae_kw": round(base_mae,4),
                        "corrected_mae_kw": round(corr_mae,4), "relative_improvement": round(improvement,4)}
    return result


def _hybrid_predictions(rows, corrections, regime_weights, domain, capacity_kw):
    preds = []
    for r,c in zip(rows, corrections):
        regime = _regime_name(float(r["solar"]["solar_elevation_deg"])); validation_w = float((regime_weights.get(regime) or {}).get("weight",0.0))
        seasonal_w = _seasonal_weight(domain, r["solar"]); w = validation_w * seasonal_w
        pred = r["baseline"] + w*float(c); preds.append(max(0.0, min(capacity_kw*1.2, pred)))
    return preds


def train_pv_model(capacity_kw=10.0, latitude_deg=None, longitude_deg=None):
    if latitude_deg is None or longitude_deg is None: raise RuntimeError("PV calibration requires latitude and longitude")
    lat=float(latitude_deg); lon=float(longitude_deg); TRAINING_DIR.mkdir(parents=True, exist_ok=True)
    rows=_load_rows(capacity_kw,lat,lon); train,validation,test,periods=_split(rows); domain=_domain(train)
    model=HistGradientBoostingRegressor(loss="squared_error",learning_rate=.05,max_iter=300,max_leaf_nodes=31,min_samples_leaf=20,l2_regularization=.2,random_state=42)
    # v3.2 learns only the residual around physics, not PV power itself.
    model.fit([r["x"] for r in train],[r["y"]-r["baseline"] for r in train])
    val_corr=[float(v) for v in model.predict([r["x"] for r in validation])]; test_corr=[float(v) for v in model.predict([r["x"] for r in test])]
    regime_weights=_validation_regime_weights(validation,val_corr)
    val_pred=_hybrid_predictions(validation,val_corr,regime_weights,domain,capacity_kw); test_pred=_hybrid_predictions(test,test_corr,regime_weights,domain,capacity_kw)
    val_true=[r["y"] for r in validation]; test_true=[r["y"] for r in test]; base_test=[r["baseline"] for r in test]
    combined=validation+test; combined_pred=val_pred+test_pred; signed=[r["y"]-p for r,p in zip(combined,combined_pred)]
    daily=_daily_stats(combined,combined_pred); daily_abs=[r["abs_error_kwh"] for r in daily]; rel=[r["abs_error_kwh"]/max(2.0,r["pred_kwh"]) for r in daily]
    uncertainty={"residual_15m":{"p50_kw":round(_quantile([abs(v) for v in signed],.5),4),"p80_kw":round(_quantile([abs(v) for v in signed],.8),4),"p95_kw":round(_quantile([abs(v) for v in signed],.95),4)},
                 "residual_1h_mean":{"p50_kw":round(_quantile(_rolling_mean_abs(signed,4),.5),4),"p80_kw":round(_quantile(_rolling_mean_abs(signed,4),.8),4),"p95_kw":round(_quantile(_rolling_mean_abs(signed,4),.95),4)},
                 "residual_3h_mean":{"p50_kw":round(_quantile(_rolling_mean_abs(signed,12),.5),4),"p80_kw":round(_quantile(_rolling_mean_abs(signed,12),.8),4),"p95_kw":round(_quantile(_rolling_mean_abs(signed,12),.95),4)},
                 "daily_energy_residual":{"p50_kwh":round(_quantile(daily_abs,.5),4),"p80_kwh":round(_quantile(daily_abs,.8),4),"p95_kwh":round(_quantile(daily_abs,.95),4),"days":len(daily),
                                          "relative_p50":round(_quantile(rel,.5),4),"relative_p80":round(_quantile(rel,.8),4),"relative_p95":round(_quantile(rel,.95),4)}}
    report={"ok":True,"model":"pv_physics_residual_gradient_boosting_v3_2","trained_at":datetime.now(timezone.utc).isoformat(),"capacity_kw":capacity_kw,
            "site":{"latitude":lat,"longitude":lon},"features":FEATURE_NAMES,"target":"pv_actual_kw - physical_baseline_kw",
            "training_filter":{"gti_min_w_m2":5.0,"solar_elevation_min_deg_exclusive":0.0},
            "rows":{"all_daylight":len(rows),"train":len(train),"validation":len(validation),"test":len(test)},"periods":periods,"training_domain":domain,
            "validation_regime_weights":regime_weights,
            "extrapolation_guard":{"seasonal_basis":"daily_max_solar_elevation_deg","final_correction_weight":"validation_regime_weight * seasonal_weight"},
            "metrics":{"validation_hybrid":_metrics(val_true,val_pred,capacity_kw),"test_hybrid":_metrics(test_true,test_pred,capacity_kw),"test_physical_baseline":_metrics(test_true,base_test,capacity_kw)},
            "uncertainty":uncertainty}
    payload={"model":model,"report":report,"feature_names":FEATURE_NAMES,"capacity_kw":capacity_kw,"latitude_deg":lat,"longitude_deg":lon,
             "training_domain":domain,"validation_regime_weights":regime_weights,"model_version":32}
    with MODEL_PATH.open("wb") as f: pickle.dump(payload,f,protocol=pickle.HIGHEST_PROTOCOL)
    REPORT_PATH.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8"); return report


def model_status():
    report=None
    if REPORT_PATH.exists():
        try: report=json.loads(REPORT_PATH.read_text(encoding="utf-8"))
        except Exception: report=None
    return {"model_exists":MODEL_PATH.exists(),"model_path":str(MODEL_PATH),"report_path":str(REPORT_PATH),"report":report}


def load_model():
    if not MODEL_PATH.exists(): return None
    with MODEL_PATH.open("rb") as f: payload=pickle.load(f)
    if not isinstance(payload,dict) or "model" not in payload: raise RuntimeError("Invalid PV calibration model payload")
    return payload


def predict_calibrated(payload,timestamp,gti_w_m2,temperature_c,cloud_cover_pct):
    capacity_kw=float(payload.get("capacity_kw",10.0)); lat=payload.get("latitude_deg"); lon=payload.get("longitude_deg")
    if lat is None or lon is None: raise RuntimeError("Stored PV calibration model lacks site coordinates; retrain model")
    if int(payload.get("model_version",0)) < 32: raise RuntimeError("Stored PV model predates v3.2 residual calibration; retrain model")
    if gti_w_m2 < 2.0: return 0.0,0.0,{"ml_weight":0.0,"extrapolation_guard_active":False}
    solar=solar_features(timestamp,float(lat),float(lon))
    if solar["solar_elevation_deg"] <= 0.0: return 0.0,0.0,{"ml_weight":0.0,"extrapolation_guard_active":False}
    baseline=_physical_baseline(capacity_kw,gti_w_m2); x=feature_vector(timestamp,gti_w_m2,temperature_c,cloud_cover_pct,capacity_kw,float(lat),float(lon))
    correction=float(payload["model"].predict([x])[0]); regime=_regime_name(float(solar["solar_elevation_deg"]))
    validation_w=float((payload.get("validation_regime_weights",{}).get(regime) or {}).get("weight",0.0)); seasonal_w=_seasonal_weight(payload.get("training_domain") or {},solar)
    weight=validation_w*seasonal_w; pred=max(0.0,min(capacity_kw*1.2,baseline+weight*correction))
    u=((payload.get("report") or {}).get("uncertainty") or {}).get("residual_15m") or {}; p80=float(u.get("p80_kw",.8)); cloud=max(0.0,min(1.0,(cloud_cover_pct or 0.0)/100.0))
    uncertainty=max(.10,p80*(.75+.5*cloud)*(1.0+(1.0-seasonal_w)*.5))
    meta={"ml_weight":round(weight,3),"validation_regime_weight":round(validation_w,3),"seasonal_weight":round(seasonal_w,3),"solar_regime":regime,
          "extrapolation_guard_active":seasonal_w<.999,"ml_residual_correction_kw":round(correction,4),"physical_baseline_kw":round(baseline,4)}
    return pred,min(capacity_kw,uncertainty),meta


def dynamic_daily_energy_uncertainty(payload,remaining_energy_kwh,mean_cloud_cover_pct,min_ml_weight,confidence="p80"):
    daily=((((payload.get("report") or {}).get("uncertainty") or {}).get("daily_energy_residual")) or {})
    absolute=float(daily.get(f"{confidence}_kwh",daily.get("p80_kwh",2.0))); relative=float(daily.get(f"relative_{confidence}",daily.get("relative_p80",.30)))
    energy_scaled=max(.25,remaining_energy_kwh*relative); cloud=max(0.0,min(1.0,mean_cloud_cover_pct/100.0)); cloud_factor=.85+.30*cloud
    domain_factor=1.0+(1.0-max(0.0,min(1.0,min_ml_weight)))*.35; value=(.25*absolute+.75*energy_scaled)*cloud_factor*domain_factor
    ceiling=max(1.0,remaining_energy_kwh*1.25+.5)
    return {"kwh":min(value,ceiling),"absolute_component_kwh":absolute,"relative_component":relative,"cloud_factor":cloud_factor,"domain_factor":domain_factor,"basis":"dynamic_empirical_daily_energy_residual_v3_2"}
