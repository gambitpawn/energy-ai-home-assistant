from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable


BATTERY_HEALTH_COST_VERSION = "battery_health_cost_v1"


@dataclass(frozen=True)
class BatteryHealthParameters:
    """Canonical battery-health economics shared by planners and evaluation.

    The values are deliberately external policy/economic assumptions. They are
    not intended to be learned by a control model against the same objective.

    High-SOC costs are marginal occupancy costs. For example, energy between
    95% and 98% SOC is charged at ``high_soc_zone_2_cost_ore_per_kwh_hour`` in
    addition to the already-filled 90-95% zone.
    """

    cycle_wear_ore_per_kwh: float = 5.0
    high_soc_enabled: bool = True
    high_soc_threshold_1_pct: float = 90.0
    high_soc_threshold_2_pct: float = 95.0
    high_soc_threshold_3_pct: float = 98.0
    high_soc_zone_1_cost_ore_per_kwh_hour: float = 5.0
    high_soc_zone_2_cost_ore_per_kwh_hour: float = 15.0
    high_soc_zone_3_cost_ore_per_kwh_hour: float = 50.0

    def validated(self) -> "BatteryHealthParameters":
        thresholds = (
            float(self.high_soc_threshold_1_pct),
            float(self.high_soc_threshold_2_pct),
            float(self.high_soc_threshold_3_pct),
        )
        if not (0.0 <= thresholds[0] < thresholds[1] < thresholds[2] < 100.0):
            raise ValueError("high-SOC thresholds must satisfy 0 <= t1 < t2 < t3 < 100")
        costs = (
            float(self.high_soc_zone_1_cost_ore_per_kwh_hour),
            float(self.high_soc_zone_2_cost_ore_per_kwh_hour),
            float(self.high_soc_zone_3_cost_ore_per_kwh_hour),
        )
        if float(self.cycle_wear_ore_per_kwh) < 0.0 or any(x < 0.0 for x in costs):
            raise ValueError("battery-health cost parameters must be non-negative")
        if not (costs[0] <= costs[1] <= costs[2]):
            raise ValueError("high-SOC marginal costs must be non-decreasing")
        return self

    def as_dict(self) -> dict[str, Any]:
        self.validated()
        return asdict(self)


DEFAULT_BATTERY_HEALTH_PARAMETERS = BatteryHealthParameters()


def _clamp_energy(energy_kwh: float, capacity_kwh: float) -> float:
    return min(float(capacity_kwh), max(0.0, float(energy_kwh)))


def _zone_energy_kwh(energy_kwh: float, lower_kwh: float, upper_kwh: float) -> float:
    """Energy occupying one SOC zone at one instant."""
    if upper_kwh <= lower_kwh:
        return 0.0
    return max(0.0, min(float(energy_kwh), upper_kwh) - lower_kwh)


def _linear_path_integral(
    fn: Callable[[float], float],
    energy_start_kwh: float,
    energy_end_kwh: float,
    interval_hours: float,
    breakpoints_kwh: tuple[float, ...],
) -> float:
    """Integrate a piecewise-linear state cost along a linear energy transition.

    The battery transition is treated as linear within the optimizer interval.
    Splitting the trajectory at every SOC-zone boundary makes trapezoidal
    integration exact for the piecewise-linear occupancy function, including
    transitions that cross several zones in one interval.
    """
    duration = float(interval_hours)
    if duration < 0.0:
        raise ValueError("interval_hours must be non-negative")
    if duration == 0.0:
        return 0.0

    e0 = float(energy_start_kwh)
    e1 = float(energy_end_kwh)
    delta = e1 - e0
    if abs(delta) <= 1e-12:
        return fn(e0) * duration

    fractions = [0.0, 1.0]
    lo, hi = sorted((e0, e1))
    for boundary in breakpoints_kwh:
        b = float(boundary)
        if lo + 1e-12 < b < hi - 1e-12:
            u = (b - e0) / delta
            if 0.0 < u < 1.0:
                fractions.append(u)
    fractions = sorted(set(fractions))

    integral = 0.0
    for u0, u1 in zip(fractions, fractions[1:]):
        ea = e0 + delta * u0
        eb = e0 + delta * u1
        integral += duration * (u1 - u0) * (fn(ea) + fn(eb)) / 2.0
    return integral


def battery_health_cost(
    *,
    energy_start_kwh: float,
    energy_end_kwh: float,
    capacity_kwh: float,
    interval_hours: float,
    parameters: BatteryHealthParameters = DEFAULT_BATTERY_HEALTH_PARAMETERS,
) -> dict[str, Any]:
    """Return canonical cycling + high-SOC occupancy cost for one interval.

    Units:
    - input energy: kWh stored in the battery;
    - interval: hours;
    - cycling parameter: ore per internal kWh moved;
    - high-SOC parameters: ore per occupied kWh per hour;
    - returned costs: ore.

    High-SOC occupancy is integrated exactly along the assumed linear movement
    from ``energy_start_kwh`` to ``energy_end_kwh``. Consequently, charging to
    100% late is cheaper than holding 100% for many hours, even if both plans
    eventually reach the same SOC.
    """
    p = parameters.validated()
    cap = float(capacity_kwh)
    duration = float(interval_hours)
    if cap <= 0.0:
        raise ValueError("capacity_kwh must be > 0")
    if duration < 0.0:
        raise ValueError("interval_hours must be non-negative")

    e0 = _clamp_energy(energy_start_kwh, cap)
    e1 = _clamp_energy(energy_end_kwh, cap)
    t1 = cap * float(p.high_soc_threshold_1_pct) / 100.0
    t2 = cap * float(p.high_soc_threshold_2_pct) / 100.0
    t3 = cap * float(p.high_soc_threshold_3_pct) / 100.0
    boundaries = (t1, t2, t3, cap)

    throughput_kwh = abs(e1 - e0)
    cycle_cost = throughput_kwh * float(p.cycle_wear_ore_per_kwh)

    zone_specs = (
        ("zone_1", t1, t2, float(p.high_soc_zone_1_cost_ore_per_kwh_hour)),
        ("zone_2", t2, t3, float(p.high_soc_zone_2_cost_ore_per_kwh_hour)),
        ("zone_3", t3, cap, float(p.high_soc_zone_3_cost_ore_per_kwh_hour)),
    )
    zone_energy_hours: dict[str, float] = {}
    zone_costs: dict[str, float] = {}
    for name, lower, upper, rate in zone_specs:
        if not p.high_soc_enabled:
            energy_hours = 0.0
        else:
            energy_hours = _linear_path_integral(
                lambda e, lo=lower, hi=upper: _zone_energy_kwh(e, lo, hi),
                e0,
                e1,
                duration,
                boundaries,
            )
        zone_energy_hours[name] = energy_hours
        zone_costs[name] = energy_hours * rate

    high_soc_cost = sum(zone_costs.values())
    total = cycle_cost + high_soc_cost
    return {
        "version": BATTERY_HEALTH_COST_VERSION,
        "capacity_kwh": cap,
        "interval_hours": duration,
        "energy_start_kwh": e0,
        "energy_end_kwh": e1,
        "soc_start_pct": e0 / cap * 100.0,
        "soc_end_pct": e1 / cap * 100.0,
        "internal_throughput_kwh": throughput_kwh,
        "cycle_wear_cost_ore": cycle_cost,
        "high_soc_enabled": bool(p.high_soc_enabled),
        "high_soc_zone_1_energy_hours": zone_energy_hours["zone_1"],
        "high_soc_zone_2_energy_hours": zone_energy_hours["zone_2"],
        "high_soc_zone_3_energy_hours": zone_energy_hours["zone_3"],
        "high_soc_zone_1_cost_ore": zone_costs["zone_1"],
        "high_soc_zone_2_cost_ore": zone_costs["zone_2"],
        "high_soc_zone_3_cost_ore": zone_costs["zone_3"],
        "high_soc_occupancy_cost_ore": high_soc_cost,
        "total_battery_health_cost_ore": total,
        "parameters": p.as_dict(),
    }
