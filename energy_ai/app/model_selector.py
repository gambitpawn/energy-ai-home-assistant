from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from statistics import mean, median
from typing import Any
from zoneinfo import ZoneInfo

from .db import DB_PATH
from .optimizer import DT_HOURS, _state_grid, _transition_action_kw
from .optimizer_evaluation import _day_bounds
from .price_economics import economics_signature, effective_prices

LOCAL_TZ = ZoneInfo("Europe/Stockholm")
BASELINE_ENGINE_ID = "deterministic_v35"

# Selector policy. These are deliberately conservative because promotion changes
# the logical control incumbent. Physical inverter writes remain outside this
# module and disabled in the current runtime.
WINDOW_DAYS = 30
MIN_PROMOTION_DAYS = 14
MIN_DAILY_INTERVALS = 80
MIN_RELATIVE_MEAN_IMPROVEMENT = 0.05
MIN_ABSOLUTE_MEAN_IMPROVEMENT_ORE = 0.10
MIN_WIN_RATE = 0.65
MAX_TAIL_RATIO = 1.10
TAIL_ABSOLUTE_TOLERANCE_ORE = 0.25
MAX_CLAMP_RATE_DELTA = 0.03
PROMOTION_COOLDOWN_DAYS = 7
ROLLBACK_WINDOW_DAYS = 5
MIN_ROLLBACK_DAYS = 3
ROLLBACK_RELATIVE_DEGRADATION = 0.15
ROLLBACK_ABSOLUTE_DEGRADATION_ORE = 0.25
LIVE_HEALTH_MIN_SELECTIONS = 20
LIVE_HEALTH_MAX_FALLBACK_RATE = 0.10
ORACLE_HORIZON_INTERVALS = 96  # 24 h at 15-minute resolution.


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dt(value: str) -> datetime:
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    pos = max(0.0, min(1.0, float(q))) * (len(ordered) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ordered[lo]
    frac = pos - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def _sign(value: float, tol: float = 0.25) -> int:
    if value > tol:
        return 1
    if value < -tol:
        return -1
    return 0


def _init_tables() -> None:
    with sqlite3.connect(DB_PATH, timeout=20) as c:
        c.executescript(
            '''
            CREATE TABLE IF NOT EXISTS engine_selector_state(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                selected_engine_id TEXT NOT NULL,
                fallback_engine_id TEXT NOT NULL,
                context_signature TEXT NOT NULL,
                evaluation_start_date TEXT NOT NULL,
                selected_since TEXT NOT NULL,
                cooldown_until TEXT,
                updated_at TEXT NOT NULL,
                last_evaluated_date TEXT,
                last_selection_reason TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS engine_selector_event(
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                from_engine_id TEXT,
                to_engine_id TEXT,
                context_signature TEXT NOT NULL,
                reason TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_selector_event_created
                ON engine_selector_event(created_at);

            CREATE TABLE IF NOT EXISTS engine_selector_day_run(
                local_date TEXT NOT NULL,
                context_signature TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(local_date,context_signature)
            );

            CREATE TABLE IF NOT EXISTS engine_daily_score(
                local_date TEXT NOT NULL,
                engine_id TEXT NOT NULL,
                context_signature TEXT NOT NULL,
                intervals INTEGER NOT NULL,
                mean_regret_ore REAL NOT NULL,
                p90_regret_ore REAL NOT NULL,
                clamp_rate REAL NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(local_date,engine_id,context_signature)
            );
            CREATE INDEX IF NOT EXISTS idx_engine_daily_score_engine_date
                ON engine_daily_score(engine_id,local_date);

            CREATE TABLE IF NOT EXISTS engine_control_selection(
                information_vintage_id TEXT PRIMARY KEY,
                decision_start TEXT NOT NULL,
                created_at TEXT NOT NULL,
                configured_selected_engine_id TEXT NOT NULL,
                routed_engine_id TEXT,
                decision_id TEXT,
                requested_action_kw REAL,
                fallback_used INTEGER NOT NULL,
                reason TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_control_selection_start
                ON engine_control_selection(decision_start);
            '''
        )


def _context_signature(cfg: dict[str, Any]) -> str:
    battery = (cfg.get("policy") or {}).get("battery") or {}
    optimizer = cfg.get("optimizer") or {}
    tariffs = cfg.get("tariffs") or {}
    payload = {
        "economics": economics_signature(cfg),
        "battery": {
            k: battery.get(k)
            for k in (
                "capacity_kwh",
                "hard_min_soc_pct",
                "hard_max_soc_pct",
                "preferred_min_soc_pct",
                "preferred_max_soc_pct",
            )
        },
        "optimizer": {
            k: optimizer.get(k)
            for k in (
                "battery_max_charge_kw",
                "battery_max_discharge_kw",
                "battery_charge_efficiency",
                "battery_discharge_efficiency",
                "battery_degradation_ore_kwh",
                "physical_grid_import_limit_kw",
                "grid_export_limit_kw",
                "soc_grid_step_kwh",
            )
        },
        "tariffs_enabled": bool(tariffs.get("enabled", False)),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _event(
    event_type: str,
    context_signature: str,
    reason: str,
    *,
    from_engine_id: str | None = None,
    to_engine_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    _init_tables()
    with sqlite3.connect(DB_PATH, timeout=20) as c:
        c.execute(
            '''INSERT INTO engine_selector_event(
               created_at,event_type,from_engine_id,to_engine_id,context_signature,reason,payload_json)
               VALUES (?,?,?,?,?,?,?)''',
            (
                _now(),
                event_type,
                from_engine_id,
                to_engine_id,
                context_signature,
                reason,
                json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
            ),
        )


def ensure_selector_state(cfg: dict[str, Any]) -> dict[str, Any]:
    _init_tables()
    context = _context_signature(cfg)
    today = datetime.now(LOCAL_TZ).date().isoformat()
    with sqlite3.connect(DB_PATH, timeout=20) as c:
        row = c.execute(
            '''SELECT selected_engine_id,fallback_engine_id,context_signature,
                      evaluation_start_date,selected_since,cooldown_until,updated_at,
                      last_evaluated_date,last_selection_reason
               FROM engine_selector_state WHERE singleton=1'''
        ).fetchone()
        if row is None:
            now = _now()
            c.execute(
                '''INSERT INTO engine_selector_state(
                   singleton,selected_engine_id,fallback_engine_id,context_signature,
                   evaluation_start_date,selected_since,cooldown_until,updated_at,
                   last_evaluated_date,last_selection_reason)
                   VALUES (1,?,?,?,?,?,?,?,?,?)''',
                (
                    BASELINE_ENGINE_ID,
                    BASELINE_ENGINE_ID,
                    context,
                    today,
                    now,
                    None,
                    now,
                    None,
                    "initial_baseline",
                ),
            )
            row = (
                BASELINE_ENGINE_ID,
                BASELINE_ENGINE_ID,
                context,
                today,
                now,
                None,
                now,
                None,
                "initial_baseline",
            )
        elif str(row[2]) != context:
            previous = str(row[0])
            now = _now()
            c.execute(
                '''UPDATE engine_selector_state SET
                   selected_engine_id=?,fallback_engine_id=?,context_signature=?,
                   evaluation_start_date=?,selected_since=?,cooldown_until=NULL,
                   updated_at=?,last_evaluated_date=NULL,last_selection_reason=?
                   WHERE singleton=1''',
                (
                    BASELINE_ENGINE_ID,
                    BASELINE_ENGINE_ID,
                    context,
                    today,
                    now,
                    now,
                    "control_context_changed_reset_to_baseline",
                ),
            )
            _event(
                "context_reset",
                context,
                "Control/economics context changed; restart validation from frozen baseline.",
                from_engine_id=previous,
                to_engine_id=BASELINE_ENGINE_ID,
                payload={"evaluation_start_date": today},
            )
            row = (
                BASELINE_ENGINE_ID,
                BASELINE_ENGINE_ID,
                context,
                today,
                now,
                None,
                now,
                None,
                "control_context_changed_reset_to_baseline",
            )

    return {
        "selected_engine_id": str(row[0]),
        "fallback_engine_id": str(row[1]),
        "context_signature": str(row[2]),
        "evaluation_start_date": str(row[3]),
        "selected_since": str(row[4]),
        "cooldown_until": row[5],
        "updated_at": str(row[6]),
        "last_evaluated_date": row[7],
        "last_selection_reason": str(row[8]),
    }


def _set_selected_engine(
    cfg: dict[str, Any],
    engine_id: str,
    reason: str,
    event_type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    state = ensure_selector_state(cfg)
    previous = state["selected_engine_id"]
    now_dt = datetime.now(timezone.utc)
    cooldown = (now_dt + timedelta(days=PROMOTION_COOLDOWN_DAYS)).isoformat()
    with sqlite3.connect(DB_PATH, timeout=20) as c:
        c.execute(
            '''UPDATE engine_selector_state SET selected_engine_id=?,selected_since=?,
               cooldown_until=?,updated_at=?,last_selection_reason=? WHERE singleton=1''',
            (str(engine_id), now_dt.isoformat(), cooldown, now_dt.isoformat(), reason),
        )
    _event(
        event_type,
        state["context_signature"],
        reason,
        from_engine_id=previous,
        to_engine_id=str(engine_id),
        payload=payload,
    )
    return ensure_selector_state(cfg)


def _actual_map(start: datetime, end: datetime) -> dict[datetime, dict[str, float]]:
    with sqlite3.connect(DB_PATH, timeout=20) as c:
        states = c.execute(
            '''SELECT bucket_start,payload_json FROM state_15m
               WHERE bucket_start>=? AND bucket_start<? ORDER BY bucket_start''',
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        prices = c.execute(
            '''SELECT start_utc,price_ore_kwh FROM price_15m
               WHERE start_utc>=? AND start_utc<? ORDER BY start_utc''',
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    price_map = {_dt(s).replace(second=0, microsecond=0): float(p) for s, p in prices}
    out: dict[datetime, dict[str, float]] = {}
    for stamp_raw, payload_raw in states:
        try:
            stamp = _dt(stamp_raw).replace(second=0, microsecond=0)
            payload = json.loads(payload_raw)
            means = payload.get("mean") or {}
            load = float(means["house_load_kw"])
            pv = float(means["pv_power_kw"])
            price_raw = means.get("spot_price_ore_kwh")
            price = float(price_raw) if price_raw not in (None, "") else price_map.get(stamp)
            if price is None or not all(math.isfinite(v) for v in (load, pv, float(price))):
                continue
        except Exception:
            continue
        out[stamp] = {
            "load_kw": max(0.0, load),
            "pv_kw": max(0.0, pv),
            "price_ore_kwh": float(price),
        }
    return out


def _competition_vintages(local_date: date) -> list[dict[str, Any]]:
    start, end = _day_bounds(local_date)
    _init_tables()
    with sqlite3.connect(DB_PATH, timeout=20) as c:
        rows = c.execute(
            '''SELECT d.decision_start,d.information_vintage_id,v.generated_at,v.payload_json,
                      d.engine_id,d.status,d.payload_json
               FROM engine_decision d
               JOIN engine_information_vintage v
                 ON v.information_vintage_id=d.information_vintage_id
               WHERE d.decision_start>=? AND d.decision_start<?
               ORDER BY d.decision_start,v.generated_at,d.engine_id''',
            (start.isoformat(), end.isoformat()),
        ).fetchall()

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for decision_start, vintage_id, generated_at, input_raw, engine_id, status, decision_raw in rows:
        key = (str(decision_start), str(vintage_id))
        try:
            input_payload = json.loads(input_raw)
            decision_payload = json.loads(decision_raw)
        except Exception:
            continue
        item = grouped.setdefault(
            key,
            {
                "decision_start": str(decision_start),
                "information_vintage_id": str(vintage_id),
                "generated_at": str(generated_at),
                "input": input_payload,
                "decisions": {},
            },
        )
        if str(status) == "ok":
            item["decisions"][str(engine_id)] = decision_payload

    by_start: dict[str, list[dict[str, Any]]] = {}
    for item in grouped.values():
        if BASELINE_ENGINE_ID not in item["decisions"]:
            continue
        by_start.setdefault(item["decision_start"], []).append(item)

    canonical = []
    for items in by_start.values():
        # Mirror live semantics: the freshest valid baseline vintage at the
        # quarter is canonical. Challengers missing that same vintage count as
        # missing, rather than being silently replaced by an older decision.
        canonical.append(max(items, key=lambda x: _dt(x["generated_at"])))
    canonical.sort(key=lambda x: _dt(x["decision_start"]))
    return canonical


def _transition_table(states: list[float], ec: float, ed: float, cmax: float, dmax: float) -> list[list[tuple[int, float]]]:
    table: list[list[tuple[int, float]]] = []
    for i0, e0 in enumerate(states):
        options: list[tuple[int, float]] = []
        for i1, e1 in enumerate(states):
            action = float(_transition_action_kw(e0, e1, ec, ed))
            if action < -cmax - 1e-9 or action > dmax + 1e-9:
                continue
            options.append((i1, action))
        table.append(options)
    return table


def _action_feasible(row: dict[str, float], action: float, cfg: dict[str, Any]) -> bool:
    opt = cfg.get("optimizer") or {}
    ilim = float(opt.get("physical_grid_import_limit_kw", 13.8))
    elim = float(opt.get("grid_export_limit_kw", 10.0))
    net = float(row["load_kw"]) - float(row["pv_kw"])
    if action >= 0.0:
        # PV may already exceed the export limit and be curtailed, but battery
        # discharge may not increase export beyond the physical limit.
        return action <= max(0.0, net + elim) + 1e-9
    return net - action <= ilim + 1e-9


def _interval_cash_cost(row: dict[str, float], action: float, cfg: dict[str, Any]) -> float:
    opt = cfg.get("optimizer") or {}
    elim = float(opt.get("grid_export_limit_kw", 10.0))
    net = float(row["load_kw"]) - float(row["pv_kw"])
    grid = net - float(action)
    imp = max(0.0, grid)
    exp = min(max(0.0, -grid), elim)
    prices = effective_prices(float(row["price_ore_kwh"]), cfg)
    degradation = abs(float(action)) * DT_HOURS * float(opt.get("battery_degradation_ore_kwh", 5.0))
    energy = (
        imp * prices["effective_import_price_ore_kwh"]
        - exp * prices["effective_export_price_ore_kwh"]
    ) * DT_HOURS
    return float(energy + degradation)


def _oracle_first_action_values(
    rows: list[dict[str, float]], cfg: dict[str, Any], initial_soc_pct: float
) -> dict[str, Any]:
    if not rows:
        raise ValueError("oracle rows are empty")
    battery = (cfg.get("policy") or {}).get("battery") or {}
    opt = cfg.get("optimizer") or {}
    cap = float(battery.get("capacity_kwh", 19.6))
    hmin = float(battery.get("hard_min_soc_pct", 5.0))
    hmax = float(battery.get("hard_max_soc_pct", 100.0))
    ec = float(opt.get("battery_charge_efficiency", 0.95))
    ed = float(opt.get("battery_discharge_efficiency", 0.95))
    cmax = float(opt.get("battery_max_charge_kw", 8.0))
    dmax = float(opt.get("battery_max_discharge_kw", 8.0))
    step = float(opt.get("soc_grid_step_kwh", 0.5))
    planning_soc = max(hmin, min(hmax, float(initial_soc_pct)))
    initial_energy = cap * planning_soc / 100.0
    states, effective_step = _state_grid(cap * hmin / 100.0, cap * hmax / 100.0, step, initial_energy)
    init_idx = min(range(len(states)), key=lambda i: abs(states[i] - initial_energy))
    transitions = _transition_table(states, ec, ed, cmax, dmax)

    terminal_reference = median(
        effective_prices(float(row["price_ore_kwh"]), cfg)["effective_import_price_ore_kwh"]
        for row in rows
    )
    # Fixed external terminal asset value. It is deliberately independent of
    # adaptive/neural policy parameters, so selection cannot improve its own
    # score by changing an internal terminal-value coefficient.
    future = [-float(e) * float(terminal_reference) for e in states]

    for t in range(len(rows) - 1, 0, -1):
        row = rows[t]
        nxt = [math.inf] * len(states)
        for i0 in range(len(states)):
            best = math.inf
            for i1, action in transitions[i0]:
                if not _action_feasible(row, action, cfg):
                    continue
                value = _interval_cash_cost(row, action, cfg) + future[i1]
                if value < best:
                    best = value
            nxt[i0] = best
        future = nxt

    first_values: list[dict[str, float]] = []
    for i1, action in transitions[init_idx]:
        if not _action_feasible(rows[0], action, cfg):
            continue
        value = _interval_cash_cost(rows[0], action, cfg) + future[i1]
        if math.isfinite(value):
            first_values.append({"action_kw": float(action), "value_ore": float(value)})
    if not first_values:
        raise RuntimeError("no feasible external-oracle first action")
    oracle = min(first_values, key=lambda x: x["value_ore"])
    return {
        "oracle_action_kw": float(oracle["action_kw"]),
        "oracle_value_ore": float(oracle["value_ore"]),
        "first_action_values": first_values,
        "terminal_reference_ore_kwh": float(terminal_reference),
        "soc_grid_effective_step_kwh": float(effective_step),
        "planning_initial_soc_pct": float(planning_soc),
    }


def _clamp_first_action(
    row: dict[str, float], requested_action_kw: float, initial_soc_pct: float, cfg: dict[str, Any]
) -> tuple[float, bool]:
    battery = (cfg.get("policy") or {}).get("battery") or {}
    opt = cfg.get("optimizer") or {}
    cap = float(battery.get("capacity_kwh", 19.6))
    hmin = float(battery.get("hard_min_soc_pct", 5.0))
    hmax = float(battery.get("hard_max_soc_pct", 100.0))
    ec = float(opt.get("battery_charge_efficiency", 0.95))
    ed = float(opt.get("battery_discharge_efficiency", 0.95))
    cmax = float(opt.get("battery_max_charge_kw", 8.0))
    dmax = float(opt.get("battery_max_discharge_kw", 8.0))
    ilim = float(opt.get("physical_grid_import_limit_kw", 13.8))
    elim = float(opt.get("grid_export_limit_kw", 10.0))
    energy = cap * max(hmin, min(hmax, float(initial_soc_pct))) / 100.0
    min_e = cap * hmin / 100.0
    max_e = cap * hmax / 100.0
    net = float(row["load_kw"]) - float(row["pv_kw"])
    requested = float(requested_action_kw)
    if requested >= 0.0:
        by_soc = max(0.0, energy - min_e) * ed / DT_HOURS
        by_export = max(0.0, net + elim)
        action = min(requested, dmax, by_soc, by_export)
    else:
        by_soc = max(0.0, max_e - energy) / max(1e-9, ec * DT_HOURS)
        by_import = max(0.0, ilim - net)
        action = -min(-requested, cmax, by_soc, by_import)
    return float(action), abs(float(action) - requested) > 1e-6


def _score_vintage(
    item: dict[str, Any], actuals: dict[datetime, dict[str, float]], cfg: dict[str, Any]
) -> dict[str, Any] | None:
    engine_input = item.get("input") or {}
    horizon = list(engine_input.get("horizon_rows") or ())[:ORACLE_HORIZON_INTERVALS]
    if not horizon:
        return None
    actual_rows: list[dict[str, float]] = []
    for row in horizon:
        try:
            stamp = _dt(str(row["start"])).replace(second=0, microsecond=0)
        except Exception:
            return None
        observed = actuals.get(stamp)
        if observed is None:
            return None
        actual_rows.append({"start": stamp.isoformat(), **observed})

    initial_soc = float(engine_input.get("initial_soc_pct") or 0.0)
    try:
        oracle = _oracle_first_action_values(actual_rows, cfg, initial_soc)
    except Exception:
        return None
    q_values = oracle["first_action_values"]
    oracle_action = float(oracle["oracle_action_kw"])
    oracle_value = float(oracle["oracle_value_ore"])
    first_actual = actual_rows[0]

    scores: dict[str, dict[str, Any]] = {}
    for engine_id, decision in (item.get("decisions") or {}).items():
        try:
            requested = float(decision["requested_action_kw"])
        except Exception:
            continue
        applied, clamped = _clamp_first_action(first_actual, requested, initial_soc, cfg)
        nearest = min(q_values, key=lambda q: abs(float(q["action_kw"]) - applied))
        evaluated_action = float(nearest["action_kw"])
        regret = max(0.0, float(nearest["value_ore"]) - oracle_value)
        scores[str(engine_id)] = {
            "requested_action_kw": requested,
            "applied_action_kw": applied,
            "evaluated_grid_action_kw": evaluated_action,
            "oracle_action_kw": oracle_action,
            "oracle_regret_ore": regret,
            "oracle_action_abs_error_kw": abs(evaluated_action - oracle_action),
            "oracle_direction_match": _sign(evaluated_action) == _sign(oracle_action),
            "clamped": bool(clamped),
            "first_interval_cash_cost_ore": _interval_cash_cost(first_actual, applied, cfg),
            "model": decision.get("model") or {},
        }
    return {
        "decision_start": item["decision_start"],
        "information_vintage_id": item["information_vintage_id"],
        "terminal_reference_ore_kwh": oracle["terminal_reference_ore_kwh"],
        "scores": scores,
    }


def _write_day_run(local_date: str, context: str, status: str, payload: dict[str, Any]) -> None:
    with sqlite3.connect(DB_PATH, timeout=20) as c:
        c.execute(
            '''INSERT OR REPLACE INTO engine_selector_day_run(
               local_date,context_signature,created_at,status,payload_json)
               VALUES (?,?,?,?,?)''',
            (local_date, context, _now(), status, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        )


def evaluate_selector_day(cfg: dict[str, Any], local_date: str, *, force: bool = False) -> dict[str, Any]:
    state = ensure_selector_state(cfg)
    context = state["context_signature"]
    day = date.fromisoformat(str(local_date))
    if not force and day.isoformat() < state["evaluation_start_date"]:
        return {"ok": True, "status": "pre_selector_epoch", "local_date": day.isoformat()}

    if not force:
        with sqlite3.connect(DB_PATH, timeout=20) as c:
            row = c.execute(
                '''SELECT status,payload_json FROM engine_selector_day_run
                   WHERE local_date=? AND context_signature=?''',
                (day.isoformat(), context),
            ).fetchone()
        if row and str(row[0]) == "complete":
            return {"ok": True, "status": "already_complete", **json.loads(row[1])}

    vintages = _competition_vintages(day)
    if len(vintages) < MIN_DAILY_INTERVALS:
        payload = {
            "local_date": day.isoformat(),
            "canonical_vintages": len(vintages),
            "required_daily_intervals": MIN_DAILY_INTERVALS,
            "reason": "insufficient canonical baseline decision vintages",
        }
        _write_day_run(day.isoformat(), context, "insufficient_decisions", payload)
        return {"ok": True, "status": "insufficient_decisions", **payload}

    starts: list[datetime] = []
    ends: list[datetime] = []
    for item in vintages:
        horizon = list((item.get("input") or {}).get("horizon_rows") or ())[:ORACLE_HORIZON_INTERVALS]
        if not horizon:
            continue
        starts.append(_dt(str(horizon[0]["start"])).replace(second=0, microsecond=0))
        ends.append(_dt(str(horizon[-1]["start"])).replace(second=0, microsecond=0) + timedelta(minutes=15))
    if not starts:
        payload = {"local_date": day.isoformat(), "reason": "no usable engine horizons"}
        _write_day_run(day.isoformat(), context, "insufficient_decisions", payload)
        return {"ok": True, "status": "insufficient_decisions", **payload}

    actuals = _actual_map(min(starts), max(ends))
    engine_obs: dict[str, list[dict[str, Any]]] = {}
    matured_vintages = 0
    for item in vintages:
        scored = _score_vintage(item, actuals, cfg)
        if scored is None:
            continue
        matured_vintages += 1
        for engine_id, obs in scored["scores"].items():
            engine_obs.setdefault(engine_id, []).append(obs)

    if matured_vintages < MIN_DAILY_INTERVALS:
        payload = {
            "local_date": day.isoformat(),
            "canonical_vintages": len(vintages),
            "matured_vintages": matured_vintages,
            "required_daily_intervals": MIN_DAILY_INTERVALS,
            "reason": "full 24h realized oracle horizon is not mature/complete yet",
        }
        _write_day_run(day.isoformat(), context, "waiting_for_actuals", payload)
        return {"ok": True, "status": "waiting_for_actuals", **payload}

    stored: dict[str, Any] = {}
    with sqlite3.connect(DB_PATH, timeout=20) as c:
        for engine_id, observations in sorted(engine_obs.items()):
            if len(observations) < MIN_DAILY_INTERVALS:
                continue
            regrets = [float(x["oracle_regret_ore"]) for x in observations]
            errors = [float(x["oracle_action_abs_error_kw"]) for x in observations]
            clamp_rate = sum(1 for x in observations if x["clamped"]) / len(observations)
            direction_accuracy = sum(1 for x in observations if x["oracle_direction_match"]) / len(observations)
            payload = {
                "local_date": day.isoformat(),
                "engine_id": engine_id,
                "context_signature": context,
                "intervals": len(observations),
                "coverage_fraction": round(len(observations) / max(1, len(vintages)), 6),
                "total_regret_ore": sum(regrets),
                "mean_regret_ore": mean(regrets),
                "median_regret_ore": median(regrets),
                "p90_regret_ore": _percentile(regrets, 0.90),
                "oracle_action_mae_kw": mean(errors),
                "oracle_direction_accuracy": direction_accuracy,
                "clamp_rate": clamp_rate,
                "first_interval_cash_cost_ore": sum(float(x["first_interval_cash_cost_ore"]) for x in observations),
                "score_semantics": "fixed_external_24h_oracle_first_action_regret",
                "terminal_asset_semantics": "median realized effective import price over oracle horizon",
            }
            c.execute(
                '''INSERT OR REPLACE INTO engine_daily_score(
                   local_date,engine_id,context_signature,intervals,mean_regret_ore,
                   p90_regret_ore,clamp_rate,payload_json,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)''',
                (
                    day.isoformat(),
                    engine_id,
                    context,
                    len(observations),
                    float(payload["mean_regret_ore"]),
                    float(payload["p90_regret_ore"]),
                    float(payload["clamp_rate"]),
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    _now(),
                ),
            )
            stored[engine_id] = payload
        c.execute(
            '''UPDATE engine_selector_state SET last_evaluated_date=?,updated_at=?
               WHERE singleton=1''',
            (day.isoformat(), _now()),
        )

    run_payload = {
        "local_date": day.isoformat(),
        "canonical_vintages": len(vintages),
        "matured_vintages": matured_vintages,
        "engines_scored": sorted(stored),
        "scores": stored,
    }
    _write_day_run(day.isoformat(), context, "complete", run_payload)
    return {"ok": True, "status": "complete", **run_payload}


def _paired_daily_scores(
    context: str, challenger: str, incumbent: str, limit: int
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    with sqlite3.connect(DB_PATH, timeout=20) as c:
        rows = c.execute(
            '''SELECT a.local_date,a.payload_json,b.payload_json
               FROM engine_daily_score a
               JOIN engine_daily_score b
                 ON b.local_date=a.local_date AND b.context_signature=a.context_signature
               WHERE a.context_signature=? AND a.engine_id=? AND b.engine_id=?
               ORDER BY a.local_date DESC LIMIT ?''',
            (context, challenger, incumbent, max(1, int(limit))),
        ).fetchall()
    parsed = [(str(d), json.loads(a), json.loads(b)) for d, a, b in rows]
    parsed.reverse()
    return parsed


def _promotion_gate(context: str, challenger: str, incumbent: str) -> dict[str, Any]:
    pairs = _paired_daily_scores(context, challenger, incumbent, WINDOW_DAYS)
    cvals = [float(c["mean_regret_ore"]) for _, c, _ in pairs]
    ivals = [float(i["mean_regret_ore"]) for _, _, i in pairs]
    cclamp = [float(c["clamp_rate"]) for _, c, _ in pairs]
    iclamp = [float(i["clamp_rate"]) for _, _, i in pairs]
    if not pairs:
        return {
            "challenger_engine_id": challenger,
            "incumbent_engine_id": incumbent,
            "paired_days": 0,
            "eligible": False,
            "reason": "no paired daily scores",
        }
    cmean, imean = mean(cvals), mean(ivals)
    absolute_improvement = imean - cmean
    relative_improvement = absolute_improvement / max(1e-9, imean)
    win_rate = sum(1 for c, i in zip(cvals, ivals) if c < i) / len(pairs)
    ctail, itail = _percentile(cvals, 0.90), _percentile(ivals, 0.90)
    cmedian, imedian = median(cvals), median(ivals)
    cclamp_mean, iclamp_mean = mean(cclamp), mean(iclamp)
    gates = {
        "minimum_days": len(pairs) >= MIN_PROMOTION_DAYS,
        "mean_relative_improvement": relative_improvement >= MIN_RELATIVE_MEAN_IMPROVEMENT,
        "mean_absolute_improvement": absolute_improvement >= MIN_ABSOLUTE_MEAN_IMPROVEMENT_ORE,
        "median_improvement": cmedian < imedian,
        "win_rate": win_rate >= MIN_WIN_RATE,
        "tail_not_materially_worse": ctail <= itail * MAX_TAIL_RATIO + TAIL_ABSOLUTE_TOLERANCE_ORE,
        "clamp_rate_not_worse": cclamp_mean <= iclamp_mean + MAX_CLAMP_RATE_DELTA,
    }
    return {
        "challenger_engine_id": challenger,
        "incumbent_engine_id": incumbent,
        "paired_days": len(pairs),
        "window_days": WINDOW_DAYS,
        "first_day": pairs[0][0],
        "last_day": pairs[-1][0],
        "challenger_mean_regret_ore": cmean,
        "incumbent_mean_regret_ore": imean,
        "absolute_improvement_ore_per_decision": absolute_improvement,
        "relative_improvement_fraction": relative_improvement,
        "challenger_median_daily_regret_ore": cmedian,
        "incumbent_median_daily_regret_ore": imedian,
        "win_rate": win_rate,
        "challenger_p90_daily_regret_ore": ctail,
        "incumbent_p90_daily_regret_ore": itail,
        "challenger_clamp_rate": cclamp_mean,
        "incumbent_clamp_rate": iclamp_mean,
        "gates": gates,
        "eligible": all(gates.values()),
    }


def _live_health(selected_engine_id: str) -> dict[str, Any]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    _init_tables()
    with sqlite3.connect(DB_PATH, timeout=20) as c:
        rows = c.execute(
            '''SELECT fallback_used,routed_engine_id FROM engine_control_selection
               WHERE created_at>=? AND configured_selected_engine_id=?''',
            (cutoff, selected_engine_id),
        ).fetchall()
    attempts = len(rows)
    fallbacks = sum(int(r[0]) for r in rows)
    return {
        "window_hours": 24,
        "configured_selected_engine_id": selected_engine_id,
        "selections": attempts,
        "fallbacks": fallbacks,
        "fallback_rate": fallbacks / attempts if attempts else 0.0,
        "healthy": attempts < LIVE_HEALTH_MIN_SELECTIONS or fallbacks / attempts < LIVE_HEALTH_MAX_FALLBACK_RATE,
    }


def _rollback_gate(context: str, selected: str) -> dict[str, Any]:
    if selected == BASELINE_ENGINE_ID:
        return {"eligible": False, "reason": "baseline is selected"}
    pairs = _paired_daily_scores(context, selected, BASELINE_ENGINE_ID, ROLLBACK_WINDOW_DAYS)
    if len(pairs) < MIN_ROLLBACK_DAYS:
        return {"eligible": False, "paired_days": len(pairs), "reason": "insufficient rollback window"}
    svals = [float(s["mean_regret_ore"]) for _, s, _ in pairs]
    bvals = [float(b["mean_regret_ore"]) for _, _, b in pairs]
    smean, bmean = mean(svals), mean(bvals)
    degradation = smean - bmean
    relative = degradation / max(1e-9, bmean)
    baseline_win_rate = sum(1 for s, b in zip(svals, bvals) if b < s) / len(pairs)
    eligible = (
        degradation >= ROLLBACK_ABSOLUTE_DEGRADATION_ORE
        and relative >= ROLLBACK_RELATIVE_DEGRADATION
        and baseline_win_rate >= 2.0 / 3.0
    )
    return {
        "eligible": eligible,
        "paired_days": len(pairs),
        "selected_mean_regret_ore": smean,
        "baseline_mean_regret_ore": bmean,
        "absolute_degradation_ore_per_decision": degradation,
        "relative_degradation_fraction": relative,
        "baseline_win_rate": baseline_win_rate,
    }


def run_selection_policy(cfg: dict[str, Any]) -> dict[str, Any]:
    state = ensure_selector_state(cfg)
    selected = state["selected_engine_id"]
    context = state["context_signature"]
    live_health = _live_health(selected)

    # Missing/failed selected decisions trigger immediate permanent rollback once
    # there is enough live evidence. Every individual missing decision already
    # falls back to deterministic_v35 in route_selected_decision().
    if selected != BASELINE_ENGINE_ID and not live_health["healthy"]:
        new_state = _set_selected_engine(
            cfg,
            BASELINE_ENGINE_ID,
            "24h live fallback rate exceeded selector safety threshold",
            "rollback_live_health",
            live_health,
        )
        return {"action": "rollback", "state": new_state, "live_health": live_health}

    tariffs_enabled = bool((cfg.get("tariffs") or {}).get("enabled", False))
    rollback = _rollback_gate(context, selected)
    if not tariffs_enabled and selected != BASELINE_ENGINE_ID and rollback.get("eligible"):
        new_state = _set_selected_engine(
            cfg,
            BASELINE_ENGINE_ID,
            "selected engine materially underperformed frozen baseline in rollback window",
            "rollback_performance",
            rollback,
        )
        return {"action": "rollback", "state": new_state, "rollback_gate": rollback}

    cooldown_until = state.get("cooldown_until")
    if cooldown_until and _dt(str(cooldown_until)) > datetime.now(timezone.utc):
        return {
            "action": "hold",
            "reason": "promotion_cooldown",
            "state": state,
            "live_health": live_health,
            "rollback_gate": rollback,
        }
    if tariffs_enabled:
        return {
            "action": "hold",
            "reason": "auto promotion blocked while demand-tariff objective is enabled",
            "state": state,
            "live_health": live_health,
            "rollback_gate": rollback,
        }

    with sqlite3.connect(DB_PATH, timeout=20) as c:
        candidates = [
            str(r[0])
            for r in c.execute(
                '''SELECT DISTINCT engine_id FROM engine_daily_score
                   WHERE context_signature=? AND engine_id<>? ORDER BY engine_id''',
                (context, selected),
            ).fetchall()
            if str(r[0]) != BASELINE_ENGINE_ID
        ]
    assessments = [_promotion_gate(context, candidate, selected) for candidate in candidates]
    eligible = [a for a in assessments if a.get("eligible")]
    if not eligible:
        return {
            "action": "hold",
            "reason": "no challenger passed promotion gates",
            "state": state,
            "live_health": live_health,
            "rollback_gate": rollback,
            "challengers": assessments,
        }

    winner = max(
        eligible,
        key=lambda a: (
            float(a.get("relative_improvement_fraction") or 0.0),
            float(a.get("absolute_improvement_ore_per_decision") or 0.0),
        ),
    )
    new_state = _set_selected_engine(
        cfg,
        str(winner["challenger_engine_id"]),
        "challenger passed sustained multi-day promotion gates",
        "promotion",
        winner,
    )
    return {
        "action": "promote",
        "winner": winner,
        "state": new_state,
        "challengers": assessments,
        "physical_writes_enabled": False,
    }


def route_selected_decision(
    cfg: dict[str, Any], information_vintage_id: str, decision_start: str
) -> dict[str, Any]:
    state = ensure_selector_state(cfg)
    selected = state["selected_engine_id"]
    _init_tables()
    with sqlite3.connect(DB_PATH, timeout=20) as c:
        rows = c.execute(
            '''SELECT engine_id,decision_id,status,requested_action_kw,payload_json
               FROM engine_decision WHERE information_vintage_id=?''',
            (str(information_vintage_id),),
        ).fetchall()
    decisions: dict[str, dict[str, Any]] = {}
    for engine_id, decision_id, status, action, raw in rows:
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
            "payload": payload,
        }

    fallback_used = False
    reason = "selected_engine_available"
    chosen = decisions.get(selected)
    if chosen is None:
        chosen = decisions.get(BASELINE_ENGINE_ID)
        fallback_used = selected != BASELINE_ENGINE_ID
        reason = "selected_engine_missing_fallback_to_deterministic_v35"
    if chosen is None:
        reason = "no_baseline_decision_available"

    result = {
        "information_vintage_id": str(information_vintage_id),
        "decision_start": str(decision_start),
        "configured_selected_engine_id": selected,
        "routed_engine_id": None if chosen is None else chosen["engine_id"],
        "decision_id": None if chosen is None else chosen["decision_id"],
        "requested_action_kw": None if chosen is None else chosen["requested_action_kw"],
        "fallback_used": bool(fallback_used),
        "reason": reason,
        "requires_downstream_deterministic_safety": True,
        "physical_writes_enabled": False,
    }
    with sqlite3.connect(DB_PATH, timeout=20) as c:
        c.execute(
            '''INSERT OR REPLACE INTO engine_control_selection(
               information_vintage_id,decision_start,created_at,configured_selected_engine_id,
               routed_engine_id,decision_id,requested_action_kw,fallback_used,reason,payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (
                str(information_vintage_id),
                str(decision_start),
                _now(),
                selected,
                result["routed_engine_id"],
                result["decision_id"],
                result["requested_action_kw"],
                1 if fallback_used else 0,
                reason,
                json.dumps(result, ensure_ascii=False, sort_keys=True),
            ),
        )
    return result


def latest_control_selection() -> dict[str, Any] | None:
    _init_tables()
    with sqlite3.connect(DB_PATH, timeout=20) as c:
        row = c.execute(
            '''SELECT payload_json FROM engine_control_selection
               ORDER BY decision_start DESC,created_at DESC LIMIT 1'''
        ).fetchone()
    return None if not row else json.loads(row[0])


def selector_scores(cfg: dict[str, Any], days: int = 30) -> dict[str, Any]:
    state = ensure_selector_state(cfg)
    context = state["context_signature"]
    with sqlite3.connect(DB_PATH, timeout=20) as c:
        rows = c.execute(
            '''SELECT local_date,engine_id,payload_json FROM engine_daily_score
               WHERE context_signature=? ORDER BY local_date DESC,engine_id LIMIT ?''',
            (context, max(1, min(int(days), 180)) * 8),
        ).fetchall()
    return {
        "context_signature": context,
        "selected_engine_id": state["selected_engine_id"],
        "scores": [json.loads(r[2]) for r in rows],
    }


def selector_status(cfg: dict[str, Any]) -> dict[str, Any]:
    state = ensure_selector_state(cfg)
    context = state["context_signature"]
    with sqlite3.connect(DB_PATH, timeout=20) as c:
        engines = [
            str(r[0])
            for r in c.execute(
                "SELECT DISTINCT engine_id FROM engine_daily_score WHERE context_signature=? ORDER BY engine_id",
                (context,),
            ).fetchall()
        ]
        day_counts = {
            str(engine): int(count)
            for engine, count in c.execute(
                '''SELECT engine_id,COUNT(*) FROM engine_daily_score
                   WHERE context_signature=? GROUP BY engine_id''',
                (context,),
            ).fetchall()
        }
        recent_events = [
            {
                "created_at": r[0],
                "event_type": r[1],
                "from_engine_id": r[2],
                "to_engine_id": r[3],
                "reason": r[4],
                "payload": json.loads(r[5]),
            }
            for r in c.execute(
                '''SELECT created_at,event_type,from_engine_id,to_engine_id,reason,payload_json
                   FROM engine_selector_event ORDER BY event_id DESC LIMIT 10'''
            ).fetchall()
        ]
    assessments = [
        _promotion_gate(context, engine, state["selected_engine_id"])
        for engine in engines
        if engine not in {state["selected_engine_id"], BASELINE_ENGINE_ID}
    ]
    return {
        "logical_control_selection_enabled": True,
        "physical_writes_enabled": False,
        "state": state,
        "score_semantics": "fixed_external_24h_oracle_first_action_regret",
        "baseline_engine_id": BASELINE_ENGINE_ID,
        "daily_score_counts": day_counts,
        "promotion_policy": {
            "window_days": WINDOW_DAYS,
            "minimum_paired_days": MIN_PROMOTION_DAYS,
            "minimum_daily_intervals": MIN_DAILY_INTERVALS,
            "minimum_relative_mean_improvement": MIN_RELATIVE_MEAN_IMPROVEMENT,
            "minimum_absolute_mean_improvement_ore_per_decision": MIN_ABSOLUTE_MEAN_IMPROVEMENT_ORE,
            "minimum_win_rate": MIN_WIN_RATE,
            "maximum_tail_ratio": MAX_TAIL_RATIO,
            "maximum_clamp_rate_delta": MAX_CLAMP_RATE_DELTA,
            "cooldown_days": PROMOTION_COOLDOWN_DAYS,
            "demand_tariff_auto_promotion_supported": False,
        },
        "rollback_policy": {
            "window_days": ROLLBACK_WINDOW_DAYS,
            "minimum_paired_days": MIN_ROLLBACK_DAYS,
            "relative_degradation": ROLLBACK_RELATIVE_DEGRADATION,
            "absolute_degradation_ore_per_decision": ROLLBACK_ABSOLUTE_DEGRADATION_ORE,
            "live_24h_max_fallback_rate": LIVE_HEALTH_MAX_FALLBACK_RATE,
        },
        "live_health": _live_health(state["selected_engine_id"]),
        "rollback_gate": _rollback_gate(context, state["selected_engine_id"]),
        "challengers": assessments,
        "latest_control_selection": latest_control_selection(),
        "recent_events": recent_events,
    }


def automatic_selector_maintenance_once(cfg: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    state = ensure_selector_state(cfg)
    today = datetime.now(LOCAL_TZ).date()
    max_mature_day = today - timedelta(days=2)
    start_day = date.fromisoformat(state["evaluation_start_date"])
    evaluations: list[dict[str, Any]] = []
    if start_day <= max_mature_day:
        day = start_day
        while day <= max_mature_day and len(evaluations) < 3:
            context = state["context_signature"]
            with sqlite3.connect(DB_PATH, timeout=20) as c:
                row = c.execute(
                    '''SELECT status FROM engine_selector_day_run
                       WHERE local_date=? AND context_signature=?''',
                    (day.isoformat(), context),
                ).fetchone()
            if force or not row or str(row[0]) != "complete":
                evaluations.append(evaluate_selector_day(cfg, day.isoformat(), force=force))
            day += timedelta(days=1)
    selection = run_selection_policy(cfg)
    return {
        "ok": True,
        "evaluations": evaluations,
        "selection": selection,
        "status": selector_status(cfg),
    }
