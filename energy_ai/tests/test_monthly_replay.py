from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.monthly_replay import _energy_prices, _solve

TZ=ZoneInfo("Europe/Stockholm")
CFG={
    "policy":{"battery":{"capacity_kwh":19.6,"hard_min_soc_pct":5,"hard_max_soc_pct":100,"preferred_min_soc_pct":15,"preferred_max_soc_pct":90,"normal_reserve_soc_pct":20,"high_uncertainty_reserve_soc_pct":28},"economics":{"import_overhead_ore_kwh":0,"export_overhead_ore_kwh":0,"minimum_arbitrage_margin_ore_kwh":20}},
    "optimizer":{"planner":"deterministic_battery_dp_v3_5","battery_max_charge_kw":8,"battery_max_discharge_kw":8,"battery_charge_efficiency":.95,"battery_discharge_efficiency":.95,"battery_degradation_ore_kwh":5,"physical_grid_import_limit_kw":13.8,"grid_export_limit_kw":10,"reserve_critical_soc_pct":10,"reserve_critical_penalty_ore_per_kwh_hour":300,"reserve_preferred_penalty_ore_per_kwh_hour":100,"reserve_target_penalty_ore_per_kwh_hour":10,"preferred_max_excess_penalty_ore_per_kwh_hour":2},
    "tariffs":{"test_scenarios":{"consumption_demand":{"kind":"import_top3_mean","rate_sek_per_kw":105.0,"start_hour":7,"end_hour":19,"active_months":[1,2,11,12],"day_rule":"workdays","top_n":3,"measurement":"clock_hour_average_import_kw"}}}
}


def day_rows():
    start=datetime(2026,1,5,tzinfo=TZ)
    return [{"start":(start+timedelta(minutes=15*i)).astimezone(ZoneInfo("UTC")).isoformat(),"load_kw":.25,"pv_kw":0.0,"price_ore_kwh":100.0} for i in range(96)]


class MonthlyReplayTests(unittest.TestCase):
    def test_tariff_optimizer_can_prefer_zero_import(self):
        rows=day_rows(); base=_solve(rows,CFG,tariff_enabled=False,initial_soc_pct=50); tariff=_solve(rows,CFG,tariff_enabled=True,initial_soc_pct=50)
        self.assertEqual("optimal",base["status"]); self.assertEqual("optimal",tariff["status"])
        self.assertGreater(base["tariff"]["metric_kw"],0.2)
        self.assertLess(tariff["tariff"]["metric_kw"],0.001)
        self.assertAlmostEqual(50.0,tariff["terminal_soc_pct"],places=2)

    def test_zero_hourly_cap_is_feasible(self):
        result=_solve(day_rows(),CFG,tariff_enabled=True,hourly_cap_kw=0.0,initial_soc_pct=50)
        self.assertEqual("optimal",result["status"]); self.assertLess(result["tariff"]["max_hour_kw"],0.001)

    def test_hourly_energy_chart_price_expands_to_quarters(self):
        t0=int(datetime(2026,1,5,tzinfo=ZoneInfo("UTC")).timestamp()); parsed=_energy_prices({"unix_seconds":[t0,t0+3600],"price":[100,120]})
        self.assertEqual(5,len(parsed)); self.assertEqual(100,parsed[min(parsed)]); self.assertIn(120,parsed.values())


if __name__=="__main__": unittest.main()
