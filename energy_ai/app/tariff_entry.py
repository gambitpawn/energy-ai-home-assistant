from __future__ import annotations

import asyncio

from fastapi import HTTPException, Query

from . import main as core
from .monthly_replay import ENGINE_NAME as MONTHLY_ENGINE_NAME, replay_status, run_month_replay, run_winter_replay
from .tariff_scenarios import DEFAULT_TEMPLATES, ENGINE_NAME, run_edge_cases, run_live_scenario

RUNTIME_BUILD = "1.0.50"
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


def _floats(raw: str | None) -> list[float] | None:
    if not raw:
        return None
    try:
        return [float(x.strip()) for x in raw.split(",") if x.strip()]
    except Exception as exc:
        raise HTTPException(400, f"fixed_caps_kw must be comma-separated numbers: {exc!r}")


def _months(raw: str) -> list[str]:
    months = [x.strip() for x in raw.split(",") if x.strip()]
    if not months:
        raise HTTPException(400, "months must contain at least one YYYY-MM value")
    return months


@app.get("/optimizer/tariff-test/config", tags=["tariff-test"])
async def tariff_test_config():
    return {
        "runtime_build": RUNTIME_BUILD,
        "engine": ENGINE_NAME,
        "monthly_replay_engine": MONTHLY_ENGINE_NAME,
        "test_only": True,
        "base_planner_unchanged": (core.cfg.get("optimizer") or {}).get("planner"),
        "templates": DEFAULT_TEMPLATES,
        "notes": {
            "consumption": "105 SEK/kW; mean of top three clock-hour import values; current requested test window 07-19.",
            "production": "10 SEK/kW preliminary test assumption; max clock-hour export; prior rule itself remains unverified.",
            "force_window": "When true, ignores month/day eligibility but keeps the tariff clock-hour window. Useful for counterfactual testing on today's forecast.",
            "monthly_replay": "Full-month perfect-hindsight benchmark. It does not impose a 0 kW target unless running the explicit fixed-cap benchmark.",
        },
    }


@app.get("/optimizer/tariff-test/consumption", tags=["tariff-test"])
async def tariff_test_consumption(
    force_window: bool = Query(True),
    historical_peaks_kw: str | None = Query(None, description="Optional month-to-date hourly import peaks, comma-separated kW"),
):
    try:
        return await asyncio.to_thread(run_live_scenario, core.cfg, "consumption_demand", force_window=force_window, historical_peaks_kw=_peaks(historical_peaks_kw))
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
        return await asyncio.to_thread(run_live_scenario, core.cfg, "production_demand", force_window=force_window, historical_peaks_kw=_peaks(historical_peaks_kw))
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


@app.get("/optimizer/tariff-replay/status", tags=["tariff-replay"])
async def tariff_replay_status():
    try:
        return await asyncio.to_thread(replay_status)
    except Exception as exc:
        raise HTTPException(500, f"Tariff replay status failed: {exc!r}")


@app.get("/optimizer/tariff-replay/month", tags=["tariff-replay"])
async def tariff_replay_month(
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    refresh_market: bool = Query(False),
    initial_soc_pct: float = Query(50.0, ge=5.0, le=100.0),
    fixed_caps_kw: str | None = Query(None, description="Optional hourly-cap benchmarks, e.g. 0,0.5,0.75,1,1.5,2"),
):
    try:
        return await run_month_replay(core.cfg, month, refresh_market=refresh_market, initial_soc_pct=initial_soc_pct, fixed_caps_kw=_floats(fixed_caps_kw))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Monthly tariff replay failed: {exc!r}")


@app.get("/optimizer/tariff-replay/winter", tags=["tariff-replay"])
async def tariff_replay_winter(
    months: str = Query("2026-01,2026-02"),
    refresh_market: bool = Query(False),
    initial_soc_pct: float = Query(50.0, ge=5.0, le=100.0),
):
    try:
        return await run_winter_replay(core.cfg, _months(months), refresh_market=refresh_market, initial_soc_pct=initial_soc_pct)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Winter tariff replay failed: {exc!r}")
