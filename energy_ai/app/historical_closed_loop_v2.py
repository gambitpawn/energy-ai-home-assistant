from __future__ import annotations

from typing import Any

from . import historical_closed_loop as _v1
from .app_comparison import _actual_rows, resolve_window
from .optimizer import DT_HOURS

ENGINE_NAME = "historical_closed_loop_v35_v2"
GRID_SIGN_CONVENTION = "raw Solinteg meter: positive export, negative import; evaluator converts to positive import"
ENERGY_BALANCE_ABS_FLOOR_KWH = 0.50
ENERGY_BALANCE_REL_TOLERANCE = 0.03
ENERGY_BALANCE_INTERVAL_MAE_TOLERANCE_KW = 0.50


def _actual_interval_solinteg(row: dict[str, Any], cfg: dict[str, Any]) -> dict[str, float]:
    """Actual economics using the observed Solinteg meter sign convention.

    Stored grid_power_kw is positive for export and negative for import, whereas
    the optimizer's internal grid convention is positive import. The sign change
    is intentionally localized to evaluation; persisted raw/state data is untouched.
    """
    econ = (cfg.get("policy") or {}).get("economics") or {}
    opt = cfg.get("optimizer") or {}
    raw_grid = float(row["grid_power_kw"])
    batt = float(row["battery_power_kw"])
    grid_import_positive = -raw_grid
    imp = max(0.0, grid_import_positive)
    exp = max(0.0, -grid_import_positive)
    buy = float(row["price_ore_kwh"]) + float(econ.get("import_overhead_ore_kwh", 0.0))
    sell = max(0.0, float(row["price_ore_kwh"]) - float(econ.get("export_overhead_ore_kwh", 0.0)))
    energy_cost = (imp * buy - exp * sell) * DT_HOURS
    degradation = abs(batt) * DT_HOURS * float(opt.get("battery_degradation_ore_kwh", 5.0))
    return {
        "grid_import_kw": imp,
        "grid_export_kw": exp,
        "energy_cost_ore": energy_cost,
        "degradation_cost_ore": degradation,
        "cash_cost_ore": energy_cost + degradation,
        "throughput_kwh": abs(batt) * DT_HOURS,
        "charge_kwh": max(0.0, -batt) * DT_HOURS,
        "discharge_kwh": max(0.0, batt) * DT_HOURS,
    }


# historical_closed_loop imports _actual_interval by value. Override that evaluator
# hook once at module import so every v2 closed-loop solve uses the corrected actual
# convention without touching live collection, persisted data, or optimizer logic.
_v1._actual_interval = _actual_interval_solinteg


def energy_balance_diagnostics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Check load - PV = grid(import+) + battery(discharge+) on realized data."""
    residuals_kw: list[float] = []
    absolute_net_load_kwh = 0.0
    for row in rows:
        net_load = float(row["load_kw"]) - float(row["pv_kw"])
        grid_import_positive = -float(row["grid_power_kw"])
        battery_discharge_positive = float(row["battery_power_kw"])
        residuals_kw.append(net_load - (grid_import_positive + battery_discharge_positive))
        absolute_net_load_kwh += abs(net_load) * DT_HOURS

    signed_residual_kwh = sum(residuals_kw) * DT_HOURS
    absolute_residual_kwh = sum(abs(v) for v in residuals_kw) * DT_HOURS
    interval_mae_kw = sum(abs(v) for v in residuals_kw) / max(1, len(residuals_kw))
    tolerance_kwh = max(
        ENERGY_BALANCE_ABS_FLOOR_KWH,
        ENERGY_BALANCE_REL_TOLERANCE * max(1.0, absolute_net_load_kwh),
    )
    passed = (
        abs(signed_residual_kwh) <= tolerance_kwh
        and interval_mae_kw <= ENERGY_BALANCE_INTERVAL_MAE_TOLERANCE_KW
    )
    return {
        "pass": passed,
        "identity": "load - PV = grid(import+) + battery(discharge+)",
        "grid_sign_convention": GRID_SIGN_CONVENTION,
        "intervals": len(rows),
        "signed_residual_kwh": round(signed_residual_kwh, 4),
        "absolute_residual_kwh": round(absolute_residual_kwh, 4),
        "interval_mae_kw": round(interval_mae_kw, 4),
        "absolute_net_load_kwh": round(absolute_net_load_kwh, 4),
        "aggregate_residual_tolerance_kwh": round(tolerance_kwh, 4),
        "interval_mae_tolerance_kw": ENERGY_BALANCE_INTERVAL_MAE_TOLERANCE_KW,
    }


def compare_closed_loop(
    cfg: dict[str, Any],
    *,
    start: str | None = None,
    end: str | None = None,
    hours: int | None = None,
    days: int | None = None,
    min_information_coverage: float = 0.90,
    min_actual_coverage: float = 0.90,
    include_rows: bool = True,
) -> dict[str, Any]:
    result = _v1.compare_closed_loop(
        cfg,
        start=start,
        end=end,
        hours=hours,
        days=days,
        min_information_coverage=min_information_coverage,
        min_actual_coverage=min_actual_coverage,
        include_rows=include_rows,
    )
    result["engine"] = ENGINE_NAME

    a, b = resolve_window(start=start, end=end, hours=hours, days=days)
    actual_rows, _ = _actual_rows(a, b)
    balance = energy_balance_diagnostics(actual_rows)
    result["energy_balance"] = balance

    if result.get("valid_comparison") and not balance["pass"]:
        result["valid_comparison"] = False
        result["winner"] = None
        result["status"] = "energy_balance_failed"

    result.setdefault("limitations", []).append(
        "actual grid sign is normalized only inside evaluation: stored Solinteg grid_power_kw is treated as positive export / negative import; live and persisted state are unchanged"
    )
    result.setdefault("validation", {})["winner_requires_energy_balance_pass"] = True
    return result
