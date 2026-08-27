from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from .engine_contract import EngineInput

FEATURE_SCHEMA = "neural_v1_features_v1"
BLOCK_INTERVALS = 8  # 2 hours at 15-minute resolution
BLOCK_COUNT = 18     # 36 hours
BLOCK_FEATURES = (
    "load_mean_kw",
    "pv_mean_kw",
    "net_mean_kw",
    "uncertainty_mean_kw",
    "known_price_mean_ore_kwh",
    "price_known_fraction",
)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def feature_names() -> list[str]:
    names = [
        "initial_soc_pct",
        "decision_hour_sin",
        "decision_hour_cos",
        "decision_dow_sin",
        "decision_dow_cos",
        "horizon_fraction",
        "price_known_fraction",
        "known_price_min_ore_kwh",
        "known_price_max_ore_kwh",
        "known_price_spread_ore_kwh",
        "forecast_load_energy_kwh",
        "forecast_pv_energy_kwh",
        "forecast_net_energy_kwh",
        "mean_load_uncertainty_kw",
        "mean_pv_uncertainty_kw",
    ]
    for block in range(BLOCK_COUNT):
        for name in BLOCK_FEATURES:
            names.append(f"b{block:02d}_{name}")
    return names


FEATURE_NAMES = tuple(feature_names())


def vectorize(engine_input: EngineInput) -> list[float]:
    rows = list(engine_input.horizon_rows)[: BLOCK_COUNT * BLOCK_INTERVALS]
    dt_h = float(engine_input.interval_minutes) / 60.0
    decision = datetime.fromisoformat(engine_input.decision_start.replace("Z", "+00:00"))
    hour = decision.hour + decision.minute / 60.0
    dow = float(decision.weekday())

    loads = [float(r.get("load_kw") or 0.0) for r in rows]
    pvs = [float(r.get("pv_kw") or 0.0) for r in rows]
    load_u = [float(r.get("load_uncertainty_kw") or 0.0) for r in rows]
    pv_u = [float(r.get("pv_uncertainty_kw") or 0.0) for r in rows]
    known_prices = [
        float(r["price_ore_kwh"])
        for r in rows
        if bool(r.get("price_known")) and r.get("price_ore_kwh") is not None
    ]
    known_fraction = len(known_prices) / max(1, len(rows))
    price_min = min(known_prices) if known_prices else 0.0
    price_max = max(known_prices) if known_prices else 0.0

    out = [
        float(engine_input.initial_soc_pct),
        math.sin(2.0 * math.pi * hour / 24.0),
        math.cos(2.0 * math.pi * hour / 24.0),
        math.sin(2.0 * math.pi * dow / 7.0),
        math.cos(2.0 * math.pi * dow / 7.0),
        len(rows) / float(BLOCK_COUNT * BLOCK_INTERVALS),
        known_fraction,
        price_min,
        price_max,
        price_max - price_min,
        sum(loads) * dt_h,
        sum(pvs) * dt_h,
        sum(l - p for l, p in zip(loads, pvs)) * dt_h,
        _mean(load_u),
        _mean(pv_u),
    ]

    for block in range(BLOCK_COUNT):
        chunk = rows[block * BLOCK_INTERVALS : (block + 1) * BLOCK_INTERVALS]
        if not chunk:
            out.extend([0.0] * len(BLOCK_FEATURES))
            continue
        bl = [float(r.get("load_kw") or 0.0) for r in chunk]
        bp = [float(r.get("pv_kw") or 0.0) for r in chunk]
        bu = [
            float(r.get("load_uncertainty_kw") or 0.0)
            + float(r.get("pv_uncertainty_kw") or 0.0)
            for r in chunk
        ]
        bknown = [
            float(r["price_ore_kwh"])
            for r in chunk
            if bool(r.get("price_known")) and r.get("price_ore_kwh") is not None
        ]
        out.extend([
            _mean(bl),
            _mean(bp),
            _mean([l - p for l, p in zip(bl, bp)]),
            _mean(bu),
            _mean(bknown),
            len(bknown) / float(len(chunk)),
        ])

    if len(out) != len(FEATURE_NAMES):
        raise RuntimeError(f"feature vector length mismatch: {len(out)} != {len(FEATURE_NAMES)}")
    return [float(x) for x in out]


def feature_metadata() -> dict[str, Any]:
    return {
        "schema": FEATURE_SCHEMA,
        "feature_count": len(FEATURE_NAMES),
        "block_interval_count": BLOCK_INTERVALS,
        "block_count": BLOCK_COUNT,
        "block_hours": BLOCK_INTERVALS * 0.25,
        "maximum_horizon_hours": BLOCK_COUNT * BLOCK_INTERVALS * 0.25,
        "feature_names": list(FEATURE_NAMES),
    }
