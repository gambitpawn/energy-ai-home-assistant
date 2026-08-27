from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from .db import DB_PATH
from .tariff_scenarios import LOCAL_TZ, _calendar_active, _tariff_metric_from_hourly


def _utc(value: str) -> datetime:
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _quarter_start(value: datetime) -> datetime:
    value = value.astimezone(timezone.utc)
    return value.replace(minute=(value.minute // 15) * 15, second=0, microsecond=0)


def _num(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _tariff(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    return dict(((cfg.get("tariffs") or {}).get(name) or {}))


def _enabled(cfg: dict[str, Any], tariff: dict[str, Any]) -> bool:
    return bool((cfg.get("tariffs") or {}).get("enabled")) and bool(tariff.get("enabled"))


def _state_cutoff(decision_start: datetime) -> datetime:
    # For live/future decisions, never use the currently collecting partial quarter.
    # For historical decisions, decision_start itself is the causal cutoff.
    now_complete_until = _quarter_start(datetime.now(timezone.utc))
    return min(decision_start.astimezone(timezone.utc), now_complete_until)


def _state_rows(decision_start: datetime) -> tuple[list[tuple[datetime, float, float]], datetime]:
    cutoff = _state_cutoff(decision_start)
    local = decision_start.astimezone(LOCAL_TZ)
    month_start_local = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_start = month_start_local.astimezone(timezone.utc)
    try:
        with sqlite3.connect(DB_PATH) as c:
            rows = c.execute(
                "SELECT bucket_start,payload_json FROM state_15m WHERE bucket_start>=? AND bucket_start<? ORDER BY bucket_start",
                (month_start.isoformat(), cutoff.isoformat()),
            ).fetchall()
    except sqlite3.OperationalError:
        return [], cutoff
    out: list[tuple[datetime, float, float]] = []
    for stamp_raw, payload_raw in rows:
        try:
            stamp = _utc(str(stamp_raw))
            payload = json.loads(payload_raw)
            raw_grid = _num((payload.get("mean") or {}).get("grid_power_kw"))
        except Exception:
            continue
        if raw_grid is None:
            continue
        # Stored Solinteg convention: positive export, negative import.
        grid_import = max(0.0, -raw_grid)
        grid_export = max(0.0, raw_grid)
        out.append((stamp, grid_import, grid_export))
    return out, cutoff


def _hourly_values(
    rows: list[tuple[datetime, float, float]],
    tariff: dict[str, Any],
    *,
    export: bool,
    decision_start: datetime,
) -> tuple[list[float], float, int]:
    groups: dict[tuple[str, int], list[tuple[datetime, float]]] = defaultdict(list)
    current_local = decision_start.astimezone(LOCAL_TZ)
    current_key = (current_local.date().isoformat(), current_local.hour)
    for stamp, imp, exp in rows:
        local = stamp.astimezone(LOCAL_TZ)
        if not _calendar_active(local, tariff, False):
            continue
        key = (local.date().isoformat(), local.hour)
        groups[key].append((stamp, exp if export else imp))

    completed: list[float] = []
    current_values: list[float] = []
    for key, values in groups.items():
        values = sorted(values, key=lambda x: x[0])
        if key == current_key:
            current_values = [v for _, v in values]
            continue
        local_minutes = [stamp.astimezone(LOCAL_TZ).minute for stamp, _ in values]
        if len(values) == 4 and local_minutes == [0, 15, 30, 45]:
            completed.append(sum(v for _, v in values) / 4.0)
    current_avg = sum(current_values) / len(current_values) if current_values else 0.0
    return completed, current_avg, len(current_values)


def _calendar_flags(decision_start: datetime, tariff: dict[str, Any], enabled: bool) -> dict[str, Any]:
    local = decision_start.astimezone(LOCAL_TZ)
    months = {int(x) for x in (tariff.get("active_months") or [])}
    month_active = not months or local.month in months
    # Evaluate the day rule independently of the hour by probing a configured active hour.
    probe_hour = int(tariff.get("start_hour", local.hour))
    probe = local.replace(hour=max(0, min(23, probe_hour)), minute=0, second=0, microsecond=0)
    day_active = _calendar_active(probe, {**tariff, "start_hour": probe.hour, "end_hour": min(24, probe.hour + 1)}, False) if month_active else False
    return {
        "enabled": enabled,
        "active_month_at_decision": bool(enabled and month_active),
        "active_day_at_decision": bool(enabled and month_active and day_active),
        "active_at_decision": bool(enabled and _calendar_active(local, tariff, False)),
    }


def tariff_state_for_decision(cfg: dict[str, Any], decision_start: str) -> dict[str, Any]:
    decision = _utc(decision_start)
    rows, cutoff = _state_rows(decision)
    result: dict[str, Any] = {
        "as_of": decision.isoformat(),
        "actual_complete_until": cutoff.isoformat(),
        "grid_sign_convention": "state_15m raw Solinteg positive export / negative import; tariff state normalizes import/export positive",
    }

    for name, export in (("consumption_demand", False), ("production_demand", True)):
        tariff = _tariff(cfg, name)
        enabled = _enabled(cfg, tariff)
        completed, current_avg, elapsed = _hourly_values(rows, tariff, export=export, decision_start=decision)
        metric = _tariff_metric_from_hourly(completed, tariff, []) if tariff.get("kind") in {"import_top3_mean", "export_max_hour"} else {"metric_kw": 0.0, "top_values_kw": []}
        top = [float(v) for v in metric.get("top_values_kw") or []]
        if name == "consumption_demand":
            top = sorted(completed, reverse=True)[: max(3, int(tariff.get("top_n") or 3))]
        result[name] = {
            **_calendar_flags(decision, tariff, enabled),
            "completed_active_hours": len(completed),
            "historical_metric_kw": float(metric.get("metric_kw") or 0.0),
            "historical_top_values_kw": top,
            "current_clock_hour_average_kw_so_far": float(current_avg),
            "current_clock_hour_quarters_elapsed": int(elapsed),
        }
    return result
