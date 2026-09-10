from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from app import selected_engine_authority as sea


def _base_plan() -> dict:
    return {
        "generated_at": "2026-09-10T10:30:00+00:00",
        "planner": "deterministic_battery_dp_v3_5",
        "mode": "shadow",
        "initial_soc_pct": 70.0,
        "rows": [
            {
                "start": "2026-09-10T10:45:00+00:00",
                "load_kw": 1.0,
                "pv_kw": 5.0,
                "price_ore_kwh": 40.0,
                "battery_action_kw": 0.0,
                "expected_soc_pct": 70.0,
                "grid_import_kw": 0.0,
                "grid_export_kw": 4.0,
            },
            {
                "start": "2026-09-10T11:00:00+00:00",
                "load_kw": 1.0,
                "pv_kw": 4.0,
                "price_ore_kwh": 45.0,
                "battery_action_kw": 0.0,
                "expected_soc_pct": 70.0,
                "grid_import_kw": 0.0,
                "grid_export_kw": 3.0,
            },
        ],
        "summary": {},
    }


def test_control_plan_uses_routed_engine_horizon_not_baseline_action() -> None:
    routing = {
        "routed_engine_id": "adaptive_deterministic_v1",
        "decision_id": "adaptive-decision",
        "selection_mode": "manual",
        "fallback_used": False,
    }
    decision = {
        "plan_rows": [
            {
                "start": "2026-09-10T10:45:00+00:00",
                "requested_action_kw": -3.0,
                "expected_soc_pct": 73.6,
            },
            {
                "start": "2026-09-10T11:00:00+00:00",
                "requested_action_kw": -2.0,
                "expected_soc_pct": 76.0,
            },
        ]
    }

    result = sea.merge_selected_engine_plan(_base_plan(), routing, decision)

    assert result["planner"] == "adaptive_deterministic_v1"
    assert result["control_plan_source"] == "routed_engine_decision"
    assert result["rows"][0]["battery_action_kw"] == pytest.approx(-3.0)
    assert result["rows"][0]["expected_soc_pct"] == pytest.approx(73.6)
    assert result["rows"][0]["grid_export_kw"] == pytest.approx(1.0)
    assert result["rows"][0]["reason"] == "pv_charge"


def test_control_plan_falls_back_explicitly_when_routed_horizon_missing() -> None:
    base = _base_plan()
    result = sea.merge_selected_engine_plan(base, None, None)
    assert result["planner"] == "deterministic_battery_dp_v3_5"
    assert result["control_plan_source"] == "baseline_fallback"
    assert result["rows"][0]["battery_action_kw"] == 0.0


@pytest.mark.asyncio
async def test_soc_replan_uses_selected_engine_pipeline_instead_of_legacy_v36(monkeypatch) -> None:
    calls = {"pipeline": 0, "legacy": 0}

    async def legacy(reason: str):
        calls["legacy"] += 1
        return {"planner": "deterministic_v36_live"}

    async def pipeline():
        calls["pipeline"] += 1
        return {
            "model_selector": {
                "routed_engine_id": "adaptive_deterministic_v1",
                "selection_mode": "manual",
                "fallback_used": False,
                "decision_start": "2026-09-10T10:45:00+00:00",
            },
            "actuator": {"status": "acknowledged"},
        }

    async def refresh():
        return await pipeline()

    fake_base = SimpleNamespace(
        run_live_replan=legacy,
        _OPTIMIZER_REFRESH_LOCK=asyncio.Lock(),
        _refresh_optimizer_pipeline_unlocked=pipeline,
        _REPLAN_STATE={},
        core=SimpleNamespace(cfg={}),
        refresh_optimizer_plan=refresh,
    )
    ui = SimpleNamespace(CURRENT_UI_EXTENSION="")
    monkeypatch.setattr(sea, "latest_plan", lambda limit=500: _base_plan())

    sea.install_selected_engine_authority(app=FastAPI(), base=fake_base, runtime_ui_module=ui)
    result = await fake_base.run_live_replan("soc_deviation")

    assert calls["legacy"] == 0
    assert calls["pipeline"] == 1
    assert result["planner"] == "adaptive_deterministic_v1"
    assert result["selected_engine_authority"] is True
    assert fake_base._REPLAN_STATE["last_selected_engine_replan"]["routed_engine_id"] == "adaptive_deterministic_v1"
