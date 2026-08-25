from __future__ import annotations

from datetime import datetime, timezone

from app.optimizer_evaluation import _apply_action, _baseline_interval, _hindsight


def _cfg():
    return {
        "policy": {
            "battery": {
                "capacity_kwh": 19.6,
                "hard_min_soc_pct": 5.0,
                "hard_max_soc_pct": 100.0,
                "preferred_min_soc_pct": 15.0,
                "preferred_max_soc_pct": 90.0,
                "normal_reserve_soc_pct": 20.0,
            },
            "economics": {
                "import_overhead_ore_kwh": 0.0,
                "export_overhead_ore_kwh": 0.0,
                "minimum_arbitrage_margin_ore_kwh": 20.0,
            },
        },
        "optimizer": {
            "battery_charge_efficiency": 0.95,
            "battery_discharge_efficiency": 0.95,
            "battery_max_charge_kw": 8.0,
            "battery_max_discharge_kw": 8.0,
            "battery_degradation_ore_kwh": 5.0,
            "physical_grid_import_limit_kw": 13.8,
            "grid_export_limit_kw": 10.0,
            "reserve_critical_soc_pct": 10.0,
            "reserve_critical_penalty_ore_per_kwh_hour": 300.0,
            "reserve_preferred_penalty_ore_per_kwh_hour": 100.0,
            "reserve_target_penalty_ore_per_kwh_hour": 10.0,
            "preferred_max_excess_penalty_ore_per_kwh_hour": 2.0,
        },
        "tariffs": {"enabled": False},
    }


def _row(hour: int, price: float, load: float = 1.0, pv: float = 0.0):
    return {
        "start": datetime(2026, 1, 1, hour, 0, tzinfo=timezone.utc).isoformat(),
        "load_kw": load,
        "pv_kw": pv,
        "price_ore_kwh": price,
    }


def test_realized_action_is_clamped_at_hard_min_soc():
    cfg = _cfg()
    cap = cfg["policy"]["battery"]["capacity_kwh"]
    hard_min_energy = cap * cfg["policy"]["battery"]["hard_min_soc_pct"] / 100.0
    result = _apply_action(_row(0, 200.0), 8.0, hard_min_energy, cfg, 20.0)
    assert result["clamped"] is True
    assert abs(float(result["applied_action_kw"])) < 1e-9
    assert abs(float(result["soc_end_pct"]) - 5.0) < 1e-9


def test_hindsight_preserves_requested_terminal_soc():
    cfg = _cfg()
    rows = [_row(0, 50.0), _row(1, 50.0), _row(2, 300.0), _row(3, 300.0)]
    result = _hindsight(rows, cfg, 50.0, 50.0)
    assert result["status"] in {"optimal", "feasible"}
    assert abs(float(result["terminal_soc_pct"]) - 50.0) < 1e-9


def test_hindsight_can_beat_zero_battery_cash_cost_on_clear_spread():
    cfg = _cfg()
    rows = [_row(0, 20.0), _row(1, 20.0), _row(2, 400.0), _row(3, 400.0)]
    baseline = sum(float(_baseline_interval(row, cfg)["cash_cost_ore"]) for row in rows)
    result = _hindsight(rows, cfg, 50.0, 50.0)
    assert result["status"] in {"optimal", "feasible"}
    assert float(result["cash_cost_ore"]) < baseline
