from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from statistics import mean, median
from typing import Any

from . import model_selector as ms

POLICY_VERSION = "robust10_v1"
QUALIFICATION_DAYS = 10
QUALIFICATION_WIN_DAYS = 7
QUARANTINE_DAYS = 7
MIN_DAILY_INTERVALS = 92
LIVE_WINDOW_SELECTIONS = 20
LIVE_MIN_RATE_SELECTIONS = 10
LIVE_MAX_FALLBACK_RATE = 0.20
LIVE_MAX_CONSECUTIVE_FAULTS = 3
LIVE_SAFETY_REJECTS_IN_FIVE = 3
ENVELOPE_TOLERANCE_KW = 0.25
GROSS_ACTION_MULTIPLE = 2.0
ADAPTIVE_ENGINE_ID = "adaptive_deterministic_v1"
BASELINE_MODEL_KEY = "deterministic_v35:3.5"

_INSTALLED = False
_ORIGINAL_CONTEXT_SIGNATURE = None
_ORIGINAL_EVALUATE_DAY = None
_ORIGINAL_SELECTOR_STATUS = None


def _init_tables() -> None:
    ms._init_tables()
    with sqlite3.connect(ms.DB_PATH, timeout=20) as c:
        c.executescript(
            '''
            CREATE TABLE IF NOT EXISTS engine_model_generation(
                engine_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                source_state_id INTEGER,
                reason TEXT NOT NULL,
                PRIMARY KEY(engine_id,generation)
            );

            CREATE TABLE IF NOT EXISTS engine_model_disqualification(
                disqualification_id INTEGER PRIMARY KEY AUTOINCREMENT,
                context_signature TEXT NOT NULL,
                engine_id TEXT NOT NULL,
                model_key TEXT NOT NULL,
                disqualified_at TEXT NOT NULL,
                quarantine_until TEXT NOT NULL,
                reason TEXT NOT NULL,
                details_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_model_disqualification_lookup
                ON engine_model_disqualification(context_signature,engine_id,model_key,disqualified_at DESC);

            CREATE TABLE IF NOT EXISTS engine_model_health_event(
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                decision_start TEXT NOT NULL,
                context_signature TEXT NOT NULL,
                engine_id TEXT NOT NULL,
                model_key TEXT NOT NULL,
                status TEXT NOT NULL,
                fault_type TEXT,
                fallback_used INTEGER NOT NULL,
                details_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_model_health_lookup
                ON engine_model_health_event(context_signature,engine_id,model_key,event_id DESC);

            CREATE TABLE IF NOT EXISTS engine_selector_robust_state(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                context_signature TEXT NOT NULL,
                selected_engine_id TEXT NOT NULL,
                selected_model_key TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            '''
        )
    _ensure_adaptive_generation()


def _latest_adaptive_state_id() -> int | None:
    try:
        with sqlite3.connect(ms.DB_PATH, timeout=20) as c:
            row = c.execute(
                "SELECT state_id FROM adaptive_parameter_state WHERE role='candidate' ORDER BY state_id DESC LIMIT 1"
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    return None if not row else int(row[0])


def _ensure_adaptive_generation() -> dict[str, Any]:
    with sqlite3.connect(ms.DB_PATH, timeout=20) as c:
        row = c.execute(
            '''SELECT generation,started_at,ended_at,source_state_id,reason
               FROM engine_model_generation WHERE engine_id=? ORDER BY generation DESC LIMIT 1''',
            (ADAPTIVE_ENGINE_ID,),
        ).fetchone()
        if row is None:
            now = ms._now()
            source_state_id = _latest_adaptive_state_id()
            c.execute(
                '''INSERT INTO engine_model_generation(
                   engine_id,generation,started_at,ended_at,source_state_id,reason)
                   VALUES (?,?,?,?,?,?)''',
                (ADAPTIVE_ENGINE_ID, 1, now, None, source_state_id, "selector_policy_initial_generation"),
            )
            row = (1, now, None, source_state_id, "selector_policy_initial_generation")
    return {
        "generation": int(row[0]),
        "started_at": str(row[1]),
        "ended_at": row[2],
        "source_state_id": None if row[3] is None else int(row[3]),
        "reason": str(row[4]),
    }


def _adaptive_generation_for_time(value: str | None = None) -> int:
    _ensure_adaptive_generation()
    target = ms._dt(value) if value else datetime.now(timezone.utc)
    with sqlite3.connect(ms.DB_PATH, timeout=20) as c:
        rows = c.execute(
            '''SELECT generation,started_at,ended_at FROM engine_model_generation
               WHERE engine_id=? ORDER BY generation DESC''',
            (ADAPTIVE_ENGINE_ID,),
        ).fetchall()
    for generation, started_at, ended_at in rows:
        start = ms._dt(str(started_at))
        end = None if ended_at is None else ms._dt(str(ended_at))
        if start <= target and (end is None or target < end):
            return int(generation)
    return int(rows[-1][0]) if rows else 1


def _latest_disqualification(context: str, engine_id: str, model_key: str) -> dict[str, Any] | None:
    _init_tables_shallow()
    with sqlite3.connect(ms.DB_PATH, timeout=20) as c:
        row = c.execute(
            '''SELECT disqualified_at,quarantine_until,reason,details_json
               FROM engine_model_disqualification
               WHERE context_signature=? AND engine_id=? AND model_key=?
               ORDER BY disqualification_id DESC LIMIT 1''',
            (context, engine_id, model_key),
        ).fetchone()
    if not row:
        return None
    return {
        "disqualified_at": str(row[0]),
        "quarantine_until": str(row[1]),
        "reason": str(row[2]),
        "details": json.loads(row[3]),
    }


def _init_tables_shallow() -> None:
    # Avoid recursive _ensure_adaptive_generation() calls from lookup helpers.
    ms._init_tables()
    with sqlite3.connect(ms.DB_PATH, timeout=20) as c:
        c.executescript(
            '''
            CREATE TABLE IF NOT EXISTS engine_model_generation(
                engine_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                source_state_id INTEGER,
                reason TEXT NOT NULL,
                PRIMARY KEY(engine_id,generation)
            );
            CREATE TABLE IF NOT EXISTS engine_model_disqualification(
                disqualification_id INTEGER PRIMARY KEY AUTOINCREMENT,
                context_signature TEXT NOT NULL,
                engine_id TEXT NOT NULL,
                model_key TEXT NOT NULL,
                disqualified_at TEXT NOT NULL,
                quarantine_until TEXT NOT NULL,
                reason TEXT NOT NULL,
                details_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_model_disqualification_lookup
                ON engine_model_disqualification(context_signature,engine_id,model_key,disqualified_at DESC);
            CREATE TABLE IF NOT EXISTS engine_model_health_event(
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                decision_start TEXT NOT NULL,
                context_signature TEXT NOT NULL,
                engine_id TEXT NOT NULL,
                model_key TEXT NOT NULL,
                status TEXT NOT NULL,
                fault_type TEXT,
                fallback_used INTEGER NOT NULL,
                details_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_model_health_lookup
                ON engine_model_health_event(context_signature,engine_id,model_key,event_id DESC);
            CREATE TABLE IF NOT EXISTS engine_selector_robust_state(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                context_signature TEXT NOT NULL,
                selected_engine_id TEXT NOT NULL,
                selected_model_key TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            '''
        )


def _policy_context_signature(cfg: dict[str, Any]) -> str:
    if _ORIGINAL_CONTEXT_SIGNATURE is None:
        raise RuntimeError("robust selector patch not installed")
    base = str(_ORIGINAL_CONTEXT_SIGNATURE(cfg))
    return hashlib.sha256(f"{base}|selector_policy={POLICY_VERSION}".encode("utf-8")).hexdigest()


def _engine_model_key(engine_id: str, decision: dict[str, Any] | None, decision_start: str | None = None) -> str:
    engine_id = str(engine_id)
    if engine_id == ms.BASELINE_ENGINE_ID:
        return BASELINE_MODEL_KEY
    if engine_id == ADAPTIVE_ENGINE_ID:
        generation = _adaptive_generation_for_time(decision_start)
        return f"{ADAPTIVE_ENGINE_ID}:generation-{generation}"
    payload = decision or {}
    model = payload.get("model") or {}
    identity = (
        model.get("model_id")
        or model.get("model_revision")
        or model.get("model_version")
        or payload.get("engine_version")
    )
    if identity is None:
        raw = json.dumps(model, sort_keys=True, separators=(",", ":"), default=str)
        identity = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{engine_id}:{identity}"


def _current_model_key(engine_id: str) -> str | None:
    engine_id = str(engine_id)
    if engine_id == ms.BASELINE_ENGINE_ID:
        return BASELINE_MODEL_KEY
    if engine_id == ADAPTIVE_ENGINE_ID:
        _maybe_advance_adaptive_generation(ms.ensure_selector_state({}).get("context_signature") if False else None)
        return f"{ADAPTIVE_ENGINE_ID}:generation-{_adaptive_generation_for_time()}"
    try:
        with sqlite3.connect(ms.DB_PATH, timeout=20) as c:
            row = c.execute(
                '''SELECT decision_start,payload_json FROM engine_decision
                   WHERE engine_id=? AND status='ok'
                   ORDER BY decision_start DESC,generated_at DESC LIMIT 1''',
                (engine_id,),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    try:
        payload = json.loads(row[1])
    except Exception:
        payload = {}
    return _engine_model_key(engine_id, payload, str(row[0]))


def _maybe_advance_adaptive_generation(context: str | None) -> dict[str, Any]:
    current = _ensure_adaptive_generation()
    current_key = f"{ADAPTIVE_ENGINE_ID}:generation-{current['generation']}"
    if not context:
        return current
    disqualification = _latest_disqualification(context, ADAPTIVE_ENGINE_ID, current_key)
    if disqualification is None:
        return current
    latest_state_id = _latest_adaptive_state_id()
    source_state_id = current.get("source_state_id")
    if latest_state_id is None or (source_state_id is not None and latest_state_id <= source_state_id):
        return current

    now = ms._now()
    with sqlite3.connect(ms.DB_PATH, timeout=20) as c:
        active = c.execute(
            '''SELECT generation,source_state_id FROM engine_model_generation
               WHERE engine_id=? AND ended_at IS NULL ORDER BY generation DESC LIMIT 1''',
            (ADAPTIVE_ENGINE_ID,),
        ).fetchone()
        if not active or int(active[0]) != int(current["generation"]):
            return _ensure_adaptive_generation()
        c.execute(
            '''UPDATE engine_model_generation SET ended_at=?
               WHERE engine_id=? AND generation=?''',
            (now, ADAPTIVE_ENGINE_ID, int(current["generation"])),
        )
        new_generation = int(current["generation"]) + 1
        c.execute(
            '''INSERT INTO engine_model_generation(
               engine_id,generation,started_at,ended_at,source_state_id,reason)
               VALUES (?,?,?,?,?,?)''',
            (
                ADAPTIVE_ENGINE_ID,
                new_generation,
                now,
                None,
                latest_state_id,
                "new_candidate_after_disqualification",
            ),
        )
    ms._event(
        "model_generation_advanced",
        context,
        "Adaptive candidate changed after disqualification; start a new qualification generation.",
        from_engine_id=ADAPTIVE_ENGINE_ID,
        to_engine_id=ADAPTIVE_ENGINE_ID,
        payload={
            "previous_model_key": current_key,
            "new_model_key": f"{ADAPTIVE_ENGINE_ID}:generation-{new_generation}",
            "source_state_id": latest_state_id,
        },
    )
    return _ensure_adaptive_generation()


def _ensure_robust_state(cfg: dict[str, Any]) -> dict[str, Any]:
    _init_tables()
    base_state = ms.ensure_selector_state(cfg)
    context = base_state["context_signature"]
    _maybe_advance_adaptive_generation(context)
    expected_engine = str(base_state["selected_engine_id"])
    expected_key = BASELINE_MODEL_KEY if expected_engine == ms.BASELINE_ENGINE_ID else None

    with sqlite3.connect(ms.DB_PATH, timeout=20) as c:
        row = c.execute(
            '''SELECT context_signature,selected_engine_id,selected_model_key,updated_at
               FROM engine_selector_robust_state WHERE singleton=1'''
        ).fetchone()
        if row is None or str(row[0]) != context:
            now = ms._now()
            c.execute(
                '''INSERT INTO engine_selector_robust_state(
                   singleton,context_signature,selected_engine_id,selected_model_key,updated_at)
                   VALUES (1,?,?,?,?)
                   ON CONFLICT(singleton) DO UPDATE SET
                     context_signature=excluded.context_signature,
                     selected_engine_id=excluded.selected_engine_id,
                     selected_model_key=excluded.selected_model_key,
                     updated_at=excluded.updated_at''',
                (context, ms.BASELINE_ENGINE_ID, BASELINE_MODEL_KEY, now),
            )
            row = (context, ms.BASELINE_ENGINE_ID, BASELINE_MODEL_KEY, now)
        elif expected_engine == ms.BASELINE_ENGINE_ID and str(row[1]) != ms.BASELINE_ENGINE_ID:
            now = ms._now()
            c.execute(
                '''UPDATE engine_selector_robust_state SET selected_engine_id=?,selected_model_key=?,updated_at=?
                   WHERE singleton=1''',
                (ms.BASELINE_ENGINE_ID, BASELINE_MODEL_KEY, now),
            )
            row = (context, ms.BASELINE_ENGINE_ID, BASELINE_MODEL_KEY, now)
        elif str(row[1]) != expected_engine:
            # Defensive reconciliation if a legacy path changed only the base selector state.
            now = ms._now()
            reconciled_key = expected_key or _current_model_key(expected_engine) or f"{expected_engine}:unknown"
            c.execute(
                '''UPDATE engine_selector_robust_state SET selected_engine_id=?,selected_model_key=?,updated_at=?
                   WHERE singleton=1''',
                (expected_engine, reconciled_key, now),
            )
            row = (context, expected_engine, reconciled_key, now)
    return {
        "context_signature": str(row[0]),
        "selected_engine_id": str(row[1]),
        "selected_model_key": str(row[2]),
        "updated_at": str(row[3]),
    }


def _set_selected_model(
    cfg: dict[str, Any],
    engine_id: str,
    model_key: str,
    reason: str,
    event_type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    base_state = ms._set_selected_engine(cfg, engine_id, reason, event_type, payload)
    context = base_state["context_signature"]
    with sqlite3.connect(ms.DB_PATH, timeout=20) as c:
        c.execute(
            '''INSERT INTO engine_selector_robust_state(
               singleton,context_signature,selected_engine_id,selected_model_key,updated_at)
               VALUES (1,?,?,?,?)
               ON CONFLICT(singleton) DO UPDATE SET
                 context_signature=excluded.context_signature,
                 selected_engine_id=excluded.selected_engine_id,
                 selected_model_key=excluded.selected_model_key,
                 updated_at=excluded.updated_at''',
            (context, str(engine_id), str(model_key), ms._now()),
        )
    return {**base_state, "selected_model_key": str(model_key)}


def _disqualify_model(
    cfg: dict[str, Any],
    engine_id: str,
    model_key: str,
    reason: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    state = _ensure_robust_state(cfg)
    context = state["context_signature"]
    now_dt = datetime.now(timezone.utc)
    quarantine_until = now_dt + timedelta(days=QUARANTINE_DAYS)
    with sqlite3.connect(ms.DB_PATH, timeout=20) as c:
        c.execute(
            '''INSERT INTO engine_model_disqualification(
               context_signature,engine_id,model_key,disqualified_at,quarantine_until,reason,details_json)
               VALUES (?,?,?,?,?,?,?)''',
            (
                context,
                str(engine_id),
                str(model_key),
                now_dt.isoformat(),
                quarantine_until.isoformat(),
                reason,
                json.dumps(details, ensure_ascii=False, sort_keys=True),
            ),
        )
    ms._event(
        "model_disqualified",
        context,
        reason,
        from_engine_id=str(engine_id),
        to_engine_id=ms.BASELINE_ENGINE_ID,
        payload={
            "model_key": model_key,
            "quarantine_until": quarantine_until.isoformat(),
            **details,
        },
    )
    selected = state["selected_engine_id"] == str(engine_id) and state["selected_model_key"] == str(model_key)
    if selected:
        _set_selected_model(
            cfg,
            ms.BASELINE_ENGINE_ID,
            BASELINE_MODEL_KEY,
            "selected model was disqualified by live circuit breaker",
            "rollback_disqualification",
            {"model_key": model_key, "reason": reason},
        )
    return {
        "engine_id": str(engine_id),
        "model_key": str(model_key),
        "disqualified_at": now_dt.isoformat(),
        "quarantine_until": quarantine_until.isoformat(),
        "reason": reason,
        "selected_model_rolled_back": selected,
    }


def _disqualification_status(context: str, engine_id: str, model_key: str) -> dict[str, Any]:
    item = _latest_disqualification(context, engine_id, model_key)
    if item is None:
        return {"disqualified_before": False, "quarantine_active": False, "qualification_not_before": None}
    quarantine_until = ms._dt(item["quarantine_until"])
    quarantine_active = quarantine_until > datetime.now(timezone.utc)
    # Same revision must collect ten complete days after quarantine. If the
    # revision changes, the model_key changes and this restriction no longer applies.
    first_full_day = quarantine_until.astimezone(ms.LOCAL_TZ).date() + timedelta(days=1)
    return {
        "disqualified_before": True,
        "quarantine_active": quarantine_active,
        "qualification_not_before": first_full_day.isoformat(),
        **item,
    }


def _annotate_day_model_keys(local_date: str, context: str) -> dict[str, Any]:
    day = date.fromisoformat(str(local_date))
    vintages = ms._competition_vintages(day)
    keys_by_engine: dict[str, set[str]] = {}
    for item in vintages:
        decision_start = str(item.get("decision_start") or "")
        for engine_id, decision in (item.get("decisions") or {}).items():
            keys_by_engine.setdefault(str(engine_id), set()).add(
                _engine_model_key(str(engine_id), decision, decision_start)
            )

    updated: dict[str, Any] = {}
    with sqlite3.connect(ms.DB_PATH, timeout=20) as c:
        rows = c.execute(
            '''SELECT engine_id,payload_json FROM engine_daily_score
               WHERE local_date=? AND context_signature=?''',
            (day.isoformat(), context),
        ).fetchall()
        for engine_id, raw in rows:
            payload = json.loads(raw)
            seen = sorted(keys_by_engine.get(str(engine_id)) or [])
            consistent = len(seen) == 1
            model_key = seen[0] if consistent else "mixed_or_unknown"
            payload.update(
                {
                    "model_key": model_key,
                    "model_keys_seen": seen,
                    "model_revision_consistent": consistent,
                    "promotion_eligible_model_revision": bool(consistent),
                    "selector_policy_version": POLICY_VERSION,
                }
            )
            c.execute(
                '''UPDATE engine_daily_score SET payload_json=?
                   WHERE local_date=? AND engine_id=? AND context_signature=?''',
                (
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    day.isoformat(),
                    str(engine_id),
                    context,
                ),
            )
            updated[str(engine_id)] = {
                "model_key": model_key,
                "model_keys_seen": seen,
                "model_revision_consistent": consistent,
            }
    return updated


def evaluate_selector_day(cfg: dict[str, Any], local_date: str, *, force: bool = False) -> dict[str, Any]:
    if _ORIGINAL_EVALUATE_DAY is None:
        raise RuntimeError("robust selector patch not installed")
    result = _ORIGINAL_EVALUATE_DAY(cfg, local_date, force=force)
    state = ms.ensure_selector_state(cfg)
    if result.get("status") in {"complete", "already_complete"}:
        annotations = _annotate_day_model_keys(str(local_date), state["context_signature"])
        result = {**result, "model_revision_annotations": annotations}
    return result


def _paired_model_scores(
    context: str,
    challenger_engine: str,
    challenger_key: str,
    incumbent_engine: str,
    incumbent_key: str,
    *,
    limit: int,
    not_before: str | None = None,
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    with sqlite3.connect(ms.DB_PATH, timeout=20) as c:
        state = c.execute(
            '''SELECT evaluation_start_date FROM engine_selector_state
               WHERE singleton=1 AND context_signature=?''',
            (context,),
        ).fetchone()
        evaluation_start = str(state[0]) if state else "0001-01-01"
        start = max(evaluation_start, str(not_before or "0001-01-01"))
        rows = c.execute(
            '''SELECT a.local_date,a.payload_json,b.payload_json
               FROM engine_daily_score a
               JOIN engine_daily_score b
                 ON b.local_date=a.local_date AND b.context_signature=a.context_signature
               WHERE a.context_signature=? AND a.engine_id=? AND b.engine_id=?
                 AND a.local_date>=? AND a.intervals>=? AND b.intervals>=?
               ORDER BY a.local_date DESC LIMIT 180''',
            (
                context,
                str(challenger_engine),
                str(incumbent_engine),
                start,
                MIN_DAILY_INTERVALS,
                MIN_DAILY_INTERVALS,
            ),
        ).fetchall()
    paired: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for day, challenger_raw, incumbent_raw in rows:
        challenger = json.loads(challenger_raw)
        incumbent = json.loads(incumbent_raw)
        if challenger.get("model_key") != challenger_key or incumbent.get("model_key") != incumbent_key:
            continue
        if not challenger.get("promotion_eligible_model_revision", False):
            continue
        if not incumbent.get("promotion_eligible_model_revision", False):
            continue
        paired.append((str(day), challenger, incumbent))
        if len(paired) >= max(1, int(limit)):
            break
    paired.reverse()
    return paired


def _robust_promotion_gate(
    context: str,
    challenger_engine: str,
    challenger_key: str,
    incumbent_engine: str,
    incumbent_key: str,
) -> dict[str, Any]:
    dq = _disqualification_status(context, challenger_engine, challenger_key)
    not_before = dq.get("qualification_not_before")
    pairs = _paired_model_scores(
        context,
        challenger_engine,
        challenger_key,
        incumbent_engine,
        incumbent_key,
        limit=QUALIFICATION_DAYS,
        not_before=not_before,
    )
    if dq.get("quarantine_active"):
        return {
            "challenger_engine_id": challenger_engine,
            "challenger_model_key": challenger_key,
            "incumbent_engine_id": incumbent_engine,
            "incumbent_model_key": incumbent_key,
            "paired_days": len(pairs),
            "eligible": False,
            "reason": "model revision is quarantined after live disqualification",
            "disqualification": dq,
        }
    if len(pairs) < QUALIFICATION_DAYS:
        return {
            "challenger_engine_id": challenger_engine,
            "challenger_model_key": challenger_key,
            "incumbent_engine_id": incumbent_engine,
            "incumbent_model_key": incumbent_key,
            "paired_days": len(pairs),
            "required_days": QUALIFICATION_DAYS,
            "eligible": False,
            "reason": "insufficient complete qualification days for this model revision",
            "qualification_not_before": not_before,
        }

    cvals = [float(c["mean_regret_ore"]) for _, c, _ in pairs]
    ivals = [float(i["mean_regret_ore"]) for _, _, i in pairs]
    cclamp = [float(c["clamp_rate"]) for _, c, _ in pairs]
    iclamp = [float(i["clamp_rate"]) for _, _, i in pairs]
    cmean, imean = mean(cvals), mean(ivals)
    absolute_improvement = imean - cmean
    relative_improvement = absolute_improvement / max(0.1, imean)
    win_days = sum(1 for c, i in zip(cvals, ivals) if c < i)
    ctail = ms._percentile(cvals, 0.90)
    itail = ms._percentile(ivals, 0.90)
    cmedian, imedian = median(cvals), median(ivals)
    cclamp_mean, iclamp_mean = mean(cclamp), mean(iclamp)
    gates = {
        "ten_complete_days": len(pairs) == QUALIFICATION_DAYS,
        "wins_at_least_seven_days": win_days >= QUALIFICATION_WIN_DAYS,
        "mean_relative_improvement": relative_improvement >= ms.MIN_RELATIVE_MEAN_IMPROVEMENT,
        "mean_absolute_improvement": absolute_improvement >= ms.MIN_ABSOLUTE_MEAN_IMPROVEMENT_ORE,
        "median_improvement": cmedian < imedian,
        "tail_not_materially_worse": ctail <= itail * ms.MAX_TAIL_RATIO + ms.TAIL_ABSOLUTE_TOLERANCE_ORE,
        "clamp_rate_not_worse": cclamp_mean <= iclamp_mean + ms.MAX_CLAMP_RATE_DELTA,
    }
    return {
        "challenger_engine_id": challenger_engine,
        "challenger_model_key": challenger_key,
        "incumbent_engine_id": incumbent_engine,
        "incumbent_model_key": incumbent_key,
        "paired_days": len(pairs),
        "qualification_days": QUALIFICATION_DAYS,
        "first_day": pairs[0][0],
        "last_day": pairs[-1][0],
        "win_days": win_days,
        "required_win_days": QUALIFICATION_WIN_DAYS,
        "challenger_mean_regret_ore": cmean,
        "incumbent_mean_regret_ore": imean,
        "absolute_improvement_ore_per_decision": absolute_improvement,
        "relative_improvement_fraction": relative_improvement,
        "challenger_median_daily_regret_ore": cmedian,
        "incumbent_median_daily_regret_ore": imedian,
        "challenger_p90_daily_regret_ore": ctail,
        "incumbent_p90_daily_regret_ore": itail,
        "challenger_clamp_rate": cclamp_mean,
        "incumbent_clamp_rate": iclamp_mean,
        "gates": gates,
        "eligible": all(gates.values()),
        "disqualification": dq,
    }


def _robust_rollback_gate(context: str, selected_engine: str, selected_key: str) -> dict[str, Any]:
    if selected_engine == ms.BASELINE_ENGINE_ID:
        return {"eligible": False, "reason": "baseline is selected"}
    pairs = _paired_model_scores(
        context,
        selected_engine,
        selected_key,
        ms.BASELINE_ENGINE_ID,
        BASELINE_MODEL_KEY,
        limit=ms.ROLLBACK_WINDOW_DAYS,
    )
    if len(pairs) < ms.MIN_ROLLBACK_DAYS:
        return {"eligible": False, "paired_days": len(pairs), "reason": "insufficient rollback window"}
    svals = [float(s["mean_regret_ore"]) for _, s, _ in pairs]
    bvals = [float(b["mean_regret_ore"]) for _, _, b in pairs]
    smean, bmean = mean(svals), mean(bvals)
    degradation = smean - bmean
    relative = degradation / max(0.1, bmean)
    baseline_wins = sum(1 for s, b in zip(svals, bvals) if b < s)
    eligible = (
        degradation >= ms.ROLLBACK_ABSOLUTE_DEGRADATION_ORE
        and relative >= ms.ROLLBACK_RELATIVE_DEGRADATION
        and baseline_wins >= math.ceil(len(pairs) * 2.0 / 3.0)
    )
    return {
        "eligible": eligible,
        "paired_days": len(pairs),
        "selected_engine_id": selected_engine,
        "selected_model_key": selected_key,
        "selected_mean_regret_ore": smean,
        "baseline_mean_regret_ore": bmean,
        "absolute_degradation_ore_per_decision": degradation,
        "relative_degradation_fraction": relative,
        "baseline_win_days": baseline_wins,
    }


def _candidate_engines(context: str, selected_engine: str) -> list[str]:
    with sqlite3.connect(ms.DB_PATH, timeout=20) as c:
        rows = c.execute(
            '''SELECT DISTINCT engine_id FROM engine_daily_score
               WHERE context_signature=? AND engine_id<>? ORDER BY engine_id''',
            (context, selected_engine),
        ).fetchall()
    return [str(r[0]) for r in rows if str(r[0]) != ms.BASELINE_ENGINE_ID]


def run_selection_policy(cfg: dict[str, Any]) -> dict[str, Any]:
    robust = _ensure_robust_state(cfg)
    context = robust["context_signature"]
    selected_engine = robust["selected_engine_id"]
    selected_key = robust["selected_model_key"]

    if selected_engine != ms.BASELINE_ENGINE_ID:
        dq = _disqualification_status(context, selected_engine, selected_key)
        if dq.get("quarantine_active"):
            state = _set_selected_model(
                cfg,
                ms.BASELINE_ENGINE_ID,
                BASELINE_MODEL_KEY,
                "selected model revision is quarantined",
                "rollback_quarantine",
                {"selected_model_key": selected_key, "disqualification": dq},
            )
            return {"action": "rollback", "reason": "quarantine", "state": state, "disqualification": dq}

        rollback = _robust_rollback_gate(context, selected_engine, selected_key)
        if rollback.get("eligible"):
            state = _set_selected_model(
                cfg,
                ms.BASELINE_ENGINE_ID,
                BASELINE_MODEL_KEY,
                "selected model materially underperformed deterministic_v35",
                "rollback_performance",
                rollback,
            )
            return {"action": "rollback", "reason": "performance", "state": state, "rollback_gate": rollback}
    else:
        rollback = {"eligible": False, "reason": "baseline is selected"}

    tariffs_enabled = bool((cfg.get("tariffs") or {}).get("enabled", False))
    if tariffs_enabled:
        return {
            "action": "hold",
            "reason": "auto promotion blocked while demand-tariff objective is enabled",
            "state": {**ms.ensure_selector_state(cfg), **robust},
            "rollback_gate": rollback,
        }

    assessments: list[dict[str, Any]] = []
    for engine_id in _candidate_engines(context, selected_engine):
        model_key = _current_model_key(engine_id)
        if not model_key:
            continue
        assessments.append(
            _robust_promotion_gate(context, engine_id, model_key, selected_engine, selected_key)
        )
    eligible = [item for item in assessments if item.get("eligible")]
    if not eligible:
        return {
            "action": "hold",
            "reason": "no challenger passed robust 10-day promotion gates",
            "state": {**ms.ensure_selector_state(cfg), **robust},
            "rollback_gate": rollback,
            "challengers": assessments,
        }

    winner = max(
        eligible,
        key=lambda item: (
            float(item.get("relative_improvement_fraction") or 0.0),
            float(item.get("absolute_improvement_ore_per_decision") or 0.0),
        ),
    )
    state = _set_selected_model(
        cfg,
        str(winner["challenger_engine_id"]),
        str(winner["challenger_model_key"]),
        "model revision passed robust 10-day qualification",
        "promotion",
        winner,
    )
    return {
        "action": "promote",
        "winner": winner,
        "state": state,
        "challengers": assessments,
        "physical_writes_enabled": False,
    }


def _health_event(
    context: str,
    decision_start: str,
    engine_id: str,
    model_key: str,
    status: str,
    fault_type: str | None,
    fallback_used: bool,
    details: dict[str, Any],
) -> None:
    _init_tables_shallow()
    with sqlite3.connect(ms.DB_PATH, timeout=20) as c:
        c.execute(
            '''INSERT INTO engine_model_health_event(
               created_at,decision_start,context_signature,engine_id,model_key,status,
               fault_type,fallback_used,details_json)
               VALUES (?,?,?,?,?,?,?,?,?)''',
            (
                ms._now(),
                str(decision_start),
                context,
                str(engine_id),
                str(model_key),
                str(status),
                fault_type,
                1 if fallback_used else 0,
                json.dumps(details, ensure_ascii=False, sort_keys=True),
            ),
        )


def _recent_health(context: str, engine_id: str, model_key: str, limit: int = LIVE_WINDOW_SELECTIONS) -> list[dict[str, Any]]:
    _init_tables_shallow()
    with sqlite3.connect(ms.DB_PATH, timeout=20) as c:
        rows = c.execute(
            '''SELECT status,fault_type,fallback_used,decision_start,details_json
               FROM engine_model_health_event
               WHERE context_signature=? AND engine_id=? AND model_key=?
               ORDER BY event_id DESC LIMIT ?''',
            (context, str(engine_id), str(model_key), max(1, int(limit))),
        ).fetchall()
    return [
        {
            "status": str(r[0]),
            "fault_type": r[1],
            "fallback_used": bool(r[2]),
            "decision_start": str(r[3]),
            "details": json.loads(r[4]),
        }
        for r in rows
    ]


def _circuit_breaker_reason(context: str, engine_id: str, model_key: str, current_fault: str | None) -> dict[str, Any] | None:
    recent = _recent_health(context, engine_id, model_key, LIVE_WINDOW_SELECTIONS)
    if current_fault in {"gross_action_out_of_bounds", "gross_expected_soc_out_of_bounds", "nonfinite_output"}:
        return {"reason": "gross invalid model output", "fault_type": current_fault, "recent": recent[:5]}

    consecutive = 0
    for item in recent:
        if item["status"] == "fault":
            consecutive += 1
        else:
            break
    if consecutive >= LIVE_MAX_CONSECUTIVE_FAULTS:
        return {"reason": "three consecutive model-health faults", "consecutive_faults": consecutive, "recent": recent[:5]}

    first_five = recent[:5]
    safety_rejects = sum(1 for item in first_five if item.get("fault_type") == "safety_envelope_reject")
    if len(first_five) >= 5 and safety_rejects >= LIVE_SAFETY_REJECTS_IN_FIVE:
        return {"reason": "repeated deterministic safety-envelope rejections", "safety_rejects_last_five": safety_rejects, "recent": first_five}

    if len(recent) >= LIVE_MIN_RATE_SELECTIONS:
        fallback_rate = sum(1 for item in recent if item["fallback_used"]) / len(recent)
        if fallback_rate >= LIVE_MAX_FALLBACK_RATE:
            return {"reason": "live fallback rate exceeded circuit-breaker threshold", "fallback_rate": fallback_rate, "recent_selections": len(recent)}
    return None


def _decision_health(
    cfg: dict[str, Any],
    information_vintage_id: str,
    requested_action_kw: float,
    expected_soc_pct: float | None,
) -> dict[str, Any]:
    if not math.isfinite(float(requested_action_kw)):
        return {"ok": False, "fault_type": "nonfinite_output", "gross": True}
    if expected_soc_pct is not None and not math.isfinite(float(expected_soc_pct)):
        return {"ok": False, "fault_type": "nonfinite_output", "gross": True}

    opt = cfg.get("optimizer") or {}
    battery = (cfg.get("policy") or {}).get("battery") or {}
    cmax = float(opt.get("battery_max_charge_kw", 8.0))
    dmax = float(opt.get("battery_max_discharge_kw", 8.0))
    action = float(requested_action_kw)
    if action > dmax * GROSS_ACTION_MULTIPLE + ENVELOPE_TOLERANCE_KW or action < -cmax * GROSS_ACTION_MULTIPLE - ENVELOPE_TOLERANCE_KW:
        return {
            "ok": False,
            "fault_type": "gross_action_out_of_bounds",
            "gross": True,
            "requested_action_kw": action,
            "charge_limit_kw": cmax,
            "discharge_limit_kw": dmax,
        }

    hmin = float(battery.get("hard_min_soc_pct", 5.0))
    hmax = float(battery.get("hard_max_soc_pct", 100.0))
    if expected_soc_pct is not None:
        expected = float(expected_soc_pct)
        if expected < hmin - 10.0 or expected > hmax + 10.0:
            return {
                "ok": False,
                "fault_type": "gross_expected_soc_out_of_bounds",
                "gross": True,
                "expected_soc_pct": expected,
                "hard_min_soc_pct": hmin,
                "hard_max_soc_pct": hmax,
            }
        if expected < hmin - 2.0 or expected > hmax + 2.0:
            return {
                "ok": False,
                "fault_type": "safety_envelope_reject",
                "gross": False,
                "reason": "expected SOC outside hard envelope",
                "expected_soc_pct": expected,
            }

    with sqlite3.connect(ms.DB_PATH, timeout=20) as c:
        row = c.execute(
            "SELECT initial_soc_pct,payload_json FROM engine_information_vintage WHERE information_vintage_id=?",
            (str(information_vintage_id),),
        ).fetchone()
    if not row:
        return {"ok": False, "fault_type": "missing_information_vintage", "gross": False}
    try:
        payload = json.loads(row[1])
        horizon = list(payload.get("horizon_rows") or ())
        first = horizon[0]
        first_row = {"load_kw": float(first["load_kw"]), "pv_kw": float(first["pv_kw"])}
        applied, clamped = ms._clamp_first_action(first_row, action, float(row[0]), cfg)
    except Exception as exc:
        return {"ok": False, "fault_type": "safety_check_failed", "gross": False, "error": repr(exc)}
    if clamped and abs(applied - action) > ENVELOPE_TOLERANCE_KW:
        return {
            "ok": False,
            "fault_type": "safety_envelope_reject",
            "gross": False,
            "requested_action_kw": action,
            "safe_action_kw": applied,
            "difference_kw": abs(applied - action),
        }
    return {"ok": True, "fault_type": None, "gross": False, "safe_action_kw": applied}


def _persist_control_selection(result: dict[str, Any]) -> None:
    with sqlite3.connect(ms.DB_PATH, timeout=20) as c:
        c.execute(
            '''INSERT OR REPLACE INTO engine_control_selection(
               information_vintage_id,decision_start,created_at,configured_selected_engine_id,
               routed_engine_id,decision_id,requested_action_kw,fallback_used,reason,payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (
                str(result["information_vintage_id"]),
                str(result["decision_start"]),
                ms._now(),
                str(result["configured_selected_engine_id"]),
                result.get("routed_engine_id"),
                result.get("decision_id"),
                result.get("requested_action_kw"),
                1 if result.get("fallback_used") else 0,
                str(result.get("reason") or ""),
                json.dumps(result, ensure_ascii=False, sort_keys=True),
            ),
        )


def route_selected_decision(cfg: dict[str, Any], information_vintage_id: str, decision_start: str) -> dict[str, Any]:
    robust = _ensure_robust_state(cfg)
    context = robust["context_signature"]
    configured_engine = robust["selected_engine_id"]
    configured_key = robust["selected_model_key"]

    with sqlite3.connect(ms.DB_PATH, timeout=20) as c:
        rows = c.execute(
            '''SELECT engine_id,decision_id,status,requested_action_kw,expected_soc_pct,payload_json
               FROM engine_decision WHERE information_vintage_id=?''',
            (str(information_vintage_id),),
        ).fetchall()
    decisions: dict[str, dict[str, Any]] = {}
    for engine_id, decision_id, status, action, expected_soc, raw in rows:
        if str(status) != "ok":
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}
        decisions[str(engine_id)] = {
            "engine_id": str(engine_id),
            "decision_id": str(decision_id),
            "requested_action_kw": float(action),
            "expected_soc_pct": None if expected_soc is None else float(expected_soc),
            "payload": payload,
        }

    baseline = decisions.get(ms.BASELINE_ENGINE_ID)
    chosen = None
    fallback_used = False
    fault_type = None
    health = None
    disqualification = None
    reason = "selected_engine_available"

    if configured_engine == ms.BASELINE_ENGINE_ID:
        chosen = baseline
        reason = "deterministic_v35_selected"
    else:
        selected = decisions.get(configured_engine)
        if selected is None:
            fallback_used = True
            fault_type = "missing_decision"
            reason = "selected_engine_missing_fallback_to_deterministic_v35"
            _health_event(context, decision_start, configured_engine, configured_key, "fault", fault_type, True, {})
            breaker = _circuit_breaker_reason(context, configured_engine, configured_key, fault_type)
            if breaker:
                disqualification = _disqualify_model(cfg, configured_engine, configured_key, breaker["reason"], breaker)
            chosen = baseline
        else:
            current_key = _engine_model_key(configured_engine, selected["payload"], decision_start)
            if current_key != configured_key:
                fallback_used = True
                fault_type = "model_revision_changed"
                reason = "selected_model_revision_changed_requires_requalification"
                _health_event(
                    context,
                    decision_start,
                    configured_engine,
                    configured_key,
                    "revision_change",
                    fault_type,
                    True,
                    {"selected_model_key": configured_key, "current_model_key": current_key},
                )
                _set_selected_model(
                    cfg,
                    ms.BASELINE_ENGINE_ID,
                    BASELINE_MODEL_KEY,
                    "selected engine produced a new model revision; requalification required",
                    "rollback_revision_change",
                    {"old_model_key": configured_key, "new_model_key": current_key},
                )
                chosen = baseline
            else:
                dq = _disqualification_status(context, configured_engine, configured_key)
                if dq.get("quarantine_active"):
                    fallback_used = True
                    fault_type = "quarantined_model_revision"
                    reason = "selected_model_is_quarantined_fallback_to_deterministic_v35"
                    chosen = baseline
                    _set_selected_model(
                        cfg,
                        ms.BASELINE_ENGINE_ID,
                        BASELINE_MODEL_KEY,
                        "selected model revision is quarantined",
                        "rollback_quarantine",
                        {"model_key": configured_key, "disqualification": dq},
                    )
                else:
                    health = _decision_health(
                        cfg,
                        information_vintage_id,
                        selected["requested_action_kw"],
                        selected["expected_soc_pct"],
                    )
                    if not health["ok"]:
                        fallback_used = True
                        fault_type = str(health.get("fault_type") or "model_health_fault")
                        reason = "selected_model_failed_live_health_check_fallback_to_deterministic_v35"
                        _health_event(
                            context,
                            decision_start,
                            configured_engine,
                            configured_key,
                            "fault",
                            fault_type,
                            True,
                            health,
                        )
                        breaker = _circuit_breaker_reason(context, configured_engine, configured_key, fault_type)
                        if breaker:
                            disqualification = _disqualify_model(
                                cfg,
                                configured_engine,
                                configured_key,
                                breaker["reason"],
                                {**breaker, "current_health": health},
                            )
                        chosen = baseline
                    else:
                        _health_event(
                            context,
                            decision_start,
                            configured_engine,
                            configured_key,
                            "healthy",
                            None,
                            False,
                            health,
                        )
                        chosen = selected

    if chosen is None:
        reason = "no_baseline_decision_available"

    result = {
        "information_vintage_id": str(information_vintage_id),
        "decision_start": str(decision_start),
        "configured_selected_engine_id": configured_engine,
        "configured_selected_model_key": configured_key,
        "routed_engine_id": None if chosen is None else chosen["engine_id"],
        "routed_model_key": None if chosen is None else _engine_model_key(chosen["engine_id"], chosen["payload"], decision_start),
        "decision_id": None if chosen is None else chosen["decision_id"],
        "requested_action_kw": None if chosen is None else chosen["requested_action_kw"],
        "fallback_used": bool(fallback_used),
        "fault_type": fault_type,
        "health": health,
        "disqualification": disqualification,
        "reason": reason,
        "requires_downstream_deterministic_safety": True,
        "physical_writes_enabled": False,
    }
    _persist_control_selection(result)
    return result


def _compat_promotion_gate(context: str, challenger: str, incumbent: str) -> dict[str, Any]:
    robust = _robust_state_for_context(context)
    challenger_key = _current_model_key(challenger)
    incumbent_key = robust.get("selected_model_key") if incumbent == robust.get("selected_engine_id") else _current_model_key(incumbent)
    if not challenger_key or not incumbent_key:
        return {"eligible": False, "reason": "model revision unavailable", "challenger_engine_id": challenger, "incumbent_engine_id": incumbent}
    return _robust_promotion_gate(context, challenger, challenger_key, incumbent, incumbent_key)


def _compat_rollback_gate(context: str, selected: str) -> dict[str, Any]:
    robust = _robust_state_for_context(context)
    key = robust.get("selected_model_key") if selected == robust.get("selected_engine_id") else _current_model_key(selected)
    if not key:
        return {"eligible": False, "reason": "model revision unavailable"}
    return _robust_rollback_gate(context, selected, key)


def _robust_state_for_context(context: str) -> dict[str, Any]:
    _init_tables_shallow()
    with sqlite3.connect(ms.DB_PATH, timeout=20) as c:
        row = c.execute(
            '''SELECT selected_engine_id,selected_model_key,updated_at
               FROM engine_selector_robust_state WHERE singleton=1 AND context_signature=?''',
            (context,),
        ).fetchone()
    if not row:
        return {"selected_engine_id": ms.BASELINE_ENGINE_ID, "selected_model_key": BASELINE_MODEL_KEY, "updated_at": None}
    return {"selected_engine_id": str(row[0]), "selected_model_key": str(row[1]), "updated_at": str(row[2])}


def _compat_live_health(selected_engine_id: str) -> dict[str, Any]:
    _init_tables_shallow()
    with sqlite3.connect(ms.DB_PATH, timeout=20) as c:
        row = c.execute(
            '''SELECT context_signature,selected_model_key FROM engine_selector_robust_state
               WHERE singleton=1 AND selected_engine_id=?''',
            (str(selected_engine_id),),
        ).fetchone()
    if not row or selected_engine_id == ms.BASELINE_ENGINE_ID:
        return {"configured_selected_engine_id": selected_engine_id, "healthy": True, "selections": 0, "fallbacks": 0, "fallback_rate": 0.0}
    recent = _recent_health(str(row[0]), str(selected_engine_id), str(row[1]), LIVE_WINDOW_SELECTIONS)
    fallbacks = sum(1 for item in recent if item["fallback_used"])
    return {
        "configured_selected_engine_id": selected_engine_id,
        "selected_model_key": str(row[1]),
        "selections": len(recent),
        "fallbacks": fallbacks,
        "fallback_rate": fallbacks / len(recent) if recent else 0.0,
        "healthy": _circuit_breaker_reason(str(row[0]), str(selected_engine_id), str(row[1]), None) is None,
        "window_selections": LIVE_WINDOW_SELECTIONS,
    }


def selector_status(cfg: dict[str, Any]) -> dict[str, Any]:
    if _ORIGINAL_SELECTOR_STATUS is None:
        raise RuntimeError("robust selector patch not installed")
    robust = _ensure_robust_state(cfg)
    base = _ORIGINAL_SELECTOR_STATUS(cfg)
    context = robust["context_signature"]
    with sqlite3.connect(ms.DB_PATH, timeout=20) as c:
        disqualifications = [
            {
                "engine_id": str(r[0]),
                "model_key": str(r[1]),
                "disqualified_at": str(r[2]),
                "quarantine_until": str(r[3]),
                "reason": str(r[4]),
            }
            for r in c.execute(
                '''SELECT engine_id,model_key,disqualified_at,quarantine_until,reason
                   FROM engine_model_disqualification WHERE context_signature=?
                   ORDER BY disqualification_id DESC LIMIT 20''',
                (context,),
            ).fetchall()
        ]
        generations = [
            {
                "engine_id": str(r[0]),
                "generation": int(r[1]),
                "started_at": str(r[2]),
                "ended_at": r[3],
                "source_state_id": r[4],
                "reason": str(r[5]),
            }
            for r in c.execute(
                '''SELECT engine_id,generation,started_at,ended_at,source_state_id,reason
                   FROM engine_model_generation ORDER BY engine_id,generation DESC LIMIT 20'''
            ).fetchall()
        ]
    from .engine_registry import registry_status
    current_keys = {
        str(item["engine_id"]): _current_model_key(str(item["engine_id"]))
        for item in registry_status().get("engines") or []
        if item.get("engine_id") and str(item["engine_id"]) != ms.BASELINE_ENGINE_ID
    }
    return {
        **base,
        "state": {**(base.get("state") or {}), **robust},
        "selector_policy_version": POLICY_VERSION,
        "promotion_policy": {
            "qualification_days": QUALIFICATION_DAYS,
            "required_win_days": QUALIFICATION_WIN_DAYS,
            "minimum_daily_intervals": MIN_DAILY_INTERVALS,
            "minimum_relative_mean_improvement": ms.MIN_RELATIVE_MEAN_IMPROVEMENT,
            "minimum_absolute_mean_improvement_ore_per_decision": ms.MIN_ABSOLUTE_MEAN_IMPROVEMENT_ORE,
            "median_must_improve": True,
            "tail_must_not_regress": True,
            "clamp_rate_must_not_regress": True,
            "same_model_revision_required_per_day": True,
            "same_model_revision_required_across_qualification": True,
        },
        "live_circuit_breaker": {
            "monitoring": "every_15_minute_control_routing",
            "individual_fault_action": "fallback_to_deterministic_v35",
            "gross_invalid_output": "immediate_disqualification",
            "consecutive_faults_to_disqualify": LIVE_MAX_CONSECUTIVE_FAULTS,
            "safety_rejects_in_last_five_to_disqualify": LIVE_SAFETY_REJECTS_IN_FIVE,
            "fallback_rate_window": LIVE_WINDOW_SELECTIONS,
            "minimum_attempts_for_rate_gate": LIVE_MIN_RATE_SELECTIONS,
            "maximum_fallback_rate": LIVE_MAX_FALLBACK_RATE,
            "quarantine_days": QUARANTINE_DAYS,
            "same_revision_requalification_days": QUALIFICATION_DAYS,
            "fallback_engine_id": ms.BASELINE_ENGINE_ID,
        },
        "selected_model_key": robust["selected_model_key"],
        "current_model_keys": current_keys,
        "recent_disqualifications": disqualifications,
        "adaptive_generations": generations,
        "live_health": _compat_live_health(robust["selected_engine_id"]),
    }


def install_robust_selector_patch() -> None:
    global _INSTALLED, _ORIGINAL_CONTEXT_SIGNATURE, _ORIGINAL_EVALUATE_DAY, _ORIGINAL_SELECTOR_STATUS
    if _INSTALLED:
        return
    _ORIGINAL_CONTEXT_SIGNATURE = ms._context_signature
    _ORIGINAL_EVALUATE_DAY = ms.evaluate_selector_day
    _ORIGINAL_SELECTOR_STATUS = ms.selector_status

    ms._context_signature = _policy_context_signature
    ms.MIN_PROMOTION_DAYS = QUALIFICATION_DAYS
    ms.MIN_WIN_RATE = QUALIFICATION_WIN_DAYS / QUALIFICATION_DAYS
    ms.MIN_DAILY_INTERVALS = MIN_DAILY_INTERVALS
    ms.WINDOW_DAYS = QUALIFICATION_DAYS
    ms.evaluate_selector_day = evaluate_selector_day
    ms.run_selection_policy = run_selection_policy
    ms.route_selected_decision = route_selected_decision
    ms._promotion_gate = _compat_promotion_gate
    ms._rollback_gate = _compat_rollback_gate
    ms._live_health = _compat_live_health
    ms.selector_status = selector_status
    _INSTALLED = True
    _init_tables()
