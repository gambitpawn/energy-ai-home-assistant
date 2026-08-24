from __future__ import annotations
from typing import Any
from pydantic import BaseModel, Field

class StateValue(BaseModel):
    entity_id: str | None = None
    state: Any = None
    available: bool = False
    last_updated: str | None = None

class EnergyState(BaseModel):
    collected_at: str
    pv_power_kw: StateValue
    house_load_kw: StateValue
    grid_power_kw: StateValue
    battery_power_kw: StateValue
    battery_soc_pct: StateValue
    spot_price_ore_kwh: StateValue
    sauna_reserve: StateValue
    sauna_reserve_until: StateValue
    ev_mode: StateValue
    ev_connected: StateValue
    ev_soc_pct: StateValue
    ev_target_soc_pct: StateValue
    ev_ready_by: StateValue
    ev_power_kw: StateValue
    demand_tariff_enabled: StateValue
    import_power_target_kw: StateValue
    export_power_target_kw: StateValue

class ExplainRequest(BaseModel):
    proposed_action: dict[str, Any] = Field(default_factory=dict)
    reason_data: dict[str, Any] = Field(default_factory=dict)
    include_current_state: bool = True

class ExplainResponse(BaseModel):
    explanation_sv: str
    model: str
