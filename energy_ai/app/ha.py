from __future__ import annotations
from datetime import datetime, timezone
from typing import Any
import os

import httpx

from .models import EnergyState, StateValue


class HomeAssistantClient:
    def __init__(self, cfg: dict[str, Any]):
        self.base_url = "http://supervisor/core/api"
        self.token = os.getenv("SUPERVISOR_TOKEN")
        self.entities = cfg.get("entities", {})
        self.timeout = 10.0

    @property
    def authenticated(self) -> bool:
        return bool(self.token)

    async def _get_entity(self, client: httpx.AsyncClient, entity_id: str | None) -> StateValue:
        if not entity_id:
            return StateValue(entity_id=None, available=False)

        if not self.token:
            return StateValue(entity_id=entity_id, available=False, state=None)

        try:
            response = await client.get(
                f"{self.base_url}/states/{entity_id}",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
                timeout=self.timeout,
            )
            if response.status_code == 404:
                return StateValue(entity_id=entity_id, available=False, state=None)
            response.raise_for_status()
            data = response.json()
            raw = data.get("state")
            available = raw not in (None, "unknown", "unavailable", "")
            return StateValue(
                entity_id=entity_id,
                state=raw if available else None,
                available=available,
                last_updated=data.get("last_updated"),
            )
        except Exception:
            return StateValue(entity_id=entity_id, available=False, state=None)

    async def snapshot(self) -> EnergyState:
        keys = {
            "pv_power_kw": "pv_power",
            "house_load_kw": "house_load",
            "grid_power_kw": "grid_power",
            "battery_power_kw": "battery_power",
            "battery_soc_pct": "battery_soc",
            "spot_price_ore_kwh": "spot_price",
            "sauna_reserve": "sauna_reserve",
            "sauna_reserve_until": "sauna_reserve_until",
            "ev_mode": "ev_mode",
            "ev_connected": "ev_connected",
            "ev_soc_pct": "ev_soc",
            "ev_target_soc_pct": "ev_target_soc",
            "ev_ready_by": "ev_ready_by",
            "ev_power_kw": "ev_power",
            "demand_tariff_enabled": "demand_tariff_enabled",
            "import_power_target_kw": "import_power_target_kw",
            "export_power_target_kw": "export_power_target_kw",
        }

        async with httpx.AsyncClient() as client:
            values = {}
            for output_key, config_key in keys.items():
                values[output_key] = await self._get_entity(
                    client,
                    self.entities.get(config_key),
                )

        return EnergyState(
            collected_at=datetime.now(timezone.utc).isoformat(),
            **values,
        )
