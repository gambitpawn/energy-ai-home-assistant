from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import DB_PATH
from .production_state import mark_actuator_ready, set_mode, status as production_status
from .solinteg_command import SolintegCommandAdapter


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dt(value: str) -> datetime:
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _init_tables() -> None:
    with sqlite3.connect(DB_PATH, timeout=10) as c:
        c.executescript(
            '''
            CREATE TABLE IF NOT EXISTS actuator_command(
                command_id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                source TEXT NOT NULL,
                source_id TEXT,
                engine_id TEXT,
                decision_start TEXT,
                valid_until TEXT,
                requested_action_kw REAL,
                safe_action_kw REAL,
                physical_write INTEGER NOT NULL,
                status TEXT NOT NULL,
                reason TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_actuator_command_created
                ON actuator_command(created_at DESC);
            CREATE TABLE IF NOT EXISTS actuator_event(
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                reason TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            '''
        )


def _event(event_type: str, reason: str, payload: dict[str, Any] | None = None) -> None:
    _init_tables()
    with sqlite3.connect(DB_PATH, timeout=10) as c:
        c.execute(
            "INSERT INTO actuator_event(created_at,event_type,reason,payload_json) VALUES (?,?,?,?)",
            (_now().isoformat(), str(event_type), str(reason), json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)),
        )


def _insert_command(
    candidate: dict[str, Any],
    *,
    safe_action_kw: float | None,
    physical_write: bool,
    status: str,
    reason: str,
    payload: dict[str, Any],
) -> int:
    _init_tables()
    with sqlite3.connect(DB_PATH, timeout=10) as c:
        cur = c.execute(
            '''INSERT INTO actuator_command(
               created_at,source,source_id,engine_id,decision_start,valid_until,
               requested_action_kw,safe_action_kw,physical_write,status,reason,payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
            (
                _now().isoformat(),
                str(candidate.get("source") or "unknown"),
                None if candidate.get("source_id") is None else str(candidate.get("source_id")),
                None if candidate.get("engine_id") is None else str(candidate.get("engine_id")),
                None if candidate.get("decision_start") is None else str(candidate.get("decision_start")),
                None if candidate.get("valid_until") is None else str(candidate.get("valid_until")),
                None if candidate.get("requested_action_kw") is None else float(candidate.get("requested_action_kw")),
                None if safe_action_kw is None else float(safe_action_kw),
                1 if physical_write else 0,
                str(status),
                str(reason),
                json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
            ),
        )
        return int(cur.lastrowid)


def _latest_actual() -> dict[str, Any] | None:
    with sqlite3.connect(DB_PATH, timeout=10) as c:
        row = c.execute(
            "SELECT collected_at,payload_json FROM raw_state ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row[1])
    except Exception:
        return None

    def value(key: str) -> float | None:
        item = payload.get(key) or {}
        if not item.get("available"):
            return None
        try:
            v = float(item.get("state"))
            return v if math.isfinite(v) else None
        except Exception:
            return None

    observed_at = _dt(str(row[0]))
    return {
        "observed_at": observed_at.isoformat(),
        "age_seconds": max(0.0, (_now() - observed_at).total_seconds()),
        "soc_pct": value("battery_soc_pct"),
        "load_kw": value("house_load_kw"),
        "pv_kw": value("pv_power_kw"),
        "grid_kw": value("grid_power_kw"),
        "battery_kw": value("battery_power_kw"),
    }


def _last_effective_command() -> dict[str, Any] | None:
    _init_tables()
    with sqlite3.connect(DB_PATH, timeout=10) as c:
        row = c.execute(
            '''SELECT command_id,created_at,source,source_id,engine_id,decision_start,valid_until,
                      requested_action_kw,safe_action_kw,status,reason,payload_json
               FROM actuator_command
               WHERE status IN ('acknowledged','held_existing') AND physical_write=1
               ORDER BY command_id DESC LIMIT 1'''
        ).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row[11] or "{}")
    except Exception:
        payload = {}
    return {
        "command_id": int(row[0]),
        "created_at": str(row[1]),
        "source": str(row[2]),
        "source_id": row[3],
        "engine_id": row[4],
        "decision_start": row[5],
        "valid_until": row[6],
        "requested_action_kw": row[7],
        "safe_action_kw": row[8],
        "status": str(row[9]),
        "reason": str(row[10]),
        "payload": payload,
    }


def _candidate_valid(candidate: dict[str, Any], cfg: dict[str, Any], now: datetime | None = None) -> tuple[bool, str]:
    now = now or _now()
    grace = float((cfg.get("actuator") or {}).get("candidate_grace_seconds", 120.0))
    valid_until = candidate.get("valid_until")
    if not valid_until:
        return False, "candidate_missing_valid_until"
    try:
        expiry = _dt(str(valid_until)) + timedelta(seconds=max(0.0, grace))
    except Exception:
        return False, "candidate_invalid_valid_until"
    if now > expiry:
        return False, "candidate_expired"
    if candidate.get("requested_action_kw") is None:
        return False, "candidate_missing_action"
    try:
        requested = float(candidate["requested_action_kw"])
    except Exception:
        return False, "candidate_invalid_action"
    if not math.isfinite(requested):
        return False, "candidate_non_finite_action"
    return True, "ok"


def safety_filter(candidate: dict[str, Any], cfg: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    requested = float(candidate["requested_action_kw"])
    actuator = cfg.get("actuator") or {}
    optimizer = cfg.get("optimizer") or {}
    battery = (cfg.get("policy") or {}).get("battery") or {}
    stale = float(actuator.get("state_max_age_seconds", 180.0))
    if float(actual.get("age_seconds") or 1e9) > stale:
        raise RuntimeError(f"actual_state_stale:{actual.get('age_seconds')}s>{stale}s")
    soc = actual.get("soc_pct")
    load = actual.get("load_kw")
    pv = actual.get("pv_kw")
    if soc is None or load is None or pv is None:
        raise RuntimeError("actual_state_missing_soc_load_or_pv")

    soc = float(soc)
    load = max(0.0, float(load))
    pv = max(0.0, float(pv))
    cap = float(battery.get("capacity_kwh", 19.6))
    hmin = float(battery.get("hard_min_soc_pct", 5.0))
    hmax = float(battery.get("hard_max_soc_pct", 100.0))
    guard = max(0.0, float(actuator.get("soc_guard_margin_pct", 1.0)))
    cmax = max(0.0, float(optimizer.get("battery_max_charge_kw", 8.0)))
    dmax = max(0.0, float(optimizer.get("battery_max_discharge_kw", 8.0)))
    ec = max(0.01, float(optimizer.get("battery_charge_efficiency", 0.95)))
    ed = max(0.01, float(optimizer.get("battery_discharge_efficiency", 0.95)))
    import_limit = max(0.0, float(optimizer.get("physical_grid_import_limit_kw", 13.8)))
    export_limit = max(0.0, float(optimizer.get("grid_export_limit_kw", 10.0)))

    # Protect an entire normal 15-minute control interval against crossing the
    # hard SOC limits, even though the watchdog observes the plant more often.
    dt_hours = 0.25
    energy = cap * max(hmin, min(hmax, soc)) / 100.0
    min_energy = cap * min(hmax, hmin + guard) / 100.0
    max_energy = cap * max(hmin, hmax - guard) / 100.0
    discharge_by_soc = max(0.0, energy - min_energy) * ed / dt_hours
    charge_by_soc = max(0.0, max_energy - energy) / ec / dt_hours

    net = load - pv
    # grid = net - battery_action. Positive grid means import.
    lower = max(-cmax, net - import_limit, -charge_by_soc)
    upper = min(dmax, net + export_limit, discharge_by_soc)
    if lower > upper + 1e-9:
        raise RuntimeError(f"no_safe_action_interval:lower={lower:.3f},upper={upper:.3f}")
    safe = min(upper, max(lower, requested))

    if soc <= hmin + guard + 1e-9 and safe > 0.0:
        safe = 0.0
    if soc >= hmax - guard - 1e-9 and safe < 0.0:
        safe = 0.0

    predicted_grid = net - safe
    reasons: list[str] = []
    if abs(safe - requested) > 1e-6:
        reasons.append("safety_clamped")
    if abs(safe) < float(actuator.get("zero_deadband_kw", 0.05)):
        safe = 0.0
    return {
        "requested_action_kw": requested,
        "safe_action_kw": round(safe, 4),
        "clamped": abs(safe - requested) > 1e-6,
        "reasons": reasons,
        "actual": actual,
        "predicted_grid_kw": round(predicted_grid, 4),
        "safe_interval_kw": {"min": round(lower, 4), "max": round(upper, 4)},
        "soc_guard": {"hard_min_pct": hmin, "hard_max_pct": hmax, "margin_pct": guard},
    }


class DeterministicActuator:
    def __init__(self, cfg: dict[str, Any], adapter: SolintegCommandAdapter):
        self.cfg = cfg
        self.adapter = adapter
        _init_tables()

    async def preflight(self) -> dict[str, Any]:
        prod = production_status()
        actual = _latest_actual()
        report: dict[str, Any] = {
            "ok": False,
            "production": prod,
            "authenticated": bool(self.adapter.ha.authenticated),
            "actual": actual,
            "physical_write_performed": False,
        }
        if not self.adapter.ha.authenticated:
            report["error"] = "home_assistant_not_authenticated"
            return report
        try:
            entities = await self.adapter.resolve_entities()
            readback = await self.adapter.readback(entities)
            control_mode = str((self.cfg.get("actuator") or {}).get("control_working_mode") or "EMS BattCtrl")
            safe_mode = str((self.cfg.get("actuator") or {}).get("safe_working_mode") or "General")
            options = [str(x) for x in (readback.get("working_mode_options") or [])]
            if options and control_mode not in options:
                raise RuntimeError(f"control mode {control_mode!r} not exposed by Working Mode entity")
            if options and safe_mode not in options:
                raise RuntimeError(f"safe mode {safe_mode!r} not exposed by Working Mode entity")
            if actual is None:
                raise RuntimeError("no_actual_state")
            max_age = float((self.cfg.get("actuator") or {}).get("state_max_age_seconds", 180.0))
            if float(actual.get("age_seconds") or 1e9) > max_age:
                raise RuntimeError("actual_state_stale")
            if actual.get("soc_pct") is None or actual.get("load_kw") is None or actual.get("pv_kw") is None:
                raise RuntimeError("actual_state_missing_soc_load_or_pv")
            report.update({"ok": True, "entities": entities.as_dict(), "readback": readback})
        except Exception as exc:
            report["error"] = repr(exc)
        return report

    async def zero_handshake_and_arm(self) -> dict[str, Any]:
        preflight = await self.preflight()
        if not preflight.get("ok"):
            mark_actuator_ready(False, detail=f"preflight_failed:{preflight.get('error')}")
            return {"ok": False, "stage": "preflight", "preflight": preflight}
        try:
            entered = await self.adapter.enter_control_mode_zero()
            released = await self.adapter.safe_release()
            if not released.get("released"):
                raise RuntimeError(f"safe release failed after zero handshake: {released}")
            mark_actuator_ready(True, detail="solinteg_zero_handshake_acknowledged")
            _event("actuator_armed", "zero_handshake_acknowledged", {"entered": entered, "released": released})
            return {
                "ok": True,
                "stage": "armed",
                "physical_write_performed": True,
                "zero_power_only": True,
                "control_mode_test": entered,
                "safe_release": released,
                "production": production_status(),
            }
        except Exception as exc:
            try:
                release = await self.adapter.safe_release()
            except Exception as release_exc:
                release = {"released": False, "error": repr(release_exc)}
            mark_actuator_ready(False, detail=f"zero_handshake_failed:{exc!r}")
            _event("actuator_arm_failed", repr(exc), {"safe_release": release})
            return {"ok": False, "stage": "zero_handshake", "error": repr(exc), "safe_release": release}

    async def disarm(self, reason: str = "manual") -> dict[str, Any]:
        try:
            release = await self.adapter.safe_release()
        except Exception as exc:
            release = {"released": False, "error": repr(exc)}
        try:
            set_mode("shadow", reason=f"actuator_disarm:{reason}")
        finally:
            mark_actuator_ready(False, detail=f"disarmed:{reason}")
        _event("actuator_disarmed", reason, {"safe_release": release})
        return {"ok": bool(release.get("released")), "safe_release": release, "production": production_status()}

    async def fail_safe(self, reason: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            release = await self.adapter.safe_release()
        except Exception as exc:
            release = {"released": False, "error": repr(exc)}
        mark_actuator_ready(False, detail=f"fault:{reason}")
        try:
            set_mode("paused", reason=f"actuator_fault:{reason}")
        except Exception:
            pass
        _event("actuator_fail_safe", reason, {"detail": payload or {}, "safe_release": release})
        return {"ok": False, "status": "fail_safe", "reason": reason, "safe_release": release, "production": production_status()}

    async def process_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        valid, valid_reason = _candidate_valid(candidate, self.cfg)
        if not valid:
            result = {"status": "rejected", "reason": valid_reason, "physical_write_performed": False}
            _insert_command(candidate, safe_action_kw=None, physical_write=False, status="rejected", reason=valid_reason, payload=result)
            if production_status().get("physical_writes_enabled"):
                return await self.fail_safe(valid_reason, candidate)
            return result

        actual = _latest_actual()
        if actual is None:
            result = {"status": "rejected", "reason": "no_actual_state", "physical_write_performed": False}
            _insert_command(candidate, safe_action_kw=None, physical_write=False, status="rejected", reason="no_actual_state", payload=result)
            if production_status().get("physical_writes_enabled"):
                return await self.fail_safe("no_actual_state", candidate)
            return result
        try:
            safety = safety_filter(candidate, self.cfg, actual)
        except Exception as exc:
            result = {"status": "rejected", "reason": repr(exc), "physical_write_performed": False}
            _insert_command(candidate, safe_action_kw=None, physical_write=False, status="rejected", reason=repr(exc), payload=result)
            if production_status().get("physical_writes_enabled"):
                return await self.fail_safe("safety_filter_failed", result)
            return result

        safe_action = float(safety["safe_action_kw"])
        prod = production_status()
        if not prod.get("physical_writes_enabled") or prod.get("operating_mode") != "active":
            result = {
                "status": "dry_run",
                "reason": "production_not_active",
                "requested_action_kw": float(candidate["requested_action_kw"]),
                "safe_action_kw": safe_action,
                "safety": safety,
                "physical_write_performed": False,
            }
            _insert_command(candidate, safe_action_kw=safe_action, physical_write=False, status="dry_run", reason="production_not_active", payload=result)
            return result
        if not prod.get("actuator_ready"):
            return await self.fail_safe("active_without_actuator_ready", {"candidate": candidate})

        previous = _last_effective_command()
        min_change = max(0.0, float((self.cfg.get("actuator") or {}).get("min_action_change_kw", 0.10)))
        if previous is not None and previous.get("safe_action_kw") is not None:
            prior = float(previous["safe_action_kw"])
            if abs(prior - safe_action) < min_change:
                # The prior target must still be inside the *current* safety envelope.
                lo = float(safety["safe_interval_kw"]["min"])
                hi = float(safety["safe_interval_kw"]["max"])
                if lo - 1e-9 <= prior <= hi + 1e-9:
                    result = {
                        "status": "held_existing",
                        "reason": "within_min_action_change",
                        "requested_action_kw": float(candidate["requested_action_kw"]),
                        "safe_action_kw": prior,
                        "safety": safety,
                        "physical_write_performed": True,
                        "write_skipped": True,
                    }
                    _insert_command(candidate, safe_action_kw=prior, physical_write=True, status="held_existing", reason="within_min_action_change", payload=result)
                    return result

        try:
            ack = await self.adapter.dispatch(safe_action)
            result = {
                "status": "acknowledged",
                "reason": "solinteg_target_acknowledged",
                "requested_action_kw": float(candidate["requested_action_kw"]),
                "safe_action_kw": safe_action,
                "safety": safety,
                "readback": ack,
                "physical_write_performed": True,
            }
            command_id = _insert_command(candidate, safe_action_kw=safe_action, physical_write=True, status="acknowledged", reason="solinteg_target_acknowledged", payload=result)
            result["command_id"] = command_id
            return result
        except Exception as exc:
            result = {"candidate": candidate, "safety": safety, "error": repr(exc)}
            _insert_command(candidate, safe_action_kw=safe_action, physical_write=True, status="failed", reason="command_or_ack_failed", payload=result)
            return await self.fail_safe("command_or_ack_failed", result)

    async def watchdog_tick(self) -> dict[str, Any]:
        prod = production_status()
        if not prod.get("physical_writes_enabled") or prod.get("operating_mode") != "active":
            return {"status": "inactive", "production": prod}
        last = _last_effective_command()
        if last is None:
            return await self.fail_safe("active_without_successful_command")
        candidate = {
            "source": last.get("source"),
            "source_id": last.get("source_id"),
            "engine_id": last.get("engine_id"),
            "decision_start": last.get("decision_start"),
            "valid_until": last.get("valid_until"),
            "requested_action_kw": last.get("safe_action_kw"),
        }
        valid, reason = _candidate_valid(candidate, self.cfg)
        if not valid:
            return await self.fail_safe(f"watchdog_{reason}", {"last_command": last})
        try:
            readback = await self.adapter.readback()
        except Exception as exc:
            return await self.fail_safe("watchdog_readback_failed", {"error": repr(exc)})
        control_mode = str((self.cfg.get("actuator") or {}).get("control_working_mode") or "EMS BattCtrl")
        tolerance = float((self.cfg.get("actuator") or {}).get("ack_tolerance_kw", 0.10))
        actual_target = readback.get("battery_power_target_kw")
        expected = float(last.get("safe_action_kw") or 0.0)
        if str(readback.get("working_mode")) != control_mode:
            return await self.fail_safe("watchdog_working_mode_drift", {"readback": readback, "last_command": last})
        if actual_target is None or abs(float(actual_target) - expected) > tolerance:
            return await self.fail_safe("watchdog_target_drift", {"readback": readback, "last_command": last})
        actual = _latest_actual()
        if actual is None:
            return await self.fail_safe("watchdog_no_actual_state")
        try:
            safety = safety_filter(candidate, self.cfg, actual)
        except Exception as exc:
            return await self.fail_safe("watchdog_safety_filter_failed", {"error": repr(exc)})
        lo, hi = float(safety["safe_interval_kw"]["min"]), float(safety["safe_interval_kw"]["max"])
        if expected < lo - 1e-6 or expected > hi + 1e-6:
            return await self.fail_safe("watchdog_command_outside_current_safety_envelope", {"safety": safety, "last_command": last})
        return {"status": "healthy", "last_command": last, "readback": readback, "actual": actual}

    async def status(self) -> dict[str, Any]:
        preflight = await self.preflight()
        last = _last_effective_command()
        with sqlite3.connect(DB_PATH, timeout=10) as c:
            recent = c.execute(
                "SELECT command_id,created_at,source,engine_id,requested_action_kw,safe_action_kw,physical_write,status,reason FROM actuator_command ORDER BY command_id DESC LIMIT 10"
            ).fetchall()
            events = c.execute(
                "SELECT event_id,created_at,event_type,reason,payload_json FROM actuator_event ORDER BY event_id DESC LIMIT 10"
            ).fetchall()
        return {
            "production": production_status(),
            "preflight": preflight,
            "last_effective_command": last,
            "recent_commands": [
                {
                    "command_id": r[0], "created_at": r[1], "source": r[2], "engine_id": r[3],
                    "requested_action_kw": r[4], "safe_action_kw": r[5], "physical_write": bool(r[6]),
                    "status": r[7], "reason": r[8],
                }
                for r in recent
            ],
            "recent_events": [
                {"event_id": r[0], "created_at": r[1], "event_type": r[2], "reason": r[3], "payload": json.loads(r[4] or "{}")}
                for r in events
            ],
            "safety_semantics": {
                "hard_soc_guard": True,
                "actual_load_pv_grid_envelope": True,
                "state_staleness_gate": True,
                "candidate_expiry_gate": True,
                "solinteg_mode_and_target_ack": True,
                "watchdog_readback": True,
                "clean_shutdown_safe_release": True,
                "process_crash_inverter_timeout_guaranteed": False,
            },
        }
