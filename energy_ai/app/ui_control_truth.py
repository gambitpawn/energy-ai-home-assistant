from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import DB_PATH
from .production_state import status as production_status


def _dt(value: str) -> datetime:
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _action_kind(action_kw: float) -> str:
    if abs(float(action_kw)) < 0.05:
        return "idle"
    return "charge" if float(action_kw) < 0.0 else "discharge"


def _item(
    *,
    start: str,
    action_kw: float,
    engine_id: str | None,
    reason: str,
    reason_code: str,
    requested_action_kw: float | None = None,
    safe_action_kw: float | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    return {
        "start": str(start),
        "action_kw": float(action_kw),
        "action": _action_kind(float(action_kw)),
        "reason_code": str(reason_code),
        "reason": str(reason),
        "engine_id": engine_id,
        "requested_action_kw": requested_action_kw,
        "safe_action_kw": safe_action_kw,
        "source": source,
    }


def _selection_rows() -> list[dict[str, Any]]:
    try:
        with sqlite3.connect(DB_PATH, timeout=20) as c:
            rows = c.execute(
                '''SELECT information_vintage_id,decision_start,created_at,routed_engine_id,
                          decision_id,requested_action_kw,fallback_used,reason,payload_json
                   FROM engine_control_selection
                   ORDER BY decision_start DESC,created_at DESC LIMIT 96'''
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for row in rows:
        try:
            payload = json.loads(row[8] or "{}")
        except Exception:
            payload = {}
        out.append({
            "information_vintage_id": str(row[0]),
            "decision_start": str(row[1]),
            "created_at": str(row[2]),
            "routed_engine_id": None if row[3] is None else str(row[3]),
            "decision_id": None if row[4] is None else str(row[4]),
            "requested_action_kw": None if row[5] is None else float(row[5]),
            "fallback_used": bool(row[6]),
            "reason": str(row[7]),
            "payload": payload if isinstance(payload, dict) else {},
        })
    return out


def _current_selection(rows: list[dict[str, Any]], now: datetime) -> dict[str, Any] | None:
    eligible = []
    for row in rows:
        try:
            start = _dt(row["decision_start"])
        except Exception:
            continue
        if start <= now < start + timedelta(minutes=15):
            eligible.append(row)
    if not eligible:
        return None
    return max(eligible, key=lambda x: (_dt(x["decision_start"]), _dt(x["created_at"])))


def _next_selection(rows: list[dict[str, Any]], now: datetime) -> dict[str, Any] | None:
    future = []
    for row in rows:
        try:
            start = _dt(row["decision_start"])
        except Exception:
            continue
        if start > now and row.get("requested_action_kw") is not None:
            future.append(row)
    if not future:
        return None
    first_start = min(_dt(row["decision_start"]) for row in future)
    same_start = [row for row in future if _dt(row["decision_start"]) == first_start]
    return max(same_start, key=lambda x: _dt(x["created_at"]))


def _current_effective_command(now: datetime) -> dict[str, Any] | None:
    try:
        with sqlite3.connect(DB_PATH, timeout=20) as c:
            rows = c.execute(
                '''SELECT command_id,created_at,source,source_id,engine_id,decision_start,
                          valid_until,requested_action_kw,safe_action_kw,status,reason,payload_json
                   FROM actuator_command
                   WHERE physical_write=1 AND status IN ('acknowledged','held_existing')
                   ORDER BY command_id DESC LIMIT 32'''
            ).fetchall()
    except sqlite3.OperationalError:
        return None
    for row in rows:
        if row[5] is None or row[6] is None:
            continue
        try:
            start, end = _dt(str(row[5])), _dt(str(row[6]))
        except Exception:
            continue
        if not (start <= now < end):
            continue
        try:
            payload = json.loads(row[11] or "{}")
        except Exception:
            payload = {}
        return {
            "command_id": int(row[0]),
            "created_at": str(row[1]),
            "source": str(row[2]),
            "source_id": row[3],
            "engine_id": None if row[4] is None else str(row[4]),
            "decision_start": str(row[5]),
            "valid_until": str(row[6]),
            "requested_action_kw": None if row[7] is None else float(row[7]),
            "safe_action_kw": None if row[8] is None else float(row[8]),
            "status": str(row[9]),
            "reason": str(row[10]),
            "payload": payload if isinstance(payload, dict) else {},
        }
    return None


def _engine_plan_rows(decision_id: str | None) -> list[dict[str, Any]]:
    if not decision_id:
        return []
    try:
        with sqlite3.connect(DB_PATH, timeout=20) as c:
            row = c.execute(
                "SELECT payload_json FROM engine_decision WHERE decision_id=? LIMIT 1",
                (str(decision_id),),
            ).fetchone()
    except sqlite3.OperationalError:
        return []
    if not row:
        return []
    try:
        payload = json.loads(row[0] or "{}")
    except Exception:
        return []
    plan_rows = payload.get("plan_rows") if isinstance(payload, dict) else None
    return [dict(x) for x in (plan_rows or []) if isinstance(x, dict)]


def _next_from_plan(
    selection: dict[str, Any] | None,
    now: datetime,
    current_action_kw: float | None,
) -> dict[str, Any] | None:
    if not selection:
        return None
    rows = _engine_plan_rows(selection.get("decision_id"))
    if not rows:
        return None
    base = float(current_action_kw or 0.0)
    candidates = []
    for row in rows:
        raw_start = row.get("start") or row.get("start_utc")
        raw_action = row.get("requested_action_kw", row.get("battery_action_kw", row.get("action_kw")))
        if raw_start is None or raw_action is None:
            continue
        try:
            stamp, action = _dt(str(raw_start)), float(raw_action)
        except Exception:
            continue
        if stamp <= now:
            continue
        if abs(action - base) <= 0.25:
            continue
        candidates.append((stamp, action))
    if not candidates:
        return None
    stamp, action = min(candidates, key=lambda x: x[0])
    engine_id = selection.get("routed_engine_id")
    return _item(
        start=stamp.isoformat(),
        action_kw=action,
        engine_id=engine_id,
        reason=f"Selected-engine horizon from {engine_id or 'routed control'}.",
        reason_code="selected_engine_horizon_change",
        requested_action_kw=action,
        source="engine_decision_plan",
    )


def decision_summary() -> dict[str, Any]:
    """Production overview sourced from control truth, not the base optimizer plan.

    In Active mode the current value is the effective actuator command for the
    current interval. In Shadow it is the routed selector decision. The next
    value prefers the freshest routed future selection and only falls back to the
    selected engine's own horizon. This keeps the operator UI aligned with what
    can actually reach the inverter.
    """
    now = datetime.now(timezone.utc)
    production = production_status()
    selections = _selection_rows()
    selected = _current_selection(selections, now)
    effective = _current_effective_command(now)
    active = bool(
        production.get("operating_mode") == "active"
        and production.get("physical_writes_enabled")
    )

    current: dict[str, Any] | None = None
    current_action: float | None = None
    if active and effective and effective.get("safe_action_kw") is not None:
        current_action = float(effective["safe_action_kw"])
        engine_id = effective.get("engine_id")
        held = effective.get("status") == "held_existing"
        reason = (
            f"Applied {engine_id or 'routed'} target; previous target retained within the actuator change threshold."
            if held
            else f"Applied {engine_id or 'routed'} target after actuator safety."
        )
        current = _item(
            start=effective["decision_start"],
            action_kw=current_action,
            engine_id=engine_id,
            reason=reason,
            reason_code="effective_actuator_target",
            requested_action_kw=effective.get("requested_action_kw"),
            safe_action_kw=effective.get("safe_action_kw"),
            source=effective.get("source"),
        )
    elif selected and selected.get("requested_action_kw") is not None:
        current_action = float(selected["requested_action_kw"])
        engine_id = selected.get("routed_engine_id")
        current = _item(
            start=selected["decision_start"],
            action_kw=current_action,
            engine_id=engine_id,
            reason=f"Routed selector decision from {engine_id or 'control selector'}."
            + (" Fallback is active." if selected.get("fallback_used") else ""),
            reason_code="routed_selector_decision",
            requested_action_kw=current_action,
            source="engine_control_selection",
        )

    future = _next_selection(selections, now)
    nxt: dict[str, Any] | None = None
    if future and future.get("requested_action_kw") is not None:
        action = float(future["requested_action_kw"])
        # Keep the UI's old semantics: only call it a planned change when the
        # action materially differs from the current target.
        if current_action is None or abs(action - current_action) > 0.25:
            engine_id = future.get("routed_engine_id")
            nxt = _item(
                start=future["decision_start"],
                action_kw=action,
                engine_id=engine_id,
                reason=f"Fresh routed future decision from {engine_id or 'control selector'}; dispatch waits for decision start.",
                reason_code="routed_future_decision",
                requested_action_kw=action,
                source="engine_control_selection",
            )
    if nxt is None:
        nxt = _next_from_plan(selected, now, current_action)

    return {
        "generated_at": now.isoformat(),
        "source": "control_truth_v1",
        "production_active": active,
        "current": current,
        "next": nxt,
    }
