from __future__ import annotations

import csv
import json
import math
import pickle
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from statistics import mean, median
from typing import Any
from zoneinfo import ZoneInfo

from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

from .training import DATASET_PATH, TRAINING_DIR

STOCKHOLM = ZoneInfo("Europe/Stockholm")
MODEL_PATH = TRAINING_DIR / "load_forecast_v3.pkl"
REPORT_PATH = TRAINING_DIR / "load_forecast_report.json"
MODEL_NAME = "load_validation_ensemble_residual_gradient_boosting_v3"
MODEL_VERSION = 30

FEATURE_NAMES = [
    "ensemble_baseline_kw", "long_profile_kw", "recent_slot_kw", "recent_level_factor",
    "temperature_c", "hour_sin", "hour_cos", "dow_sin", "dow_cos", "doy_sin", "doy_cos", "is_weekend",
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


def _weekday_slot_key(ts: datetime) -> tuple[int, int]:
    local = ts.astimezone(STOCKHOLM)
    return local.weekday(), _slot(ts)


def _build_profile(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_weekday_slot: dict[tuple[int, int], list[float]] = defaultdict(list)
    by_slot: dict[int, list[float]] = defaultdict(list)
    all_values: list[float] = []
    for r in rows:
        local = r["ts"].astimezone(STOCKHOLM)
        s = _slot(r["ts"]); y = float(r["y"])
        by_weekday_slot[(local.weekday(), s)].append(y); by_slot[s].append(y); all_values.append(y)
    return {
        "weekday_slot": {f"{dow}:{slot}": median(vals) for (dow, slot), vals in by_weekday_slot.items()},
        "slot": {str(slot): median(vals) for slot, vals in by_slot.items()},
        "global": median(all_values) if all_values else 0.5,
    }


def profile_baseline(profile: dict[str, Any], ts: datetime) -> float:
    local = ts.astimezone(STOCKHOLM); s = _slot(ts); key = f"{local.weekday()}:{s}"
    if key in (profile.get("weekday_slot") or {}): return max(0.0, float(profile["weekday_slot"][key]))
    if str(s) in (profile.get("slot") or {}): return max(0.0, float(profile["slot"][str(s)]))
    return max(0.0, float(profile.get("global", 0.5)))


def _ensemble_baseline(ctx: dict[str, float], recent_weight: float, level_weight: float) -> float:
    shaped = (1.0 - recent_weight) * ctx["long_profile_kw"] + recent_weight * ctx["recent_slot_kw"]
    factor = 1.0 + level_weight * (ctx["recent_level_factor"] - 1.0)
    return max(0.0, shaped * factor)


def _recent_context(profile: dict[str, Any], history: list[dict[str, Any]], ts: datetime) -> dict[str, float]:
    cutoff = ts - timedelta(days=28); recent = [r for r in history if cutoff <= r["ts"] < ts]
    long_base = profile_baseline(profile, ts)
    if not recent:
        return {"long_profile_kw": long_base, "recent_slot_kw": long_base, "recent_level_factor": 1.0}
    key = _weekday_slot_key(ts)
    same_slot = [float(r["y"]) for r in recent if _weekday_slot_key(r["ts"]) == key]
    recent_slot = median(same_slot) if same_slot else long_base
    level_rows = [r for r in recent if r["ts"] >= ts - timedelta(days=7)]
    if level_rows:
        actual_sum = sum(float(r["y"]) for r in level_rows)
        expected_sum = sum(profile_baseline(profile, r["ts"]) for r in level_rows)
        level_factor = actual_sum / expected_sum if expected_sum > 0.05 * len(level_rows) else 1.0
    else:
        level_factor = 1.0
    return {"long_profile_kw": long_base, "recent_slot_kw": max(0.0, recent_slot),
            "recent_level_factor": max(0.60, min(1.80, level_factor))}


def _features(ctx: dict[str, float], baseline: float, ts: datetime, temperature_c: float | None) -> list[float]:
    local = ts.astimezone(STOCKHOLM); hour = local.hour + local.minute / 60.0; dow = local.weekday(); doy = local.timetuple().tm_yday
    return [baseline, ctx["long_profile_kw"], ctx["recent_slot_kw"], ctx["recent_level_factor"],
            float(temperature_c if temperature_c is not None else 12.0),
            math.sin(2 * math.pi * hour / 24.0), math.cos(2 * math.pi * hour / 24.0),
            math.sin(2 * math.pi * dow / 7.0), math.cos(2 * math.pi * dow / 7.0),
            math.sin(2 * math.pi * doy / 365.25), math.cos(2 * math.pi * doy / 365.25), 1.0 if dow >= 5 else 0.0]


def _load_rows() -> list[dict[str, Any]]:
    if not DATASET_PATH.exists(): raise RuntimeError(f"Training dataset not found: {DATASET_PATH}")
    out = []
    with DATASET_PATH.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            load = _number(row.get("load_power_kw"))
            if load is None or load < 0: continue
            try: ts = _parse_ts(row["timestamp_utc"])
            except Exception: continue
            out.append({"ts": ts, "y": float(load), "temperature_c": _number(row.get("temperature_c"))})
    out.sort(key=lambda r: r["ts"])
    if len(out) < 1000: raise RuntimeError(f"Too few load rows for training: {len(out)}")
    return out


def _split(rows: list[dict[str, Any]]):
    last = rows[-1]["ts"]; test_start = last - timedelta(days=30); val_start = test_start - timedelta(days=30)
    train = [r for r in rows if r["ts"] < val_start]; val = [r for r in rows if val_start <= r["ts"] < test_start]; test = [r for r in rows if r["ts"] >= test_start]
    if min(len(train), len(val), len(test)) < 200:
        n = len(rows); a = int(n * .70); b = int(n * .85); train, val, test = rows[:a], rows[a:b], rows[b:]
    return train, val, test, {
        "train_start": train[0]["ts"].isoformat(), "train_end": train[-1]["ts"].isoformat(),
        "validation_start": val[0]["ts"].isoformat(), "validation_end": val[-1]["ts"].isoformat(),
        "test_start": test[0]["ts"].isoformat(), "test_end": test[-1]["ts"].isoformat()}


def _metrics(y_true: list[float], y_pred: list[float]) -> dict[str, float]:
    mae = float(mean_absolute_error(y_true, y_pred)); rmse = float(mean_squared_error(y_true, y_pred) ** 0.5); avg = float(mean(y_true)) if y_true else 0.0
    return {"mae_kw": round(mae, 4), "rmse_kw": round(rmse, 4), "r2": round(float(r2_score(y_true, y_pred)), 4),
            "mean_actual_kw": round(avg, 4), "mae_pct_of_mean": round(100.0 * mae / avg, 2) if avg > 0 else 0.0}


def _quantile(values: list[float], q: float) -> float:
    if not values: return 0.0
    values = sorted(values); pos = (len(values) - 1) * q; lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    if lo == hi: return values[lo]
    f = pos - lo; return values[lo] * (1 - f) + values[hi] * f


def _validation_weight(y_true: list[float], baseline: list[float], corrected: list[float]) -> tuple[float, dict[str, float]]:
    b = float(mean_absolute_error(y_true, baseline)); c = float(mean_absolute_error(y_true, corrected)); improvement = (b - c) / b if b > 1e-9 else 0.0
    weight = max(0.0, min(1.0, improvement / 0.10))
    return weight, {"baseline_mae_kw": round(b, 4), "corrected_mae_kw": round(c, 4), "relative_improvement": round(improvement, 4), "correction_weight": round(weight, 3)}


def _walk_contexts(profile: dict[str, Any], seed_history: list[dict[str, Any]], part: list[dict[str, Any]]):
    slot_queues: dict[tuple[int, int], deque[tuple[datetime, float]]] = defaultdict(deque)
    level_queue: deque[tuple[datetime, float, float]] = deque(); actual_sum = 0.0; expected_sum = 0.0
    def add(r):
        nonlocal actual_sum, expected_sum
        ts = r["ts"]; y = float(r["y"]); exp = profile_baseline(profile, ts)
        slot_queues[_weekday_slot_key(ts)].append((ts, y)); level_queue.append((ts, y, exp)); actual_sum += y; expected_sum += exp
    seed = sorted(seed_history, key=lambda r: r["ts"])
    if part:
        cutoff = part[0]["ts"] - timedelta(days=28); seed = [r for r in seed if cutoff <= r["ts"] < part[0]["ts"]]
    for r in seed: add(r)
    contexts = []
    for r in part:
        ts = r["ts"]; key = _weekday_slot_key(ts); sq = slot_queues[key]; cutoff28 = ts - timedelta(days=28); cutoff7 = ts - timedelta(days=7)
        while sq and sq[0][0] < cutoff28: sq.popleft()
        while level_queue and level_queue[0][0] < cutoff7:
            _, y0, e0 = level_queue.popleft(); actual_sum -= y0; expected_sum -= e0
        long_base = profile_baseline(profile, ts); recent_slot = median([v for _, v in sq]) if sq else long_base
        level_factor = actual_sum / expected_sum if level_queue and expected_sum > 0.05 * len(level_queue) else 1.0
        contexts.append({"long_profile_kw": long_base, "recent_slot_kw": max(0.0, recent_slot), "recent_level_factor": max(0.60, min(1.80, level_factor))})
        add(r)
    return contexts


def _select_baseline(val_true: list[float], contexts: list[dict[str, float]]) -> tuple[dict[str, float], list[dict[str, float]]]:
    trials = []
    for rw_i in range(11):
        rw = rw_i / 10.0
        for lw_i in range(11):
            lw = lw_i / 10.0
            pred = [_ensemble_baseline(c, rw, lw) for c in contexts]
            mae = float(mean_absolute_error(val_true, pred)); rmse = float(mean_squared_error(val_true, pred) ** 0.5)
            trials.append({"recent_weight": rw, "level_weight": lw, "mae_kw": mae, "rmse_kw": rmse})
    trials.sort(key=lambda x: (x["mae_kw"], x["rmse_kw"]))
    best = dict(trials[0]); best["mae_kw"] = round(best["mae_kw"], 4); best["rmse_kw"] = round(best["rmse_kw"], 4)
    shortlist = [{**t, "mae_kw": round(t["mae_kw"], 4), "rmse_kw": round(t["rmse_kw"], 4)} for t in trials[:10]]
    return best, shortlist


def train_load_model() -> dict[str, Any]:
    TRAINING_DIR.mkdir(parents=True, exist_ok=True)
    rows = _load_rows(); train, val, test, periods = _split(rows); profile = _build_profile(train)
    train_ctx = _walk_contexts(profile, [], train); val_ctx = _walk_contexts(profile, train[-3000:], val); test_ctx = _walk_contexts(profile, (train + val)[-3000:], test)
    val_true = [r["y"] for r in val]; test_true = [r["y"] for r in test]

    selected, shortlist = _select_baseline(val_true, val_ctx)
    rw = float(selected["recent_weight"]); lw = float(selected["level_weight"])
    train_base = [_ensemble_baseline(c, rw, lw) for c in train_ctx]
    val_base = [_ensemble_baseline(c, rw, lw) for c in val_ctx]
    test_base = [_ensemble_baseline(c, rw, lw) for c in test_ctx]

    x_train = [_features(c, b, r["ts"], r.get("temperature_c")) for r, c, b in zip(train, train_ctx, train_base)]
    residual_train = [r["y"] - b for r, b in zip(train, train_base)]
    model = HistGradientBoostingRegressor(loss="squared_error", learning_rate=0.05, max_iter=250, max_leaf_nodes=31, min_samples_leaf=30, l2_regularization=0.3, random_state=42)
    model.fit(x_train, residual_train)

    val_corr = model.predict([_features(c, b, r["ts"], r.get("temperature_c")) for r, c, b in zip(val, val_ctx, val_base)])
    val_full = [max(0.0, b + float(c)) for b, c in zip(val_base, val_corr)]
    correction_weight, gate = _validation_weight(val_true, val_base, val_full)
    val_pred = [max(0.0, b + correction_weight * float(c)) for b, c in zip(val_base, val_corr)]
    test_corr = model.predict([_features(c, b, r["ts"], r.get("temperature_c")) for r, c, b in zip(test, test_ctx, test_base)])
    test_pred = [max(0.0, b + correction_weight * float(c)) for b, c in zip(test_base, test_corr)]

    static_val = [c["long_profile_kw"] for c in val_ctx]; static_test = [c["long_profile_kw"] for c in test_ctx]
    v21_val = [_ensemble_baseline(c, .45, 1.0) for c in val_ctx]; v21_test = [_ensemble_baseline(c, .45, 1.0) for c in test_ctx]
    residuals = [abs(y - p) for y, p in zip(val_true + test_true, val_pred + test_pred)]
    uncertainty = {"p50_kw": round(_quantile(residuals, .50), 4), "p80_kw": round(_quantile(residuals, .80), 4), "p95_kw": round(_quantile(residuals, .95), 4)}
    report = {
        "ok": True, "model": MODEL_NAME, "trained_at": datetime.now(timezone.utc).isoformat(), "target": "house_load_kw",
        "baseline": "validation-optimized blend of static profile, recent weekday-slot and 7d level factor",
        "features": FEATURE_NAMES, "temperature_training_rows": sum(r.get("temperature_c") is not None for r in rows),
        "rows": {"all": len(rows), "train": len(train), "validation": len(val), "test": len(test)}, "periods": periods,
        "baseline_selection": {"objective": "minimum validation MAE; RMSE tie-break", "grid_step": 0.1,
                               "selected_recent_weight": rw, "selected_level_weight": lw, "best_validation": selected, "top_10": shortlist,
                               "special_cases": {"static_v1": {"recent_weight": 0.0, "level_weight": 0.0}, "v2_1": {"recent_weight": 0.45, "level_weight": 1.0}}},
        "validation_gate": gate,
        "metrics": {"validation_hybrid": _metrics(val_true, val_pred), "validation_selected_baseline": _metrics(val_true, val_base),
                    "validation_static_v1": _metrics(val_true, static_val), "validation_v2_1_baseline": _metrics(val_true, v21_val),
                    "test_hybrid": _metrics(test_true, test_pred), "test_selected_baseline": _metrics(test_true, test_base),
                    "test_static_v1": _metrics(test_true, static_test), "test_v2_1_baseline": _metrics(test_true, v21_test)},
        "uncertainty": uncertainty,
    }
    payload = {"model": model, "profile": profile, "report": report, "feature_names": FEATURE_NAMES,
               "correction_weight": correction_weight, "recent_weight": rw, "level_weight": lw, "model_version": MODEL_VERSION}
    with MODEL_PATH.open("wb") as f: pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def load_model() -> dict[str, Any] | None:
    if not MODEL_PATH.exists(): return None
    with MODEL_PATH.open("rb") as f: payload = pickle.load(f)
    if not isinstance(payload, dict) or "model" not in payload or "profile" not in payload: raise RuntimeError("Invalid load forecast model payload")
    return payload


def model_status() -> dict[str, Any]:
    report = None
    if REPORT_PATH.exists():
        try: report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
        except Exception: pass
    return {"model_exists": MODEL_PATH.exists(), "model_path": str(MODEL_PATH), "report_path": str(REPORT_PATH), "report": report}


def predict_load(payload: dict[str, Any], ts: datetime, history: list[dict[str, Any]] | None = None, temperature_c: float | None = None) -> tuple[float, float, dict[str, float]]:
    if int(payload.get("model_version", 0)) < MODEL_VERSION: raise RuntimeError("Stored load model predates v3; retrain")
    profile = payload["profile"]; ctx = _recent_context(profile, history or [], ts)
    rw = float(payload.get("recent_weight", 0.0)); lw = float(payload.get("level_weight", 0.0)); baseline = _ensemble_baseline(ctx, rw, lw)
    correction = float(payload["model"].predict([_features(ctx, baseline, ts, temperature_c)])[0]); weight = float(payload.get("correction_weight", 0.0))
    pred = max(0.0, baseline + weight * correction); p80 = float((((payload.get("report") or {}).get("uncertainty") or {}).get("p80_kw", 0.75)))
    return pred, p80, {"ensemble_baseline_kw": round(baseline, 4), "long_profile_kw": round(ctx["long_profile_kw"], 4),
                       "recent_slot_kw": round(ctx["recent_slot_kw"], 4), "recent_level_factor": round(ctx["recent_level_factor"], 4),
                       "recent_weight": round(rw, 2), "level_weight": round(lw, 2), "ml_residual_correction_kw": round(correction, 4),
                       "correction_weight": round(weight, 3)}
