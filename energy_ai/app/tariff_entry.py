from __future__ import annotations

import asyncio

from fastapi import HTTPException, Query

from . import main as core
from .tariff_scenarios import DEFAULT_TEMPLATES, ENGINE_NAME, run_edge_cases, run_live_scenario

RUNTIME_BUILD = "1.0.49"
core.RUNTIME_VERSION = RUNTIME_BUILD
core.cfg["runtime_build"] = RUNTIME_BUILD
core.app.version = RUNTIME_BUILD
app = core.app


def _peaks(raw: str | None) -> list[float]:
    if not raw:
        return []
    try:
        return [float(x.strip()) for x in raw.split(",") if x.strip()]
    except Exception as exc:
        raise HTTPException(400, f"historical_peaks_kw must be comma-separated numbers: {exc!r}")


@app.get("/optimizer/tariff-test/config", tags=["tariff-test"])
async def tariff_test_config():
    return {
        "runtime_build": RUNTIME_BUILD,
        "engine": ENGINE_NAME,
        "test_only": True,
        "base_planner_unchanged": (core.cfg.get("optimizer") or {}).get("planner"),
        "templates": DEFAULT_TEMPLATES,
        "notes": {
            "consumption": "105 SEK/kW; mean of top three clock-hour import values; current requested test window 07-19.",
            "production": "10 SEK/kW preliminary test assumption; max clock-hour export; prior rule itself remains unverified.",
            "force_window": "When true, ignores month/day eligibility but keeps the tariff clock-hour window. Useful for counterfactual testing on today's forecast.",
        },
    }


@app.get("/optimizer/tariff-test/consumption", tags=["tariff-test"])
async def tariff_test_consumption(
    force_window: bool = Query(True),
    historical_peaks_kw: str | None = Query(None, description="Optional month-to-date hourly import peaks, comma-separated kW"),
):
    try:
        return await asyncio.to_thread(
            run_live_scenario,
            core.cfg,
            "consumption_demand",
            force_window=force_window,
            historical_peaks_kw=_peaks(historical_peaks_kw),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Consumption tariff shadow test failed: {exc!r}")


@app.get("/optimizer/tariff-test/production", tags=["tariff-test"])
async def tariff_test_production(
    force_window: bool = Query(True),
    historical_peaks_kw: str | None = Query(None, description="Optional prior export peak(s), comma-separated kW"),
):
    try:
        return await asyncio.to_thread(
            run_live_scenario,
            core.cfg,
            "production_demand",
            force_window=force_window,
            historical_peaks_kw=_peaks(historical_peaks_kw),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Production tariff shadow test failed: {exc!r}")


@app.get("/optimizer/tariff-test/edge-cases", tags=["tariff-test"])
async def tariff_test_edge_cases():
    try:
        return await asyncio.to_thread(run_edge_cases, core.cfg)
    except Exception as exc:
        raise HTTPException(500, f"Tariff edge-case tests failed: {exc!r}")
