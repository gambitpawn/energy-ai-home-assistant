from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from .actuator_release_state import release_status
from .actuator_timing_v194 import candidate_start_status
from .dashboard import DASHBOARD_HTML
from .db import DB_PATH
from .production_state import set_mode, status as production_status
from .runtime_ui import CURRENT_UI_EXTENSION


OPERATOR_MODE_EXTENSION = r'''
<style>
.operator-mode-card{margin-bottom:14px;border-color:#40556a}.operator-mode-head{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}.operator-mode-copy{min-width:0}.operator-mode-copy h2{margin:0 0 4px}.operator-mode-copy p{margin:0;color:var(--muted);font-size:11px;line-height:1.45}.operator-mode-switch{display:flex;gap:4px;padding:4px;border:1px solid var(--line);border-radius:10px;background:var(--panel2)}.operator-mode-btn{border:0;border-radius:7px;padding:8px 15px;background:transparent;color:var(--muted);font:inherit;font-size:12px;font-weight:700;cursor:pointer}.operator-mode-btn:hover{color:var(--text)}.operator-mode-btn.is-selected{background:#24384a;color:#fff}.operator-mode-btn[data-mode="active"].is-selected{background:#28483a}.operator-mode-btn:disabled{opacity:.55;cursor:wait}.operator-mode-state{margin-top:9px;font-size:11px;color:var(--muted)}.operator-mode-state strong{color:var(--text)}.operator-mode-error{color:var(--bad)}
@media(max-width:700px){.operator-mode-head{align-items:flex-start;flex-direction:column}.operator-mode-switch{width:100%}.operator-mode-btn{flex:1}}
</style>
<script>
let operatorModeBusy=false;
function operatorModeCard(){
  const p=$('parameters');
  if(!p||$('operatorModeControl'))return;
  p.insertAdjacentHTML('afterbegin',`<div class="card operator-mode-card" id="operatorModeControl"><div class="operator-mode-head"><div class="operator-mode-copy"><h2>Operating mode</h2><p>Shadow never writes battery commands. Active performs preflight, arming and the current safe command automatically. Future quarter decisions still wait for their decision time.</p></div><div class="operator-mode-switch"><button type="button" class="operator-mode-btn" data-mode="shadow">Shadow</button><button type="button" class="operator-mode-btn" data-mode="active">Active</button></div></div><div class="operator-mode-state" id="operatorModeState">Loading operating mode…</div></div>`);
  $('operatorModeControl').querySelectorAll('[data-mode]').forEach(btn=>btn.addEventListener('click',()=>setOperatorMode(btn.dataset.mode)));
}
function paintOperatorMode(d){
  operatorModeCard();
  const selected=d.selected_mode||'shadow',prod=d.production||{},state=$('operatorModeState');
  $('operatorModeControl').querySelectorAll('[data-mode]').forEach(btn=>{btn.classList.toggle('is-selected',btn.dataset.mode===selected);btn.disabled=operatorModeBusy});
  if(!state)return;
  const raw=prod.operating_mode||selected;
  if(selected==='active')state.innerHTML=`<strong>ACTIVE</strong> · physical writes enabled · actuator ${prod.actuator_ready?'ready':'not ready'}`;
  else if(raw==='paused')state.innerHTML=`<strong>SHADOW</strong> · runtime is paused after a fault; selecting Active will run the full activation sequence again.`;
  else state.innerHTML=`<strong>SHADOW</strong> · no physical battery writes${prod.actuator_ready?' · actuator armed':''}`;
}
async function loadOperatorMode(){
  operatorModeCard();
  try{paintOperatorMode(await api('control/operator-mode'))}catch(e){const s=$('operatorModeState');if(s){s.textContent=`Could not read operating mode: ${e.message}`;s.classList.add('operator-mode-error')}}
}
async function setOperatorMode(mode){
  if(operatorModeBusy)return;
  operatorModeBusy=true;
  const s=$('operatorModeState');if(s){s.classList.remove('operator-mode-error');s.textContent=mode==='active'?'Activating: preflight → arm → current command…':'Switching to Shadow and safely releasing Solinteg…'}
  $('operatorModeControl')?.querySelectorAll('[data-mode]').forEach(btn=>btn.disabled=true);
  try{
    const r=await api(`control/operator-mode/${mode}`,{method:'POST'});
    paintOperatorMode(r);
    if(s&&mode==='active'){
      const src=r.activation_candidate_source||'current candidate';
      const target=r.actuation?.physical_target_kw??r.actuation?.safe_action_kw;
      s.innerHTML=`<strong>ACTIVE</strong> · ${src}${target==null?'':` · target ${Number(target).toFixed(2)} kW`}`;
    }
  }catch(e){
    if(s){s.textContent=`Mode change failed: ${e.message}`;s.classList.add('operator-mode-error')}
    await loadOperatorMode();
  }finally{operatorModeBusy=false;await loadOperatorMode()}
}
operatorModeCard();loadOperatorMode();
$('tabs')?.addEventListener('click',e=>{if(e.target.closest('.tab')?.dataset.view==='parameters')loadOperatorMode()});
</script>
'''


def _utc(value: str) -> datetime:
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def selection_at_or_before(at: datetime) -> dict[str, Any] | None:
    """Return the freshest routed selector decision whose interval has started."""
    at_utc = at.astimezone(timezone.utc)
    try:
        with sqlite3.connect(DB_PATH, timeout=20) as c:
            row = c.execute(
                '''SELECT payload_json FROM engine_control_selection
                   WHERE decision_start<=?
                   ORDER BY decision_start DESC,created_at DESC LIMIT 1''',
                (at_utc.isoformat(),),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    try:
        payload = json.loads(row[0] or "{}")
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def candidate_is_current(candidate: dict[str, Any] | None, at: datetime) -> bool:
    """Activation is strict: grace may protect watchdog continuity, never start an old interval."""
    if not candidate or not candidate.get("decision_start") or not candidate.get("valid_until"):
        return False
    try:
        start = _utc(str(candidate["decision_start"]))
        end = _utc(str(candidate["valid_until"]))
    except Exception:
        return False
    now = at.astimezone(timezone.utc)
    return start <= now < end and candidate.get("requested_action_kw") is not None


def zero_hold_candidate(at: datetime) -> dict[str, Any]:
    """Safe immediate ownership until the next quarter when no current selector decision exists."""
    now = at.astimezone(timezone.utc)
    quarter = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    next_quarter = quarter + timedelta(minutes=15)
    return {
        "source": "operator_activation_zero_hold",
        "source_id": f"operator:{now.isoformat()}",
        "engine_id": "operator_zero_hold",
        "decision_start": now.isoformat(),
        "valid_until": next_quarter.isoformat(),
        "requested_action_kw": 0.0,
        "selector_fallback_used": False,
        "selector_reason": "no_current_selector_candidate_zero_hold_until_next_boundary",
    }


def _remove_get_ui(app: FastAPI) -> None:
    kept = []
    for route in app.router.routes:
        if getattr(route, "path", None) != "/ui":
            kept.append(route)
            continue
        methods = {str(m).upper() for m in (getattr(route, "methods", None) or set())}
        if "GET" not in methods:
            kept.append(route)
    app.router.routes[:] = kept


def install_operator_mode_control(
    *,
    app: FastAPI,
    core,
    actuator,
    adapter,
    timing_scheduler,
    selector_module,
    candidate_from_selection: Callable[[dict[str, Any] | None], dict[str, Any] | None],
) -> None:
    if getattr(app.state, "operator_mode_control_installed", False):
        return
    app.state.operator_mode_control_installed = True

    # Replace only the HTML renderer. Existing consolidated UI/API routes remain intact.
    _remove_get_ui(app)

    @app.get("/ui", response_class=HTMLResponse, include_in_schema=False)
    async def current_ui_with_operator_mode():
        return DASHBOARD_HTML.replace("</body>", CURRENT_UI_EXTENSION + OPERATOR_MODE_EXTENSION + "</body>")

    def payload(extra: dict[str, Any] | None = None) -> dict[str, Any]:
        prod = production_status()
        selected = "active" if prod.get("operating_mode") == "active" and prod.get("physical_writes_enabled") else "shadow"
        return {
            "ok": True,
            "selected_mode": selected,
            "production": prod,
            "startup_policy": "safe_default_shadow_after_addon_restart",
            "activation_policy": "preflight_arm_current_candidate_or_zero_hold",
            "decision_start_policy": "future_candidates_remain_pending_until_start",
            **(extra or {}),
        }

    async def queue_latest_future(now: datetime) -> dict[str, Any] | None:
        latest = await asyncio.to_thread(selector_module.latest_control_selection)
        candidate = candidate_from_selection(latest)
        if candidate is None:
            return None
        timing = candidate_start_status(candidate, now=now)
        if timing.get("state") != "future":
            return None
        return await actuator.process_candidate(candidate)

    @app.get("/control/operator-mode", tags=["control"])
    async def operator_mode_status():
        return payload({"timing": timing_scheduler.status()})

    @app.post("/control/operator-mode/shadow", tags=["control"])
    async def operator_mode_shadow():
        current = production_status()
        if current.get("operating_mode") == "shadow" and not current.get("physical_writes_enabled") and not current.get("actuator_ready"):
            return payload({"transition": "already_shadow"})
        result = await actuator.disarm("operator_mode_shadow")
        if not result.get("ok"):
            raise HTTPException(503, {"error": "safe_release_failed", "result": result})
        return payload({"transition": "safe_release_and_disarm", "disarm": result})

    @app.post("/control/operator-mode/active", tags=["control"])
    async def operator_mode_active():
        current = production_status()
        if current.get("operating_mode") == "active" and current.get("physical_writes_enabled") and current.get("actuator_ready"):
            queued = await queue_latest_future(datetime.now(timezone.utc))
            return payload({"transition": "already_active", "queued_future": queued, "timing": timing_scheduler.status()})

        release = None
        if release_status().get("release_pending"):
            release = await adapter.safe_release()
            if not release.get("released"):
                raise HTTPException(503, {"error": "pending_safe_release_failed", "safe_release": release})

        preflight = await actuator.preflight()
        if not preflight.get("ok"):
            raise HTTPException(409, {"error": "actuator_preflight_failed", "preflight": preflight})

        arm = None
        if not production_status().get("actuator_ready"):
            arm = await actuator.zero_handshake_and_arm()
            if not arm.get("ok"):
                raise HTTPException(409, {"error": "actuator_arm_failed", "arm": arm})

        # Resolve the decision only after the physical zero-handshake. This avoids
        # activating the previous interval if a quarter boundary passed during arm.
        now = datetime.now(timezone.utc)
        selection = await asyncio.to_thread(selection_at_or_before, now)
        candidate = candidate_from_selection(selection)
        activation_source = "selector_current_interval"
        if not candidate_is_current(candidate, now):
            candidate = zero_hold_candidate(now)
            activation_source = "zero_hold_until_next_quarter"

        async def enable_active() -> dict[str, Any]:
            return await asyncio.to_thread(set_mode, "active", reason="operator_mode_active")

        try:
            actuation = await timing_scheduler.activate_with(candidate, enable_active)
        except Exception as exc:
            try:
                await actuator.fail_safe("operator_active_transition_failed", {"error": repr(exc), "candidate": candidate})
            except Exception:
                pass
            raise HTTPException(500, f"Operator ACTIVE transition failed: {exc!r}")

        if actuation.get("status") not in {"acknowledged", "held_existing"}:
            try:
                await actuator.fail_safe("operator_active_unacknowledged", {"actuation": actuation})
            except Exception:
                pass
            raise HTTPException(409, {"error": "active_command_not_acknowledged", "actuation": actuation})

        queued = await queue_latest_future(datetime.now(timezone.utc))
        return payload({
            "transition": "activated",
            "preflight": preflight,
            "arm": arm,
            "recovered_release": release,
            "activation_candidate_source": activation_source,
            "activation_candidate": candidate,
            "actuation": actuation,
            "queued_future": queued,
            "timing": timing_scheduler.status(),
        })

    app.openapi_schema = None
