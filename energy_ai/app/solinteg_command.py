from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from .ha import HomeAssistantClient

CONTROL_MODE_DEFAULT = "EMS BattCtrl"
SAFE_MODE_DEFAULT = "General"


@dataclass(frozen=True)
class SolintegEntities:
    working_mode: str
    battery_power_target: str
    source: str

    def as_dict(self) -> dict[str, str]:
        return {
            "working_mode": self.working_mode,
            "battery_power_target": self.battery_power_target,
            "source": self.source,
        }


def _text(entity: dict[str, Any]) -> str:
    attrs = entity.get("attributes") or {}
    return f"{entity.get('entity_id','')} {attrs.get('friendly_name','')}".lower()


def _rank(entity: dict[str, Any], kind: str) -> int:
    entity_id = str(entity.get("entity_id") or "").lower()
    text = _text(entity)
    score = 0
    if "solinteg" in text:
        score += 8
    if "inverter" in text:
        score += 2
    if kind == "working_mode":
        if entity_id.startswith("select."):
            score += 5
        if "working mode" in text or "working_mode" in text:
            score += 12
        if "ems battctrl" in str((entity.get("attributes") or {}).get("options") or "").lower():
            score += 8
    elif kind == "battery_power_target":
        if entity_id.startswith("number."):
            score += 5
        for token in (
            "battery_charge_discharge_power_target",
            "charge discharge power target",
            "ems battctrl charge discharge power target",
        ):
            if token in text:
                score += 12
                break
    if entity.get("state") in (None, "unknown", "unavailable", ""):
        score -= 5
    return score


def _is_grid_import_limit_entity(entity: dict[str, Any]) -> bool:
    """Match the generic Solinteg Import Limit, never the EMS BattCtrl limit."""
    entity_id = str(entity.get("entity_id") or "").lower()
    attrs = entity.get("attributes") or {}
    friendly = str(attrs.get("friendly_name") or "").strip().lower()
    if not entity_id.startswith("number."):
        return False
    if "ems battctrl" in friendly or "ems_battctrl" in entity_id:
        return False
    exact_id = entity_id.endswith("_import_limit")
    exact_name = friendly.endswith(" import limit") and "max grid import" not in friendly
    unit = str(attrs.get("unit_of_measurement") or "").lower()
    return (exact_id or exact_name) and unit in ("kw", "")


class SolintegCommandAdapter:
    """Solinteg EMS command path through Home Assistant's entity services.

    The SolaX Modbus Solinteg plugin owns register encoding. Energy AI uses
    Home Assistant entities for both the EMS battery target and the generic
    inverter grid-import limit. The configured optimizer physical grid-import
    limit is the authoritative value while Energy AI owns control.
    """

    def __init__(self, cfg: dict[str, Any], ha: HomeAssistantClient):
        self.cfg = cfg
        self.ha = ha
        self.timeout = float((cfg.get("actuator") or {}).get("ack_timeout_seconds", 8.0))
        self.ack_tolerance_kw = float((cfg.get("actuator") or {}).get("ack_tolerance_kw", 0.10))
        self._grid_import_limit_entity: str | None = None

    async def _state(self, entity_id: str) -> dict[str, Any]:
        if not self.ha.token:
            raise RuntimeError("No Home Assistant API token is available")
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.ha.base_url}/states/{entity_id}",
                headers=self.ha._headers(),
                timeout=self.ha.timeout,
            )
            response.raise_for_status()
            return response.json()

    async def _service(self, domain: str, service: str, payload: dict[str, Any]) -> Any:
        if not self.ha.token:
            raise RuntimeError("No Home Assistant API token is available")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.ha.base_url}/services/{domain}/{service}",
                headers=self.ha._headers(),
                json=payload,
                timeout=max(10.0, self.timeout),
            )
            response.raise_for_status()
            try:
                return response.json()
            except Exception:
                return None

    async def resolve_entities(self) -> SolintegEntities:
        configured = self.cfg.get("entities") or {}
        working = str(configured.get("solinteg_working_mode") or "").strip()
        target = str(configured.get("solinteg_battery_power_target") or "").strip()
        if working and target:
            # Verify they exist before declaring the command path resolved.
            await self._state(working)
            await self._state(target)
            return SolintegEntities(working, target, "configured")

        states = await self.ha.all_states()
        working_ranked = sorted(
            ((_rank(e, "working_mode"), e) for e in states), key=lambda x: x[0], reverse=True
        )
        target_ranked = sorted(
            ((_rank(e, "battery_power_target"), e) for e in states), key=lambda x: x[0], reverse=True
        )
        working_candidates = [e for score, e in working_ranked if score >= 12]
        target_candidates = [e for score, e in target_ranked if score >= 12]
        if not working:
            if not working_candidates:
                raise RuntimeError("No Solinteg Working Mode select entity could be discovered")
            best_score = _rank(working_candidates[0], "working_mode")
            tied = [e for e in working_candidates if _rank(e, "working_mode") == best_score]
            if len(tied) != 1:
                raise RuntimeError("Multiple equally ranked Solinteg Working Mode entities; configure one explicitly")
            working = str(tied[0]["entity_id"])
        if not target:
            if not target_candidates:
                raise RuntimeError("No Solinteg EMS battery power target number entity could be discovered")
            best_score = _rank(target_candidates[0], "battery_power_target")
            tied = [e for e in target_candidates if _rank(e, "battery_power_target") == best_score]
            if len(tied) != 1:
                raise RuntimeError("Multiple equally ranked Solinteg battery power target entities; configure one explicitly")
            target = str(tied[0]["entity_id"])
        return SolintegEntities(working, target, "discovered")

    async def resolve_grid_import_limit_entity(self) -> str:
        if self._grid_import_limit_entity:
            await self._state(self._grid_import_limit_entity)
            return self._grid_import_limit_entity

        states = await self.ha.all_states()
        candidates = [e for e in states if _is_grid_import_limit_entity(e)]
        if not candidates:
            raise RuntimeError("No generic Solinteg Import Limit number entity could be discovered")
        if len(candidates) != 1:
            names = sorted(str(e.get("entity_id") or "") for e in candidates)
            raise RuntimeError(f"Multiple Solinteg Import Limit entities found; refusing ambiguous write: {names}")
        entity_id = str(candidates[0].get("entity_id") or "")
        await self._state(entity_id)
        self._grid_import_limit_entity = entity_id
        return entity_id

    def configured_grid_import_limit_kw(self) -> float:
        return max(0.0, float((self.cfg.get("optimizer") or {}).get("physical_grid_import_limit_kw", 13.8)))

    async def read_grid_import_limit(self) -> dict[str, Any]:
        entity_id = await self.resolve_grid_import_limit_entity()
        state = await self._state(entity_id)
        raw = state.get("state")
        if raw in (None, "unknown", "unavailable", ""):
            raise RuntimeError(f"Solinteg Import Limit state is unavailable: {entity_id}")
        value = float(raw)
        attrs = state.get("attributes") or {}
        return {
            "entity_id": entity_id,
            "grid_import_limit_kw": value,
            "min_kw": attrs.get("min"),
            "max_kw": attrs.get("max"),
            "step_kw": attrs.get("step"),
        }

    async def ensure_grid_import_limit(self) -> dict[str, Any]:
        desired = self.configured_grid_import_limit_kw()
        current = await self.read_grid_import_limit()
        minimum = current.get("min_kw")
        maximum = current.get("max_kw")
        if minimum is not None and desired < float(minimum) - 1e-9:
            raise RuntimeError(f"Configured grid import limit {desired} kW is below inverter minimum {minimum} kW")
        if maximum is not None and desired > float(maximum) + 1e-9:
            raise RuntimeError(f"Configured grid import limit {desired} kW exceeds inverter maximum {maximum} kW")

        entity_id = str(current["entity_id"])
        before = float(current["grid_import_limit_kw"])
        tolerance = max(0.01, min(self.ack_tolerance_kw, 0.10))
        if abs(before - desired) <= tolerance:
            return {
                **current,
                "configured_grid_import_limit_kw": desired,
                "changed": False,
                "acknowledged": True,
            }

        await self._service(
            "number",
            "set_value",
            {"entity_id": entity_id, "value": round(desired, 2)},
        )
        deadline = asyncio.get_running_loop().time() + self.timeout
        last = current
        while asyncio.get_running_loop().time() < deadline:
            last = await self.read_grid_import_limit()
            actual = float(last["grid_import_limit_kw"])
            if abs(actual - desired) <= tolerance:
                return {
                    **last,
                    "configured_grid_import_limit_kw": desired,
                    "previous_grid_import_limit_kw": before,
                    "changed": True,
                    "acknowledged": True,
                }
            await asyncio.sleep(0.5)
        raise RuntimeError(
            f"Solinteg Import Limit acknowledgement timeout; expected={desired!r} kW, last={last!r}"
        )

    async def discovery_report(self) -> dict[str, Any]:
        states = await self.ha.all_states()
        def rows(kind: str) -> list[dict[str, Any]]:
            ranked = sorted(((_rank(e, kind), e) for e in states), key=lambda x: x[0], reverse=True)
            return [
                {
                    "entity_id": e.get("entity_id"),
                    "friendly_name": (e.get("attributes") or {}).get("friendly_name"),
                    "state": e.get("state"),
                    "score": score,
                }
                for score, e in ranked[:10]
                if score > 0
            ]
        resolved = None
        grid_import_limit = None
        error = None
        try:
            resolved = (await self.resolve_entities()).as_dict()
            grid_import_limit = await self.read_grid_import_limit()
        except Exception as exc:
            error = repr(exc)
        return {
            "resolved": resolved,
            "grid_import_limit": grid_import_limit,
            "error": error,
            "working_mode_candidates": rows("working_mode"),
            "battery_power_target_candidates": rows("battery_power_target"),
        }

    async def readback(self, entities: SolintegEntities | None = None) -> dict[str, Any]:
        entities = entities or await self.resolve_entities()
        mode, target = await asyncio.gather(
            self._state(entities.working_mode),
            self._state(entities.battery_power_target),
        )
        return {
            "entities": entities.as_dict(),
            "working_mode": mode.get("state"),
            "working_mode_options": (mode.get("attributes") or {}).get("options"),
            "battery_power_target_kw": None if target.get("state") in (None, "unknown", "unavailable", "") else float(target.get("state")),
            "target_min_kw": (target.get("attributes") or {}).get("min"),
            "target_max_kw": (target.get("attributes") or {}).get("max"),
        }

    async def set_power_target(self, target_kw: float, entities: SolintegEntities | None = None) -> None:
        entities = entities or await self.resolve_entities()
        await self._service(
            "number",
            "set_value",
            {"entity_id": entities.battery_power_target, "value": round(float(target_kw), 2)},
        )

    async def set_working_mode(self, mode: str, entities: SolintegEntities | None = None) -> None:
        entities = entities or await self.resolve_entities()
        state = await self._state(entities.working_mode)
        options = [str(x) for x in ((state.get("attributes") or {}).get("options") or [])]
        if options and str(mode) not in options:
            raise RuntimeError(f"Solinteg working mode {mode!r} is not offered by {entities.working_mode}: {options}")
        await self._service(
            "select",
            "select_option",
            {"entity_id": entities.working_mode, "option": str(mode)},
        )

    async def wait_for_ack(
        self,
        *,
        expected_mode: str | None = None,
        expected_target_kw: float | None = None,
        entities: SolintegEntities | None = None,
    ) -> dict[str, Any]:
        entities = entities or await self.resolve_entities()
        deadline = asyncio.get_running_loop().time() + self.timeout
        last: dict[str, Any] | None = None
        while asyncio.get_running_loop().time() < deadline:
            last = await self.readback(entities)
            mode_ok = expected_mode is None or str(last.get("working_mode")) == str(expected_mode)
            target = last.get("battery_power_target_kw")
            target_ok = expected_target_kw is None or (
                target is not None and abs(float(target) - float(expected_target_kw)) <= self.ack_tolerance_kw
            )
            if mode_ok and target_ok:
                return {**last, "acknowledged": True}
            await asyncio.sleep(0.5)
        raise RuntimeError(
            f"Solinteg command acknowledgement timeout; expected mode={expected_mode!r}, "
            f"target={expected_target_kw!r}, last={last!r}"
        )

    async def enter_control_mode_zero(self) -> dict[str, Any]:
        entities = await self.resolve_entities()
        control_mode = str((self.cfg.get("actuator") or {}).get("control_working_mode") or CONTROL_MODE_DEFAULT)
        # The configured physical grid-import limit must be active before EMS
        # battery control can create any non-zero power flow.
        grid_limit = await self.ensure_grid_import_limit()
        # Zero first, then enter EMS BattCtrl. This avoids a stale non-zero target
        # becoming active during the mode transition.
        await self.set_power_target(0.0, entities)
        await self.wait_for_ack(expected_target_kw=0.0, entities=entities)
        await self.set_working_mode(control_mode, entities)
        result = await self.wait_for_ack(expected_mode=control_mode, expected_target_kw=0.0, entities=entities)
        return {**result, "grid_import_limit": grid_limit}

    async def dispatch(self, target_kw: float) -> dict[str, Any]:
        entities = await self.resolve_entities()
        control_mode = str((self.cfg.get("actuator") or {}).get("control_working_mode") or CONTROL_MODE_DEFAULT)
        grid_limit = await self.ensure_grid_import_limit()
        before = await self.readback(entities)
        if str(before.get("working_mode")) != control_mode:
            entered = await self.enter_control_mode_zero()
            grid_limit = entered.get("grid_import_limit") or grid_limit
        await self.set_power_target(float(target_kw), entities)
        result = await self.wait_for_ack(
            expected_mode=control_mode,
            expected_target_kw=float(target_kw),
            entities=entities,
        )
        return {**result, "grid_import_limit": grid_limit}

    async def safe_release(self) -> dict[str, Any]:
        entities = await self.resolve_entities()
        safe_mode = str((self.cfg.get("actuator") or {}).get("safe_working_mode") or SAFE_MODE_DEFAULT)
        errors: list[str] = []
        try:
            await self.set_power_target(0.0, entities)
            await self.wait_for_ack(expected_target_kw=0.0, entities=entities)
        except Exception as exc:
            errors.append(f"zero_target:{exc!r}")
        try:
            await self.set_working_mode(safe_mode, entities)
            readback = await self.wait_for_ack(expected_mode=safe_mode, entities=entities)
        except Exception as exc:
            errors.append(f"safe_mode:{exc!r}")
            readback = await self.readback(entities)
        return {"released": not errors, "errors": errors, "readback": readback}
