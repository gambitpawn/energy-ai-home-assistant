from __future__ import annotations

import asyncio
import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .actuator_release_state import release_status
from .db import DB_PATH
from .optimizer_store import latest_plan
from .production_state import (
    mark_actuator_ready,
    set_mode,
    status as production_status,
)

LOCAL_TZ = ZoneInfo("Europe/Stockholm")
ACTION_KIND = "self_sufficiency"
OFFGRID_MODE = "EMS Off-Grid"
CONTROL_INTERVAL_MINUTES = 15
CONTROL_INTERVAL_HOURS = CONTROL_INTERVAL_MINUTES / 60.0
PREP_HORIZON_HOURS = 36
_GRID_CONFIRMATIONS_REQUIRED = 3
_LOCK = RLock()
_INITIALIZED_PATH: str | None = None
_RUNTIME_CONTROLLER: "ExtraordinaryActionController | None" = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        d = value
    else:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=LOCAL_TZ)
    return d.astimezone(timezone.utc)


def _init() -> None:
    global _INITIALIZED_PATH
    path = str(DB_PATH)
    with _LOCK:
        if _INITIALIZED_PATH == path:
            return
        with sqlite3.connect(DB_PATH, timeout=30) as c:
            c.execute("PRAGMA busy_timeout=30000")
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS extraordinary_action(
                    action_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    starts_at TEXT NOT NULL,
                    ends_at TEXT NOT NULL,
                    authorized INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    runtime_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_extraordinary_action_window
                    ON extraordinary_action(status,starts_at,ends_at);
                """
            )
        _INITIALIZED_PATH = path


def _decode(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _row_to_action(row) -> dict[str, Any]:
    return {
        "action_id": int(row[0]),
        "kind": str(row[1]),
        "status": str(row[2]),
        "starts_at": str(row[3]),
        "ends_at": str(row[4]),
        "authorized": bool(row[5]),
        "payload": _decode(row[6]),
        "runtime": _decode(row[7]),
        "created_at": str(row[8]),
        "updated_at": str(row[9]),
    }


def get_action(action_id: int) -> dict[str, Any]:
    _init()
    with sqlite3.connect(DB_PATH, timeout=20) as c:
        row = c.execute(
            """SELECT action_id,kind,status,starts_at,ends_at,authorized,payload_json,
                      runtime_json,created_at,updated_at
               FROM extraordinary_action WHERE action_id=?""",
            (int(action_id),),
        ).fetchone()
    if not row:
        raise KeyError(int(action_id))
    return _row_to_action(row)


def list_actions(*, include_terminal: bool = True) -> list[dict[str, Any]]:
    _init()
    query = """SELECT action_id,kind,status,starts_at,ends_at,authorized,payload_json,
                      runtime_json,created_at,updated_at
               FROM extraordinary_action"""
    args: tuple[Any, ...] = ()
    if not include_terminal:
        query += " WHERE status='scheduled'"
    query += " ORDER BY starts_at ASC, action_id ASC"
    with sqlite3.connect(DB_PATH, timeout=20) as c:
        rows = c.execute(query, args).fetchall()
    return [_row_to_action(row) for row in rows]


def _overlap_exists(start: datetime, end: datetime) -> bool:
    _init()
    with sqlite3.connect(DB_PATH, timeout=20) as c:
        row = c.execute(
            """SELECT action_id FROM extraordinary_action
               WHERE status='scheduled'
                 AND starts_at < ?
                 AND ends_at > ?
               LIMIT 1""",
            (_iso(end), _iso(start)),
        ).fetchone()
    return bool(row)


def create_self_sufficiency_action(
    starts_at: str,
    ends_at: str,
    *,
    restore_operator_mode: str,
) -> dict[str, Any]:
    _init()
    start = _parse(starts_at)
    end = _parse(ends_at)
    now = _now()
    if end <= start:
        raise ValueError("End time must be after start time")
    if end <= now:
        raise ValueError("End time must be in the future")
    if (end - start) > timedelta(hours=72):
        raise ValueError("Self-sufficiency windows are limited to 72 hours")
    if _overlap_exists(start, end):
        raise ValueError("Another scheduled extraordinary action overlaps this window")

    restore = "active" if str(restore_operator_mode).lower() == "active" else "shadow"
    created = _iso(now)
    payload = {
        "purpose": "planned_power_outage",
        "authorized_physical_control": True,
        "offgrid_working_mode": OFFGRID_MODE,
        "restore_operator_mode": restore,
        "timezone": str(LOCAL_TZ),
    }
    runtime = {
        "phase": "scheduled",
        "last_transition_at": created,
        "auto_activated": False,
        "grid_confirmations": 0,
        "last_error": None,
    }
    with sqlite3.connect(DB_PATH, timeout=30) as c:
        cur = c.execute(
            """INSERT INTO extraordinary_action(
                   kind,status,starts_at,ends_at,authorized,payload_json,runtime_json,created_at,updated_at
               ) VALUES(?,?,?,?,1,?,?,?,?)""",
            (
                ACTION_KIND,
                "scheduled",
                _iso(start),
                _iso(end),
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                json.dumps(runtime, ensure_ascii=False, sort_keys=True),
                created,
                created,
            ),
        )
        action_id = int(cur.lastrowid)
    return get_action(action_id)


def update_runtime(action_id: int, **updates: Any) -> dict[str, Any]:
    _init()
    action = get_action(action_id)
    runtime = dict(action.get("runtime") or {})
    runtime.update(updates)
    stamp = _iso(_now())
    with sqlite3.connect(DB_PATH, timeout=30) as c:
        c.execute(
            "UPDATE extraordinary_action SET runtime_json=?,updated_at=? WHERE action_id=?",
            (json.dumps(runtime, ensure_ascii=False, sort_keys=True, default=str), stamp, int(action_id)),
        )
    return get_action(action_id)


def mark_status(action_id: int, status: str, **runtime_updates: Any) -> dict[str, Any]:
    if status not in {"scheduled", "cancelled", "completed", "failed"}:
        raise ValueError(f"Unsupported action status {status!r}")
    action = get_action(action_id)
    runtime = dict(action.get("runtime") or {})
    runtime.update(runtime_updates)
    stamp = _iso(_now())
    with sqlite3.connect(DB_PATH, timeout=30) as c:
        c.execute(
            "UPDATE extraordinary_action SET status=?,runtime_json=?,updated_at=? WHERE action_id=?",
            (
                status,
                json.dumps(runtime, ensure_ascii=False, sort_keys=True, default=str),
                stamp,
                int(action_id),
            ),
        )
    return get_action(action_id)


def cancel_action(action_id: int) -> dict[str, Any]:
    action = get_action(action_id)
    if action["status"] != "scheduled":
        return action
    return mark_status(
        action_id,
        "cancelled",
        phase="cancelled",
        cancelled_at=_iso(_now()),
        last_transition_at=_iso(_now()),
    )


def current_scheduled_action(now: datetime | None = None) -> dict[str, Any] | None:
    now = (now or _now()).astimezone(timezone.utc)
    scheduled = [a for a in list_actions(include_terminal=False) if a.get("authorized")]
    if not scheduled:
        return None
    # An already-started action always outranks a later scheduled action.
    active = [a for a in scheduled if _parse(a["starts_at"]) <= now]
    return (active or scheduled)[0]


def action_phase(action: dict[str, Any], now: datetime | None = None) -> str:
    if action.get("status") != "scheduled":
        return str(action.get("status"))
    now = (now or _now()).astimezone(timezone.utc)
    start = _parse(action["starts_at"])
    end = _parse(action["ends_at"])
    if now >= end:
        return "ending"
    if now >= start:
        return "active"
    if start - now <= timedelta(hours=PREP_HORIZON_HOURS):
        return "preparing"
    return "scheduled"


def control_authority_status(now: datetime | None = None) -> dict[str, Any]:
    action = current_scheduled_action(now)
    if not action:
        return {"blocking": False, "action": None, "phase": None}
    phase = action_phase(action, now)
    return {
        "blocking": phase in {"preparing", "active", "ending"},
        "action": action,
        "phase": phase,
    }


def _plan_rows(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in (plan.get("rows") or []) if row.get("start")]


def _row_bounds(row: dict[str, Any]) -> tuple[datetime, datetime]:
    start = _parse(str(row["start"]))
    return start, start + timedelta(minutes=CONTROL_INTERVAL_MINUTES)


def _overlaps(row: dict[str, Any], start: datetime, end: datetime) -> bool:
    a, b = _row_bounds(row)
    return a < end and b > start


def _battery_params(cfg: dict[str, Any]) -> dict[str, float]:
    battery = (cfg.get("policy") or {}).get("battery") or {}
    optimizer = cfg.get("optimizer") or {}
    actuator = cfg.get("actuator") or {}
    return {
        "capacity_kwh": float(battery.get("capacity_kwh", 19.6)),
        "hard_min_soc_pct": float(battery.get("hard_min_soc_pct", 5.0)),
        "hard_max_soc_pct": float(battery.get("hard_max_soc_pct", 100.0)),
        "guard_pct": max(0.0, float(actuator.get("soc_guard_margin_pct", 1.0))),
        "charge_efficiency": max(0.01, float(optimizer.get("battery_charge_efficiency", 0.95))),
        "discharge_efficiency": max(0.01, float(optimizer.get("battery_discharge_efficiency", 0.95))),
        "max_charge_kw": max(0.0, float(optimizer.get("battery_max_charge_kw", 8.0))),
        "max_discharge_kw": max(0.0, float(optimizer.get("battery_max_discharge_kw", 8.0))),
        "grid_import_limit_kw": max(0.0, float(optimizer.get("physical_grid_import_limit_kw", 13.8))),
    }


def required_start_energy(
    action: dict[str, Any],
    plan: dict[str, Any],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Conservative energy requirement at outage start.

    Forecast uncertainty is treated pessimistically: load uncertainty is added
    and PV uncertainty is subtracted. PV surplus inside the outage may recharge
    the battery, while deficit intervals must be supplied by battery discharge.
    """
    params = _battery_params(cfg)
    cap = params["capacity_kwh"]
    hard_floor_pct = min(
        params["hard_max_soc_pct"],
        params["hard_min_soc_pct"] + params["guard_pct"],
    )
    hard_floor = cap * hard_floor_pct / 100.0
    usable_max_soc_pct = max(hard_floor_pct, params["hard_max_soc_pct"] - params["guard_pct"])
    max_energy = cap * usable_max_soc_pct / 100.0
    start = _parse(action["starts_at"])
    end = _parse(action["ends_at"])
    rows = [r for r in _plan_rows(plan) if _overlaps(r, start, end)]
    if not rows:
        return {
            "available": False,
            "reason": "outage_window_outside_forecast_horizon",
            "required_start_energy_kwh": None,
            "required_start_soc_pct": None,
            "coverage_fraction": 0.0,
            "power_feasible": None,
            "energy_feasible": None,
            "fallback_target_energy_kwh": round(max_energy, 4),
            "fallback_target_soc_pct": round(usable_max_soc_pct, 2),
        }

    covered = 0.0
    required = hard_floor
    power_feasible = True
    max_pessimistic_deficit_kw = 0.0
    for row in reversed(rows):
        row_start, row_end = _row_bounds(row)
        overlap_h = max(
            0.0,
            (min(row_end, end) - max(row_start, start)).total_seconds() / 3600.0,
        )
        if overlap_h <= 0:
            continue
        covered += overlap_h
        load = max(0.0, float(row.get("load_kw") or 0.0))
        pv = max(0.0, float(row.get("pv_kw") or 0.0))
        load_unc = max(0.0, float(row.get("load_uncertainty_kw") or 0.0))
        pv_unc = max(0.0, float(row.get("pv_uncertainty_kw") or 0.0))
        pessimistic_net = (load + load_unc) - max(0.0, pv - pv_unc)
        if pessimistic_net >= 0.0:
            max_pessimistic_deficit_kw = max(max_pessimistic_deficit_kw, pessimistic_net)
            if pessimistic_net > params["max_discharge_kw"] + 1e-9:
                power_feasible = False
            required += pessimistic_net * overlap_h / params["discharge_efficiency"]
        else:
            pv_charge_kw = min(params["max_charge_kw"], -pessimistic_net)
            required = max(
                hard_floor,
                required - pv_charge_kw * overlap_h * params["charge_efficiency"],
            )

    duration_h = max(0.0, (end - start).total_seconds() / 3600.0)
    coverage = min(1.0, covered / duration_h) if duration_h > 0 else 0.0
    energy_feasible = required <= max_energy + 1e-9
    required_clamped = min(max_energy, max(hard_floor, required))
    return {
        "available": coverage >= 0.999,
        "reason": None if coverage >= 0.999 else "forecast_does_not_cover_entire_outage_window",
        "required_start_energy_kwh": round(required_clamped, 4),
        "required_start_soc_pct": round(required_clamped / cap * 100.0, 2),
        "coverage_fraction": round(coverage, 4),
        "power_feasible": power_feasible,
        "energy_feasible": energy_feasible,
        "max_pessimistic_deficit_kw": round(max_pessimistic_deficit_kw, 4),
        "hard_floor_soc_pct": round(hard_floor_pct, 2),
        "usable_max_soc_pct": round(usable_max_soc_pct, 2),
        "fallback_target_energy_kwh": round(max_energy, 4),
        "fallback_target_soc_pct": round(usable_max_soc_pct, 2),
        "uncertainty_policy": "load_plus_uncertainty_pv_minus_uncertainty",
    }


def _max_charge_gain(row: dict[str, Any], cfg: dict[str, Any]) -> float:
    params = _battery_params(cfg)
    net = float(row.get("load_kw") or 0.0) - float(row.get("pv_kw") or 0.0)
    max_ac_charge = min(
        params["max_charge_kw"],
        max(0.0, params["grid_import_limit_kw"] - net),
    )
    return max_ac_charge * CONTROL_INTERVAL_HOURS * params["charge_efficiency"]


def reserve_overlay(
    candidate: dict[str, Any],
    action: dict[str, Any],
    plan: dict[str, Any],
    cfg: dict[str, Any],
    actual: dict[str, Any],
) -> dict[str, Any]:
    """Clamp a selected-engine decision only as much as outage readiness requires."""
    requirement = required_start_energy(action, plan, cfg)
    params = _battery_params(cfg)
    fallback_target = requirement.get("fallback_target_energy_kwh")
    if not requirement.get("available") and fallback_target is None:
        return {
            "candidate": dict(candidate),
            "changed": False,
            "assessment": requirement,
            "reason": requirement.get("reason"),
        }

    cap = params["capacity_kwh"]
    current_soc = actual.get("soc_pct")
    if current_soc is None:
        return {
            "candidate": dict(candidate),
            "changed": False,
            "assessment": requirement,
            "reason": "current_soc_unavailable",
        }

    decision_start = _parse(str(candidate.get("decision_start") or _now().isoformat()))
    interval_end = decision_start + timedelta(minutes=CONTROL_INTERVAL_MINUTES)
    outage_start = _parse(action["starts_at"])
    if interval_end > outage_start:
        interval_end = outage_start
    dt_h = max(0.0, (interval_end - decision_start).total_seconds() / 3600.0)
    if dt_h <= 0:
        return {
            "candidate": dict(candidate),
            "changed": False,
            "assessment": requirement,
            "reason": "candidate_not_before_outage",
        }

    rows = _plan_rows(plan)
    future_gain = 0.0
    for row in rows:
        row_start, row_end = _row_bounds(row)
        if row_start < interval_end or row_start >= outage_start:
            continue
        future_gain += _max_charge_gain(row, cfg)

    target = float(requirement.get("required_start_energy_kwh") if requirement.get("available") else fallback_target)
    hard_floor = cap * min(
        params["hard_max_soc_pct"],
        params["hard_min_soc_pct"] + params["guard_pct"],
    ) / 100.0
    floor_after = max(hard_floor, target - future_gain)
    current_energy = cap * float(current_soc) / 100.0
    requested = float(candidate.get("requested_action_kw") or 0.0)

    if requested < 0:
        predicted_after = current_energy + (-requested) * params["charge_efficiency"] * dt_h
    else:
        predicted_after = current_energy - requested * dt_h / params["discharge_efficiency"]

    adjusted = requested
    if predicted_after < floor_after - 1e-9:
        delta = floor_after - current_energy
        if delta >= 0:
            adjusted = -(delta / params["charge_efficiency"]) / dt_h
        else:
            adjusted = min(
                requested,
                (-delta) * params["discharge_efficiency"] / dt_h,
            )
        adjusted = max(-params["max_charge_kw"], min(params["max_discharge_kw"], adjusted))

    max_future_with_current = (
        current_energy
        + max(0.0, -adjusted) * params["charge_efficiency"] * dt_h
        - max(0.0, adjusted) * dt_h / params["discharge_efficiency"]
        + future_gain
    )
    readiness_feasible = max_future_with_current >= target - 1e-6

    result_candidate = dict(candidate)
    changed = abs(adjusted - requested) > 1e-6
    if changed:
        result_candidate.update(
            {
                "source": "extraordinary_action_reserve_overlay",
                "source_id": f"self_sufficiency:{action['action_id']}",
                "engine_id": str(candidate.get("engine_id") or "unknown"),
                "requested_action_kw": round(adjusted, 6),
                "action_id": int(action["action_id"]),
                "action_kind": ACTION_KIND,
                "original_requested_action_kw": requested,
                "reserve_floor_after_interval_kwh": round(floor_after, 4),
                "required_start_soc_pct": requirement.get("required_start_soc_pct"),
            }
        )
    return {
        "candidate": result_candidate,
        "changed": changed,
        "assessment": {
            **requirement,
            "current_soc_pct": round(float(current_soc), 2),
            "reserve_floor_after_interval_kwh": round(floor_after, 4),
            "future_max_charge_gain_kwh": round(future_gain, 4),
            "predicted_energy_after_kwh": round(predicted_after, 4),
            "readiness_feasible_from_current_interval": readiness_feasible,
            "original_requested_action_kw": round(requested, 4),
            "overlay_requested_action_kw": round(adjusted, 4),
        },
        "reason": ("outage_reserve_constraint" if requirement.get("available") else "conservative_full_charge_until_outage_forecast_complete") if changed else "selected_engine_within_outage_reserve_constraint",
    }


def action_assessment(
    action: dict[str, Any],
    cfg: dict[str, Any],
    plan: dict[str, Any] | None = None,
    actual: dict[str, Any] | None = None,
) -> dict[str, Any]:
    plan = plan or latest_plan(500)
    requirement = required_start_energy(action, plan, cfg) if plan.get("rows") else {
        "available": False,
        "reason": "no_optimizer_plan",
    }
    result = dict(requirement)
    if actual and actual.get("soc_pct") is not None:
        result["current_soc_pct"] = round(float(actual["soc_pct"]), 2)
    result["phase"] = action_phase(action)
    result["starts_in_minutes"] = round(
        (_parse(action["starts_at"]) - _now()).total_seconds() / 60.0,
        1,
    )
    return result


class ExtraordinaryActionController:
    def __init__(self, *, base, app: FastAPI, runtime_ui_module):
        self.base = base
        self.app = app
        self.runtime_ui_module = runtime_ui_module
        self.actuator = base.ACTUATOR
        self.adapter = base.ADAPTER
        self.cfg = base.core.cfg
        self._original_process_candidate = self.actuator.process_candidate
        self._original_watchdog_tick = self.actuator.watchdog_tick
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._transition_lock = asyncio.Lock()
        self._grid_confirmations: dict[int, int] = {}
        self._last_mode_correction: dict[int, datetime] = {}

    def actual(self) -> dict[str, Any] | None:
        try:
            return self.actuator.current_actual()
        except Exception:
            return None

    def plan(self) -> dict[str, Any]:
        try:
            return latest_plan(500)
        except Exception:
            return {}

    def decorated_action(self, action: dict[str, Any]) -> dict[str, Any]:
        return {
            **action,
            "phase": action_phase(action),
            "assessment": action_assessment(action, self.cfg, self.plan(), self.actual()),
        }

    def status_payload(self) -> dict[str, Any]:
        actions = [self.decorated_action(a) for a in list_actions(include_terminal=True)]
        authority = control_authority_status()
        return {
            "ok": True,
            "timezone": str(LOCAL_TZ),
            "offgrid_working_mode": OFFGRID_MODE,
            "actions": actions,
            "authority": {
                "blocking": authority.get("blocking"),
                "phase": authority.get("phase"),
                "action_id": (authority.get("action") or {}).get("action_id"),
            },
            "runtime_task_running": bool(self._task is not None and not self._task.done()),
        }

    async def process_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        authority = control_authority_status()
        action = authority.get("action")
        phase = authority.get("phase")
        if not action:
            return await self._original_process_candidate(candidate)

        if phase in {"active", "ending"}:
            return {
                "status": "extraordinary_action_owns_control",
                "reason": "self_sufficiency_offgrid_window",
                "action_id": action["action_id"],
                "phase": phase,
                "requested_action_kw": candidate.get("requested_action_kw"),
                "physical_write_performed": False,
                "normal_candidate_suppressed": True,
            }

        if phase == "preparing":
            plan = self.plan()
            actual = self.actual()
            if not plan.get("rows") or not actual:
                result = await self._original_process_candidate(candidate)
                result["extraordinary_action"] = {
                    "action_id": action["action_id"],
                    "phase": phase,
                    "overlay_applied": False,
                    "reason": "missing_plan_or_actual",
                }
                return result
            overlay = reserve_overlay(candidate, action, plan, self.cfg, actual)
            result = await self._original_process_candidate(overlay["candidate"])
            result["extraordinary_action"] = {
                "action_id": action["action_id"],
                "phase": phase,
                "overlay_applied": bool(overlay["changed"]),
                "reason": overlay["reason"],
                "assessment": overlay["assessment"],
            }
            return result

        return await self._original_process_candidate(candidate)

    async def watchdog_tick(self) -> dict[str, Any]:
        authority = control_authority_status()
        action = authority.get("action")
        phase = authority.get("phase")
        if not action or phase not in {"active", "ending"}:
            return await self._original_watchdog_tick()

        prod = production_status()
        if prod.get("operating_mode") == "paused":
            return {
                "status": "extraordinary_action_degraded",
                "reason": "production_paused",
                "action_id": action["action_id"],
                "production": prod,
            }
        try:
            readback = await self.adapter.readback()
        except Exception as exc:
            # During an actual grid outage communications may also disappear.
            # Never leave off-grid mode merely because readback is unavailable.
            update_runtime(
                action["action_id"],
                phase=phase,
                last_error=f"offgrid_readback_unavailable:{exc!r}",
                last_watchdog_at=_iso(_now()),
            )
            return {
                "status": "extraordinary_action_degraded",
                "reason": "offgrid_readback_unavailable_hold_mode",
                "action_id": action["action_id"],
                "error": repr(exc),
            }

        mode = str(readback.get("working_mode"))
        if mode == OFFGRID_MODE:
            update_runtime(
                action["action_id"],
                phase=phase,
                last_error=None,
                last_watchdog_at=_iso(_now()),
                verified_working_mode=mode,
            )
            return {
                "status": "extraordinary_action_healthy",
                "action_id": action["action_id"],
                "phase": phase,
                "readback": readback,
            }

        last = self._last_mode_correction.get(int(action["action_id"]))
        if last is None or (_now() - last).total_seconds() >= 60:
            self._last_mode_correction[int(action["action_id"])] = _now()
            try:
                corrected = await self.adapter.enter_offgrid_mode(OFFGRID_MODE)
                update_runtime(
                    action["action_id"],
                    phase=phase,
                    last_error=None,
                    last_mode_correction_at=_iso(_now()),
                    verified_working_mode=OFFGRID_MODE,
                )
                return {
                    "status": "extraordinary_action_corrected",
                    "action_id": action["action_id"],
                    "readback": corrected,
                }
            except Exception as exc:
                update_runtime(
                    action["action_id"],
                    phase=phase,
                    last_error=f"offgrid_mode_correction_failed:{exc!r}",
                    last_watchdog_at=_iso(_now()),
                )
                return {
                    "status": "extraordinary_action_degraded",
                    "reason": "offgrid_mode_correction_failed",
                    "action_id": action["action_id"],
                    "error": repr(exc),
                }

        return {
            "status": "extraordinary_action_waiting_before_mode_retry",
            "action_id": action["action_id"],
            "readback": readback,
        }

    async def _ensure_prep_control(self, action: dict[str, Any]) -> dict[str, Any]:
        prod = production_status()
        if prod.get("operating_mode") == "paused":
            update_runtime(
                action["action_id"],
                phase="preparing",
                last_error="production_paused_requires_manual_recovery",
            )
            return {"ok": False, "reason": "production_paused"}

        runtime = action.get("runtime") or {}
        if not runtime.get("restore_operator_mode"):
            restore_mode = "active" if prod.get("operating_mode") == "active" and prod.get("physical_writes_enabled") else "shadow"
            action = update_runtime(action["action_id"], restore_operator_mode=restore_mode)
            runtime = action.get("runtime") or {}

        if prod.get("operating_mode") == "active" and prod.get("physical_writes_enabled") and prod.get("actuator_ready"):
            return {"ok": True, "already_active": True, "restore_operator_mode": runtime.get("restore_operator_mode")}

        release = None
        if release_status().get("release_pending"):
            release = await self.adapter.safe_release()
            if not release.get("released"):
                raise RuntimeError(f"pending safe release failed: {release}")

        preflight = await self.actuator.preflight()
        if not preflight.get("ok"):
            raise RuntimeError(f"action preflight failed: {preflight}")

        arm = await self.actuator.zero_handshake_and_arm()
        if not arm.get("ok"):
            raise RuntimeError(f"action arm failed: {arm}")

        await asyncio.to_thread(
            set_mode,
            "active",
            reason=f"extraordinary_action_prepare:{action['action_id']}",
        )
        update_runtime(
            action["action_id"],
            phase="preparing",
            auto_activated=True,
            auto_activated_at=_iso(_now()),
            last_transition_at=_iso(_now()),
            last_error=None,
        )
        return {
            "ok": True,
            "preflight": preflight,
            "arm": arm,
            "release": release,
        }

    async def _enter_offgrid(self, action: dict[str, Any]) -> dict[str, Any]:
        prod = production_status()
        if prod.get("operating_mode") == "paused":
            update_runtime(
                action["action_id"],
                phase="active",
                last_error="production_paused_at_action_start",
            )
            return {"ok": False, "reason": "production_paused"}

        await self._ensure_prep_control(action)
        readback = await self.adapter.enter_offgrid_mode(OFFGRID_MODE)
        update_runtime(
            action["action_id"],
            phase="active",
            offgrid_entered_at=_iso(_now()),
            verified_working_mode=str(readback.get("working_mode") or OFFGRID_MODE),
            last_transition_at=_iso(_now()),
            last_error=None,
        )
        return {"ok": True, "readback": readback}

    async def _restore_after_action(
        self,
        action: dict[str, Any],
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        if not force:
            grid = await self.adapter.grid_availability()
            if not grid.get("available"):
                self._grid_confirmations[int(action["action_id"])] = 0
                update_runtime(
                    action["action_id"],
                    phase="ending",
                    grid_confirmations=0,
                    grid_status=grid,
                    last_error=None if grid.get("measurable") else "grid_confirmation_unavailable",
                )
                return {"ok": False, "waiting": True, "grid": grid}
            confirmations = self._grid_confirmations.get(int(action["action_id"]), 0) + 1
            self._grid_confirmations[int(action["action_id"])] = confirmations
            update_runtime(
                action["action_id"],
                phase="ending",
                grid_confirmations=confirmations,
                grid_status=grid,
                last_error=None,
            )
            if confirmations < _GRID_CONFIRMATIONS_REQUIRED:
                return {
                    "ok": False,
                    "waiting": True,
                    "grid": grid,
                    "confirmations": confirmations,
                    "required": _GRID_CONFIRMATIONS_REQUIRED,
                }

        restore = str((action.get("runtime") or {}).get("restore_operator_mode") or (action.get("payload") or {}).get("restore_operator_mode") or "shadow")
        if restore == "active":
            entered = await self.adapter.enter_control_mode_zero()
            mark_status(
                action["action_id"],
                "completed",
                phase="completed",
                completed_at=_iso(_now()),
                forced_end=bool(force),
                last_transition_at=_iso(_now()),
                last_error=None,
            )
            try:
                refresh = await self.base.refresh_optimizer_plan()
            except Exception as exc:
                await self.actuator.fail_safe(
                    "extraordinary_action_restore_refresh_failed",
                    {"action_id": action["action_id"], "error": repr(exc)},
                )
                return {
                    "ok": False,
                    "restored_mode": "active",
                    "entered": entered,
                    "error": repr(exc),
                }
            return {
                "ok": True,
                "restored_mode": "active",
                "entered": entered,
                "refresh": refresh,
            }

        disarm = await self.actuator.disarm("extraordinary_action_complete_restore_shadow")
        mark_status(
            action["action_id"],
            "completed",
            phase="completed",
            completed_at=_iso(_now()),
            forced_end=bool(force),
            last_transition_at=_iso(_now()),
            last_error=None,
        )
        return {
            "ok": bool(disarm.get("ok")),
            "restored_mode": "shadow",
            "disarm": disarm,
        }

    async def cancel(self, action_id: int) -> dict[str, Any]:
        async with self._transition_lock:
            action = get_action(action_id)
            phase = action_phase(action)
            if action["status"] != "scheduled":
                return self.decorated_action(action)
            if phase in {"active", "ending"}:
                # Cancellation of an active outage is an end request; grid return
                # must still be confirmed unless the explicit force-end API is used.
                runtime = update_runtime(
                    action_id,
                    cancellation_requested=True,
                    scheduled_end_overridden_at=_iso(_now()),
                    last_transition_at=_iso(_now()),
                )
                with sqlite3.connect(DB_PATH, timeout=30) as c:
                    c.execute(
                        "UPDATE extraordinary_action SET ends_at=?,updated_at=? WHERE action_id=?",
                        (_iso(_now()), _iso(_now()), int(action_id)),
                    )
                return self.decorated_action(get_action(action_id))

            cancelled = cancel_action(action_id)
            runtime = cancelled.get("runtime") or {}
            if runtime.get("auto_activated"):
                restore = str((cancelled.get("runtime") or {}).get("restore_operator_mode") or (cancelled.get("payload") or {}).get("restore_operator_mode") or "shadow")
                if restore == "shadow":
                    await self.actuator.disarm("extraordinary_action_cancelled")
                else:
                    try:
                        await self.base.refresh_optimizer_plan()
                    except Exception:
                        pass
            return self.decorated_action(cancelled)

    async def force_end(self, action_id: int) -> dict[str, Any]:
        async with self._transition_lock:
            action = get_action(action_id)
            if action["status"] != "scheduled":
                return self.decorated_action(action)
            result = await self._restore_after_action(action, force=True)
            return {"action": self.decorated_action(get_action(action_id)), "restore": result}

    async def reconcile_once(self) -> dict[str, Any]:
        action = current_scheduled_action()
        if not action:
            return {"status": "idle"}
        phase = action_phase(action)
        action_id = int(action["action_id"])
        try:
            if phase == "scheduled":
                update_runtime(action_id, phase="scheduled", last_error=None)
                return {"status": "scheduled", "action_id": action_id}

            if phase == "preparing":
                assessment = action_assessment(action, self.cfg, self.plan(), self.actual())
                update_runtime(
                    action_id,
                    phase="preparing",
                    assessment=assessment,
                    last_reconciled_at=_iso(_now()),
                )
                if not assessment.get("available"):
                    return {
                        "status": "preparing_waiting_for_forecast_coverage",
                        "action_id": action_id,
                        "assessment": assessment,
                    }
                control = await self._ensure_prep_control(action)
                if not control.get("ok"):
                    return {"status": "preparing_blocked", "action_id": action_id, "control": control}
                runtime = get_action(action_id).get("runtime") or {}
                refresh = None
                if not runtime.get("prep_refresh_at") or not control.get("already_active"):
                    # Exactly one immediate refresh when action preparation gains
                    # control. Normal quarter planning handles subsequent updates.
                    refresh = await self.base.refresh_optimizer_plan()
                    update_runtime(action_id, prep_refresh_at=_iso(_now()))
                return {
                    "status": "preparing",
                    "action_id": action_id,
                    "assessment": assessment,
                    "refresh": refresh,
                }

            if phase == "active":
                runtime = action.get("runtime") or {}
                if not runtime.get("offgrid_entered_at"):
                    result = await self._enter_offgrid(action)
                    return {"status": "active_transition", "action_id": action_id, "result": result}
                try:
                    readback = await self.adapter.readback()
                except Exception as exc:
                    update_runtime(
                        action_id,
                        phase="active",
                        last_error=f"active_mode_readback_unavailable:{exc!r}",
                    )
                    return {
                        "status": "active_degraded_hold_mode",
                        "action_id": action_id,
                        "error": repr(exc),
                    }
                if str(readback.get("working_mode")) != OFFGRID_MODE:
                    result = await self._enter_offgrid(action)
                    return {
                        "status": "active_reentered_offgrid",
                        "action_id": action_id,
                        "result": result,
                    }
                return {"status": "active", "action_id": action_id, "readback": readback}

            if phase == "ending":
                result = await self._restore_after_action(action, force=False)
                return {
                    "status": "completed" if result.get("ok") else "ending",
                    "action_id": action_id,
                    "result": result,
                }
        except Exception as exc:
            update_runtime(
                action_id,
                phase=phase,
                last_error=repr(exc),
                last_reconciled_at=_iso(_now()),
            )
            return {
                "status": "error",
                "action_id": action_id,
                "phase": phase,
                "error": repr(exc),
            }
        return {"status": phase, "action_id": action_id}

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                pass

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self.run(), name="energy-ai-extraordinary-actions")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None


def install_extraordinary_actions(*, app: FastAPI, base, runtime_ui_module, ui_extension: str) -> dict[str, Any]:
    global _RUNTIME_CONTROLLER
    if getattr(app.state, "extraordinary_actions_installed", False):
        return {"installed": True, "already_installed": True}

    controller = ExtraordinaryActionController(
        base=base,
        app=app,
        runtime_ui_module=runtime_ui_module,
    )
    _RUNTIME_CONTROLLER = controller
    base.ACTUATOR.process_candidate = controller.process_candidate
    base.ACTUATOR.watchdog_tick = controller.watchdog_tick

    if ui_extension not in runtime_ui_module.CURRENT_UI_EXTENSION:
        runtime_ui_module.CURRENT_UI_EXTENSION += ui_extension

    @app.get("/actions", tags=["actions"])
    async def actions_status():
        return JSONResponse(controller.status_payload())

    @app.post("/actions/self-sufficiency", tags=["actions"])
    async def create_action_route(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        starts_at = str((body or {}).get("starts_at") or "").strip()
        ends_at = str((body or {}).get("ends_at") or "").strip()
        if not starts_at or not ends_at:
            raise HTTPException(400, "starts_at and ends_at are required")
        prod = production_status()
        restore_mode = "active" if prod.get("operating_mode") == "active" and prod.get("physical_writes_enabled") else "shadow"
        try:
            action = await asyncio.to_thread(
                create_self_sufficiency_action,
                starts_at,
                ends_at,
                restore_operator_mode=restore_mode,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        reconcile = await controller.reconcile_once()
        return JSONResponse({"action": controller.decorated_action(action), "reconcile": reconcile})

    @app.delete("/actions/{action_id}", tags=["actions"])
    async def cancel_action_route(action_id: int):
        try:
            result = await controller.cancel(action_id)
        except KeyError:
            raise HTTPException(404, "Action not found")
        return JSONResponse(result)

    @app.post("/actions/{action_id}/force-end", tags=["actions"])
    async def force_end_action_route(action_id: int):
        try:
            result = await controller.force_end(action_id)
        except KeyError:
            raise HTTPException(404, "Action not found")
        return JSONResponse(result)

    @app.post("/actions/reconcile", tags=["actions"])
    async def reconcile_actions_route():
        return JSONResponse(await controller.reconcile_once())

    base_lifespan = app.router.lifespan_context

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def actions_lifespan(application):
        async with base_lifespan(application) as lifespan_state:
            controller.start()
            try:
                yield lifespan_state
            finally:
                await controller.stop()

    app.router.lifespan_context = actions_lifespan
    app.state.extraordinary_actions_installed = True
    app.openapi_schema = None
    return {
        "installed": True,
        "already_installed": False,
        "policy": "scheduled_self_sufficiency_v1",
        "control_precedence": "extraordinary_action_over_selected_engine",
        "offgrid_mode": OFFGRID_MODE,
        "grid_return_policy": "three_voltage_confirmations_or_explicit_force_end",
        "frozen_baseline_modified": False,
    }
