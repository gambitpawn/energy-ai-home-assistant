from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


POOL_CONTROL_MODES = ("observe", "simulate", "active")

POOL_POLICY_DEFAULTS = {
    "pool_optimization_enabled": False,
    "pool_control_mode": "observe",
    "pool_preferred_temp_c": 28.0,
    "pool_minimum_temp_c": 27.0,
    "pool_maximum_temp_c": 29.0,
    "pool_maximum_boost_c": 1.0,
    "pool_grid_heating_allowed": True,
    "pool_min_on_minutes": 30,
    "pool_min_off_minutes": 15,
    "pool_manual_override_minutes": 360,
    "pool_nominal_power_kw": 0.0,
}


class PoolState(BaseModel):
    """Normalized pool observations available to future optimization/control."""

    collected_at: str | None = None
    available: bool = False
    water_temp_c: float | None = None
    target_temp_c: float | None = None
    heating_active: bool | None = None
    compressor_running: bool | None = None
    electrical_power_kw: float | None = None
    ambient_temp_c: float | None = None
    flow_ok: bool | None = None
    source: str = "home_assistant"


class PoolPolicy(BaseModel):
    """User-owned constraints/preferences; learned thermal physics lives elsewhere."""

    enabled: bool = False
    control_mode: Literal["observe", "simulate", "active"] = "observe"
    preferred_temp_c: float = 28.0
    minimum_temp_c: float = 27.0
    maximum_temp_c: float = 29.0
    maximum_boost_c: float = 1.0
    grid_heating_allowed: bool = True
    min_on_minutes: int = 30
    min_off_minutes: int = 15
    manual_override_minutes: int = 360
    nominal_power_kw: float = 0.0

    @classmethod
    def from_mapping(cls, values: dict | None = None) -> "PoolPolicy":
        raw = {**POOL_POLICY_DEFAULTS, **dict(values or {})}
        minimum = float(raw["pool_minimum_temp_c"])
        preferred = float(raw["pool_preferred_temp_c"])
        maximum = float(raw["pool_maximum_temp_c"])
        boost = float(raw["pool_maximum_boost_c"])
        min_on = int(raw["pool_min_on_minutes"])
        min_off = int(raw["pool_min_off_minutes"])
        manual_override = int(raw["pool_manual_override_minutes"])
        nominal_power = float(raw["pool_nominal_power_kw"])

        if not minimum <= preferred <= maximum:
            raise ValueError("pool temperatures must satisfy minimum <= preferred <= maximum")
        if boost < 0:
            raise ValueError("pool maximum boost must be non-negative")
        if min_on < 0 or min_off < 0 or manual_override < 0:
            raise ValueError("pool timing constraints must be non-negative")
        if nominal_power < 0:
            raise ValueError("pool nominal power must be non-negative")

        return cls(
            enabled=bool(raw["pool_optimization_enabled"]),
            control_mode=str(raw["pool_control_mode"]).strip().lower(),
            preferred_temp_c=preferred,
            minimum_temp_c=minimum,
            maximum_temp_c=maximum,
            maximum_boost_c=boost,
            grid_heating_allowed=bool(raw["pool_grid_heating_allowed"]),
            min_on_minutes=min_on,
            min_off_minutes=min_off,
            manual_override_minutes=manual_override,
            nominal_power_kw=nominal_power,
        )


class PoolThermalModelState(BaseModel):
    """Persisted learned-state contract. Sprint 1 creates storage, not learning."""

    model_kind: str = "empirical_pool_thermal"
    trained_at: str
    training_window_start: str | None = None
    training_window_end: str | None = None
    base_loss_c_per_hour: float | None = None
    ambient_loss_coefficient: float | None = None
    heat_gain_c_per_kwh: float | None = None
    effective_power_kw: float | None = None
    sample_count_off: int = 0
    sample_count_on: int = 0
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    metrics: dict = Field(default_factory=dict)


class PoolPlanStep(BaseModel):
    start: str
    heating_allowed: bool
    expected_power_kw: float = 0.0
    predicted_temp_c: float | None = None
    desired_setpoint_c: float | None = None


class PoolControlIntent(BaseModel):
    mode: Literal["hold", "allow", "boost"]
    target_temp_c: float | None = None
    valid_until: str
    plan_id: str
    reason: str
