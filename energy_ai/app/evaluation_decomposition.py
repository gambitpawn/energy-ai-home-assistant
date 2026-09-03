from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .db import connect_db
from .optimizer_evaluation import _day_bounds
from .regret_decomposition import ENGINE_NAME as REGRET_ENGINE_NAME, regret_decomposition


ARTIFACT_SCHEMA = "evaluation_decomposition_v1"
_RETRY_AFTER_HOURS = 6


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def config_fingerprint(cfg: dict[str, Any]) -> str:
    relevant = {
        "policy": cfg.get("policy") or {},
        "optimizer": cfg.get("optimizer") or {},
        "tariffs": cfg.get("tariffs") or {},
    }
    raw = json.dumps(relevant, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _ensure_table() -> None:
    with connect_db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS optimizer_evaluation_decomposition(
                local_date TEXT NOT NULL,
                source_created_at TEXT NOT NULL,
                config_fingerprint TEXT NOT NULL,
                engine TEXT NOT NULL,
                artifact_schema TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                next_retry_after TEXT,
                payload_json TEXT,
                error TEXT,
                PRIMARY KEY(local_date, source_created_at, config_fingerprint, engine, artifact_schema)
            )"""
        )


def _normalized(raw: dict[str, Any]) -> dict[str, Any]:
    valid = bool(raw.get("valid_decomposition")) and raw.get("status") == "valid"
    d = raw.get("decomposition") or {}
    return {
        "status": "complete" if valid else str(raw.get("status") or "unavailable"),
        "valid": valid,
        "forecast_gap_sek": d.get("forecast_regret_sek") if valid else None,
        "future_price_horizon_gap_sek": d.get("price_information_regret_sek") if valid else None,
        "planner_policy_gap_sek": d.get("planner_horizon_policy_residual_sek") if valid else None,
        "total_gap_sek": d.get("realtime_to_hindsight_total_gap_sek") if valid else None,
        "definitions": {
            "forecast_gap": "Cost of forecast PV/load versus realized PV/load with historical price availability unchanged.",
            "future_price_horizon_gap": "Value of prices that had not yet been published at decision time; already published prices do not change.",
            "planner_policy_gap": "Residual from rolling horizon, terminal value, reserve and policy choices after perfect load, PV and price information.",
        },
    }


def _stored_evaluations(limit: int) -> list[dict[str, Any]]:
    with connect_db(timeout=5.0) as c:
        rows = c.execute(
            "SELECT local_date,created_at,payload_json FROM optimizer_day_eval ORDER BY local_date DESC LIMIT ?",
            (max(1, min(180, int(limit))),),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for local_date, created_at, payload_raw in rows:
        try:
            payload = json.loads(payload_raw)
        except Exception:
            continue
        out.append(
            {
                "local_date": str(local_date),
                "source_created_at": str(created_at),
                "status": payload.get("status"),
            }
        )
    return out


def _artifact_row(local_date: str, source_created_at: str, fingerprint: str) -> dict[str, Any] | None:
    try:
        with connect_db(timeout=5.0) as c:
            row = c.execute(
                """SELECT status,attempts,updated_at,next_retry_after,payload_json,error
                   FROM optimizer_evaluation_decomposition
                   WHERE local_date=? AND source_created_at=? AND config_fingerprint=?
                     AND engine=? AND artifact_schema=?""",
                (local_date, source_created_at, fingerprint, REGRET_ENGINE_NAME, ARTIFACT_SCHEMA),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    payload = None
    if row[4]:
        try:
            payload = json.loads(row[4])
        except Exception:
            payload = None
    return {
        "status": row[0],
        "attempts": int(row[1] or 0),
        "updated_at": row[2],
        "next_retry_after": row[3],
        "payload": payload,
        "error": row[5],
    }


def _retry_due(artifact: dict[str, Any] | None, now: datetime) -> bool:
    if artifact is None:
        return True
    if artifact.get("status") == "complete":
        return False
    raw = artifact.get("next_retry_after")
    if not raw:
        return True
    try:
        due = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        return now >= due.astimezone(timezone.utc)
    except Exception:
        return True


def _store_artifact(
    *,
    local_date: str,
    source_created_at: str,
    fingerprint: str,
    status: str,
    attempts: int,
    payload: dict[str, Any] | None,
    error: str | None,
    retry: bool,
) -> None:
    now = _now()
    next_retry = _iso(now + timedelta(hours=_RETRY_AFTER_HOURS)) if retry else None
    with connect_db() as c:
        c.execute(
            """INSERT OR REPLACE INTO optimizer_evaluation_decomposition(
                local_date,source_created_at,config_fingerprint,engine,artifact_schema,status,
                attempts,updated_at,next_retry_after,payload_json,error
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                local_date,
                source_created_at,
                fingerprint,
                REGRET_ENGINE_NAME,
                ARTIFACT_SCHEMA,
                status,
                int(attempts),
                _iso(now),
                next_retry,
                None if payload is None else json.dumps(payload, ensure_ascii=False),
                error,
            ),
        )


def run_pending_evaluation_decomposition(cfg: dict[str, Any], max_days: int = 1) -> dict[str, Any]:
    """Evaluate at most ``max_days`` persisted complete days.

    This function is intentionally not called from UI routes. Runtime maintenance
    executes it through the shared low-priority worker so decomposition cannot
    become a prerequisite for application startup, arming or control planning.
    """
    _ensure_table()
    fingerprint = config_fingerprint(cfg)
    now = _now()
    candidates = []
    for item in reversed(_stored_evaluations(30)):
        if item.get("status") != "ok":
            continue
        artifact = _artifact_row(item["local_date"], item["source_created_at"], fingerprint)
        if _retry_due(artifact, now):
            candidates.append((item, artifact))
    processed: list[dict[str, Any]] = []
    for item, previous in candidates[: max(0, int(max_days))]:
        local_date = item["local_date"]
        attempts = int((previous or {}).get("attempts") or 0) + 1
        start, end = _day_bounds(date.fromisoformat(local_date))
        try:
            raw = regret_decomposition(cfg, start=start.isoformat(), end=end.isoformat(), include_rows=False)
            normalized = _normalized(raw)
            if normalized["valid"]:
                _store_artifact(
                    local_date=local_date,
                    source_created_at=item["source_created_at"],
                    fingerprint=fingerprint,
                    status="complete",
                    attempts=attempts,
                    payload=normalized,
                    error=None,
                    retry=False,
                )
                processed.append({"local_date": local_date, "status": "complete"})
            else:
                raw_status = str(raw.get("status") or "unavailable")
                retryable = raw_status in {"insufficient_future_actual_coverage", "insufficient_actual_coverage", "unavailable"}
                _store_artifact(
                    local_date=local_date,
                    source_created_at=item["source_created_at"],
                    fingerprint=fingerprint,
                    status="pending" if retryable else "failed",
                    attempts=attempts,
                    payload=normalized,
                    error=None,
                    retry=retryable,
                )
                processed.append({"local_date": local_date, "status": "pending" if retryable else "failed", "detail": raw_status})
        except Exception as exc:
            _store_artifact(
                local_date=local_date,
                source_created_at=item["source_created_at"],
                fingerprint=fingerprint,
                status="failed",
                attempts=attempts,
                payload=None,
                error=repr(exc),
                retry=True,
            )
            processed.append({"local_date": local_date, "status": "failed", "error": repr(exc)})
    return {
        "artifact_schema": ARTIFACT_SCHEMA,
        "engine": REGRET_ENGINE_NAME,
        "processed": processed,
        "processed_count": len(processed),
        "candidate_count": len(candidates),
        "max_days": max(0, int(max_days)),
    }


def decomposition_history(cfg: dict[str, Any], days: int = 30) -> dict[str, Any]:
    """Read persisted decomposition state only; never calculate decomposition."""
    fingerprint = config_fingerprint(cfg)
    evaluations = list(reversed(_stored_evaluations(days)))
    result = []
    for item in evaluations:
        if item.get("status") != "ok":
            result.append({"local_date": item["local_date"], "status": "not_applicable", "valid": False})
            continue
        artifact = _artifact_row(item["local_date"], item["source_created_at"], fingerprint)
        if artifact is None:
            result.append({"local_date": item["local_date"], "status": "pending", "valid": False})
            continue
        payload = artifact.get("payload") or {}
        result.append(
            {
                "local_date": item["local_date"],
                "status": artifact.get("status") or "pending",
                "valid": bool(payload.get("valid")) and artifact.get("status") == "complete",
                "forecast_gap_sek": payload.get("forecast_gap_sek"),
                "future_price_horizon_gap_sek": payload.get("future_price_horizon_gap_sek"),
                "planner_policy_gap_sek": payload.get("planner_policy_gap_sek"),
                "total_gap_sek": payload.get("total_gap_sek"),
                "updated_at": artifact.get("updated_at"),
                "attempts": artifact.get("attempts"),
                "error": artifact.get("error") if artifact.get("status") == "failed" else None,
            }
        )
    return {
        "artifact_schema": ARTIFACT_SCHEMA,
        "engine": REGRET_ENGINE_NAME,
        "window_days": int(days),
        "days": result,
        "complete_days": sum(1 for x in result if x.get("valid")),
        "pending_days": sum(1 for x in result if x.get("status") == "pending"),
        "failed_days": sum(1 for x in result if x.get("status") == "failed"),
    }
