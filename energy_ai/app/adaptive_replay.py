from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from statistics import median
from typing import Any

from .adaptive_deterministic import AdaptiveParameters, solve_adaptive_from_rows
from .historical_closed_loop import _canonical, _information_vintages, _slice_horizon, _vintage_for_interval
from .optimizer import DT_HOURS
from .optimizer_evaluation import _actual_rows, _apply_action, _day_bounds

MIN_ACTUAL_COVERAGE = 0.98
MIN_INFORMATION_COVERAGE = 0.98
INVALID_SCORE_ORE = 1.0e12


@dataclass
class DailyReplayEvaluator:
    cfg: dict[str, Any]
    local_date: date

    def __post_init__(self) -> None:
        tariffs = self.cfg.get("tariffs") or {}
        if bool(tariffs.get("enabled")) and any(
            bool((tariffs.get(name) or {}).get("enabled"))
            for name in ("consumption_demand", "production_demand")
        ):
            raise RuntimeError("adaptive deterministic v1 daily learner does not yet support active demand tariffs")

        self.start, self.end = _day_bounds(self.local_date)
        self.rows, self.data = _actual_rows(self.local_date)
        expected = int(self.data.get("expected_intervals") or 0)
        coverage = float(self.data.get("actual_coverage_fraction") or 0.0)
        if expected <= 0 or coverage < MIN_ACTUAL_COVERAGE or len(self.rows) != expected:
            raise RuntimeError(
                f"insufficient actual coverage for {self.local_date}: "
                f"{len(self.rows)}/{expected} ({coverage:.3f})"
            )
        if not self.rows:
            raise RuntimeError(f"no actual rows for {self.local_date}")
        expected_first = self.start.isoformat()
        expected_last = (self.end - __import__('datetime').timedelta(minutes=15)).isoformat()
        if self.data.get("first") != expected_first or self.data.get("last") != expected_last:
            raise RuntimeError("actual replay day is not boundary-complete")

        first_soc = next((r.get("battery_soc_start_pct") for r in self.rows if r.get("battery_soc_start_pct") is not None), None)
        if first_soc is None:
            raise RuntimeError("missing initial SOC for adaptive replay")
        self.initial_soc_pct = float(first_soc)

        vintages = _information_vintages(self.start, self.end)
        self.vintage_map: dict[Any, dict[str, Any]] = {}
        for row in self.rows:
            stamp = _canonical(row["start"])
            vintage = _vintage_for_interval(stamp, vintages)
            if vintage is not None:
                self.vintage_map[stamp] = vintage
        info_coverage = len(self.vintage_map) / max(1, expected)
        if info_coverage < MIN_INFORMATION_COVERAGE or len(self.vintage_map) != expected:
            raise RuntimeError(
                f"insufficient information-vintage coverage for {self.local_date}: "
                f"{len(self.vintage_map)}/{expected} ({info_coverage:.3f})"
            )

        econ = (self.cfg.get("policy") or {}).get("economics") or {}
        buys = [
            float(r["price_ore_kwh"]) + float(econ.get("import_overhead_ore_kwh", 0.0))
            for r in self.rows
        ]
        self.reference_price_ore_kwh = float(median(buys))
        battery = (self.cfg.get("policy") or {}).get("battery") or {}
        self.capacity_kwh = float(battery.get("capacity_kwh", 19.6))
        self.initial_energy_kwh = self.capacity_kwh * self.initial_soc_pct / 100.0
        self._score_cache: dict[tuple[float, ...], float] = {}
        self._details_cache: dict[tuple[float, ...], dict[str, Any]] = {}

    @staticmethod
    def _key(params: AdaptiveParameters) -> tuple[float, ...]:
        p = params.bounded()
        return (
            p.pv_forecast_risk,
            p.load_forecast_risk,
            p.terminal_energy_value_ore_kwh,
            p.discharge_hurdle_ore_kwh,
            p.reserve_energy_value_ore_kwh,
            p.charge_hurdle_ore_kwh,
            p.cycling_penalty_ore_kwh,
        )

    def __call__(self, params: AdaptiveParameters) -> float:
        return self.evaluate(params)["score_ore"]

    def evaluate(self, params: AdaptiveParameters) -> dict[str, Any]:
        key = self._key(params)
        if key in self._details_cache:
            return dict(self._details_cache[key])

        energy = self.initial_energy_kwh
        cash_cost = 0.0
        throughput = 0.0
        grid_import_kwh = 0.0
        grid_export_kwh = 0.0
        clamps = 0
        solve_failures = 0

        for actual in self.rows:
            stamp = _canonical(actual["start"])
            vintage = self.vintage_map[stamp]
            horizon = _slice_horizon(vintage["payload"], stamp)
            if not horizon:
                solve_failures += 1
                break
            try:
                current_soc = energy / self.capacity_kwh * 100.0
                solved = solve_adaptive_from_rows(self.cfg, horizon, current_soc, params)
                requested = float(solved["first_action_kw"])
                applied = _apply_action(actual, requested, energy, self.cfg, None)
            except Exception:
                solve_failures += 1
                break

            energy = float(applied["energy_end_kwh"])
            cash_cost += float(applied["cash_cost_ore"])
            throughput += float(applied["throughput_kwh"])
            grid_import_kwh += float(applied["grid_import_kw"]) * DT_HOURS
            grid_export_kwh += float(applied["grid_export_kw"]) * DT_HOURS
            clamps += int(bool(applied["clamped"]))

        if solve_failures:
            score = INVALID_SCORE_ORE + solve_failures * 1.0e9
        else:
            terminal_asset_ore = (energy - self.initial_energy_kwh) * self.reference_price_ore_kwh
            score = cash_cost - terminal_asset_ore

        details = {
            "score_ore": float(score),
            "cash_cost_ore": round(cash_cost, 6),
            "terminal_soc_pct": round(energy / self.capacity_kwh * 100.0, 4),
            "terminal_asset_adjustment_ore": round((energy - self.initial_energy_kwh) * self.reference_price_ore_kwh, 6),
            "reference_price_ore_kwh": round(self.reference_price_ore_kwh, 6),
            "throughput_kwh": round(throughput, 6),
            "grid_import_kwh": round(grid_import_kwh, 6),
            "grid_export_kwh": round(grid_export_kwh, 6),
            "clamps": clamps,
            "solve_failures": solve_failures,
            "intervals": len(self.rows),
            "actual_coverage_fraction": float(self.data.get("actual_coverage_fraction") or 0.0),
            "information_coverage_fraction": len(self.vintage_map) / max(1, int(self.data.get("expected_intervals") or 1)),
            "objective_semantics": "realized_energy_plus_fixed_degradation_minus_terminal_asset_change",
        }
        self._score_cache[key] = float(score)
        self._details_cache[key] = details
        return dict(details)

    def diagnostics(self, params: AdaptiveParameters) -> dict[str, Any]:
        return self.evaluate(params)


def build_daily_evaluator(cfg: dict[str, Any], replay_date: str) -> DailyReplayEvaluator:
    return DailyReplayEvaluator(cfg, date.fromisoformat(replay_date))
