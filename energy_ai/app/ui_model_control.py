from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .engine_operator_selection import (
    control_status,
    race_ranking,
    set_operator_preference,
)


MODELS_CONTROL_EXTENSION = r'''
<style>
.model-control-card{margin:0 0 12px;display:flex;align-items:center;justify-content:space-between;gap:18px;flex-wrap:wrap}.model-control-main{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.model-control-label{font-size:11px;color:var(--muted);font-weight:750;text-transform:uppercase;letter-spacing:.04em}.model-control-select{min-width:220px;border:1px solid var(--line);background:var(--panel2);color:var(--text);border-radius:9px;padding:9px 11px;font-weight:650}.model-current{font-size:12px;color:var(--muted)}.model-current strong{color:var(--text)}.model-control-note{font-size:11px;color:var(--muted);max-width:600px}.ranking-card{margin:0 0 12px}.ranking-head{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;margin-bottom:8px}.ranking-sub{font-size:11px;color:var(--muted)}.ranking-list{display:grid;gap:6px}.ranking-row{display:grid;grid-template-columns:42px minmax(180px,1.2fr) minmax(150px,.8fr) minmax(110px,.6fr) minmax(120px,.7fr);gap:10px;align-items:center;padding:9px 10px;border:1px solid var(--line);border-radius:10px;background:#0f1720}.ranking-pos{font-size:17px;font-weight:780}.ranking-name{font-weight:680}.ranking-engine{font-size:10px;color:var(--muted);margin-top:1px}.ranking-state{font-size:11px}.ranking-metric{font-variant-numeric:tabular-nums;font-size:12px}.ranking-detail{font-size:10px;color:var(--muted)}.rank-good{color:var(--good)}.rank-bad{color:var(--bad)}.rank-warn{color:var(--warn)}
@media(max-width:850px){.ranking-row{grid-template-columns:38px 1fr 1fr}.ranking-row .ranking-extra{grid-column:2/-1}.model-control-card{align-items:flex-start}}
</style>
<script>
const MODEL_LABELS={deterministic_v35:'Deterministic v3.5',adaptive_deterministic_v1:'Adaptive deterministic',neural_v1:'Neural v1',hybrid_v1:'Hybrid v1'};
function modelDisplayName(id){return MODEL_LABELS[id]||id||'—'}
function installModelControlUi(){
  const section=$('models');if(!section||$('modelControlCard'))return;
  section.insertAdjacentHTML('afterbegin',`<div id="modelControlCard" class="card model-control-card"><div class="model-control-main"><span class="model-control-label">Control engine</span><select id="modelControlSelect" class="model-control-select"><option value="auto">Auto</option></select><span id="modelControlCurrent" class="model-current"></span></div><div id="modelControlNote" class="model-control-note">Loading engine control…</div></div><div id="modelRankingCard" class="card ranking-card"><div class="ranking-head"><div><h2 style="margin:0">Current model ranking</h2><div class="ranking-sub">Auto selector race · lower oracle regret is better</div></div><div id="rankingPolicy" class="ranking-sub"></div></div><div id="modelRanking" class="ranking-list"><div class="empty" style="height:90px">Loading ranking…</div></div><div id="rankingNote" class="model-note"></div></div>`);
  $('modelControlSelect').onchange=async e=>{
    const value=e.target.value;$('modelControlNote').textContent='Saving selection…';
    try{
      const r=await api('ui/model-control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({selection:value})});
      renderModelControl(r);await loadModelRanking();
    }catch(err){$('modelControlNote').textContent=`Could not change engine: ${err.message}`;await loadModelControl()}
  };
}
function renderModelControl(d){
  if(!d)return;const sel=$('modelControlSelect');if(!sel)return;
  const choices=d.choices||[];
  sel.innerHTML=choices.map(x=>`<option value="${esc(x.value)}" ${x.available===false?'':' '}>${esc(x.label)}${x.available===false?' · unavailable':''}</option>`).join('');
  sel.value=d.selection||'auto';
  if(d.mode==='auto'){
    $('modelControlCurrent').innerHTML=`Current: <strong>${esc(d.auto_selected_engine_label||modelDisplayName(d.auto_selected_engine_id))}</strong>`;
    $('modelControlNote').textContent='Auto uses the robust selector winner. Promotion, rollback and qualification continue automatically.';
  }else{
    $('modelControlCurrent').innerHTML=`Manual: <strong>${esc(d.requested_engine_label||modelDisplayName(d.manual_engine_id))}</strong>`;
    const routed=d.latest_routing?.routed_engine_id;
    $('modelControlNote').textContent=`Manual routing only; the Auto race continues in the background.${routed&&routed!==d.manual_engine_id?` Latest routing fell back to ${modelDisplayName(routed)}.`:''}`;
  }
}
async function loadModelControl(){try{renderModelControl(await api('ui/model-control'))}catch(e){if($('modelControlNote'))$('modelControlNote').textContent=`Engine control unavailable: ${e.message}`}}
function rankingStateLabel(row){
  const s=row.qualification_state;
  if(s==='incumbent')return 'Auto incumbent';if(s==='qualified')return 'Qualified';if(s==='evaluating')return `Evaluating ${row.paired_days||0}/${row.required_days||10}`;if(s==='not_qualified')return 'Not qualified';if(s==='quarantined')return 'Quarantined';if(s==='fallback_reference')return 'Fallback reference';return 'Waiting for model/data';
}
function renderModelRanking(d){
  if(!d)return;$('rankingPolicy').textContent=d.policy||'';$('rankingNote').textContent=(d.ranking_semantics||'')+' Manual engine selection does not change this Auto ranking.';
  const rows=d.rows||[];
  if(!rows.length){$('modelRanking').innerHTML='<div class="empty" style="height:90px">No ranking data yet.</div>';return}
  $('modelRanking').innerHTML=rows.map(r=>{
    const rel=r.relative_improvement_fraction,cls=rel==null?'':rel>0?'rank-good':rel<0?'rank-bad':'',relText=rel==null?'—':`${rel>=0?'+':''}${n(100*Number(rel),1)}%`;
    const wins=r.win_days==null?'—':`${r.win_days}/${r.required_win_days||7}`;
    return `<div class="ranking-row" title="${esc(r.reason||'')}"><div class="ranking-pos">#${r.rank}</div><div><div class="ranking-name">${esc(r.label||modelDisplayName(r.engine_id))}</div><div class="ranking-engine">${esc(r.engine_id)}</div></div><div><div class="ranking-state ${r.qualification_state==='qualified'?'rank-good':r.qualification_state==='quarantined'?'rank-bad':r.qualification_state==='evaluating'?'rank-warn':''}">${esc(rankingStateLabel(r))}</div><div class="ranking-detail">${esc(r.reason||'')}</div></div><div class="ranking-extra"><div class="ranking-metric ${cls}">${relText}</div><div class="ranking-detail">vs Auto incumbent</div></div><div class="ranking-extra"><div class="ranking-metric">${wins}</div><div class="ranking-detail">win days</div></div></div>`
  }).join('');
}
async function loadModelRanking(){try{renderModelRanking(await api('ui/model-ranking'))}catch(e){if($('modelRanking'))$('modelRanking').innerHTML=`<div class="empty" style="height:90px">Ranking unavailable: ${esc(e.message)}</div>`}}

// Models-only renderer: use semantic labels so chart tooltips never fall back to
// "Series 1", "Series 2", etc. Overview rendering is deliberately untouched.
renderModels=function(){
  if(!modelData)return;
  if(modelMode==='economic'){
    const ids=Object.keys(modelData.economics||{}).filter(id=>modelEnabled[id]),allDates=[...new Set(ids.flatMap(id=>(modelData.economics[id]||[]).map(r=>r.date)))].sort(),palette=[C.load,C.pv,C.battery,C.price,C.gridImport];
    const series=ids.map((id,ix)=>{const map=Object.fromEntries((modelData.economics[id]||[]).map(r=>[r.date,r.cumulative_oracle_regret_sek]));let last=0;return {label:modelDisplayName(id),kind:id,axis:'power',color:palette[ix%palette.length],on:true,values:allDates.map(d=>{if(map[d]!=null)last=Number(map[d]);return last}),width:2.4}});
    $('modelChartTitle').textContent='Cumulative realized oracle regret';$('modelNote').textContent='SEK relative to the perfect-information oracle on the latest mature scored days. Lower is better.';lineChart($('modelChart'),series,allDates.map(d=>`${d}T12:00:00Z`));return;
  }
  const ids=Object.keys(modelData.behaviour||{}).filter(id=>modelEnabled[id]),all=[...new Set(ids.flatMap(id=>(modelData.behaviour[id]||[]).map(r=>r.start)))].sort(),palette=[C.load,C.pv,C.battery,C.price,C.gridImport],isSoc=modelBehaviourMetric==='soc',field=isSoc?'expected_soc_pct':'requested_action_kw';
  const series=ids.map((id,ix)=>{const map=Object.fromEntries((modelData.behaviour[id]||[]).map(r=>[r.start,r[field]]));return {label:modelDisplayName(id),kind:id,axis:isSoc?'soc':'power',color:palette[ix%palette.length],on:true,values:all.map(t=>map[t]??null),width:2.1}});
  $('modelChartTitle').textContent=isSoc?'Expected SOC by model':'Battery action by model';$('modelNote').textContent=isSoc?'Expected battery SOC after each model decision on the same stored information vintages.':'Positive = discharge, negative = charge. Decisions share the same stored information vintages.';lineChart($('modelChart'),series,all);
};

installModelControlUi();
const loadModelsBeforeControl=loadModels;
loadModels=async function(){await loadModelsBeforeControl();await Promise.all([loadModelControl(),loadModelRanking()])};
$('tabs')?.addEventListener('click',e=>{if(e.target.closest('.tab')?.dataset.view==='models'){loadModelControl();loadModelRanking()}});
</script>
'''


def install_model_control_routes(app: FastAPI, cfg: dict[str, Any]) -> None:
    @app.get("/ui/model-control", include_in_schema=False)
    async def ui_model_control():
        return JSONResponse(control_status(cfg))

    @app.post("/ui/model-control", include_in_schema=False)
    async def ui_set_model_control(request: Request):
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        selection = str((payload or {}).get("selection") or "").strip()
        try:
            set_operator_preference(selection)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(control_status(cfg))

    @app.get("/ui/model-ranking", include_in_schema=False)
    async def ui_model_ranking():
        return JSONResponse(race_ranking(cfg))
