from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .dashboard import DASHBOARD_HTML, _history
from .engine_registry import registry_status
from .neural_engine import neural_runtime_status
from .overview_extension import OVERVIEW_EXTENSION
from .ui_v158 import V158_EXTENSION
from .ui_v159 import V159_EXTENSION
from .ui_v160 import V160_EXTENSION
from .ui_v161 import V161_EXTENSION
from .ui_v161_fix import V161_FIX_EXTENSION
from .ui_v163 import V163_EXTENSION
from .ui_v164 import V164_EXTENSION
from .ui_v165 import V165_EXTENSION
from .ui_v180 import V180_EXTENSION
from .ui_v183 import V183_EXTENSION, _model_comparison
from .ui_v183_fix import V183_FIX_EXTENSION

V184_EXTENSION = r'''
<style>
.history-note{font-size:11px;color:var(--muted);margin:7px 0 0}.model-state{font-size:10px;color:var(--muted);margin-left:3px}.model-state.learning{color:var(--warn)}
</style>
<script>
// 1.0.84: the editable parameter editor is authoritative. The base dashboard's
// async init used to race it and sometimes replace it with the reduced read-only
// runtime-config renderer.
renderParameters=function(){ if(typeof loadParameterEditor==='function') loadParameterEditor(); };
const _tabs184=$('tabs');
if(_tabs184)_tabs184.addEventListener('click',e=>{
  if(e.target.closest('.tab')?.dataset.view==='parameters'&&typeof loadParameterEditor==='function') loadParameterEditor();
});
setTimeout(()=>{if(typeof loadParameterEditor==='function')loadParameterEditor()},0);

renderHistory=function(){
 const h=state.history||{},days=h.days||[];
 $('historyKpis').innerHTML=card('Fully evaluated',n(h.complete_days,0),`${n(h.partial_days,0)} partial`)+card('Stored mature days',n(h.stored_days,0),'Automatic backfill')+card('Total saving',sek(h.total_realtime_economic_saving_vs_zero_battery_sek),'Fully evaluated days',h.total_realtime_economic_saving_vs_zero_battery_sek>0?'good':'')+card('Mean daily saving',sek(h.mean_daily_realtime_economic_saving_sek),'Fully evaluated days')+card('Perfect-info gap',sek(h.total_perfect_information_gap_sek),'Fully evaluated days')+card('Mean coverage',pct(h.mean_plan_action_coverage_fraction),'Stored evaluated days');
 const withSaving=days.filter(x=>x.saving_sek!=null);
 const withRegret=days.filter(x=>x.perfect_information_gap_sek!=null);
 bars($('savingChart'),withSaving.map(x=>({label:`${x.local_date}${x.status==='ok'?'':' · partial'}`,value:x.saving_sek})));
 bars($('regretChart'),withRegret.map(x=>({label:`${x.local_date}${x.status==='ok'?'':' · partial'}`,value:x.perfect_information_gap_sek})),'#ffbf5a','#51d88a');
 if(!$('historyPartialNote')){$('historyTable').parentElement.insertAdjacentHTML('beforebegin','<div id="historyPartialNote" class="history-note">Partial days are shown when they contain usable metrics; they are excluded from aggregate saving/regret KPIs until plan coverage reaches the evaluation threshold.</div>')}
 $('historyTable').innerHTML=days.length?`<table class="tbl"><thead><tr><th>Date</th><th>Status</th><th>Coverage</th><th>Saving SEK</th><th>Perfect-info gap SEK</th><th>Load MAE kW</th><th>PV MAE kW</th><th>Throughput kWh</th><th>Clamps</th></tr></thead><tbody>${days.map(x=>`<tr><td>${x.local_date}</td><td><span class="pill ${x.status==='ok'?'ok':'partial'}">${x.status}</span></td><td>${pct(x.coverage)}</td><td>${n(x.saving_sek)}</td><td>${n(x.perfect_information_gap_sek)}</td><td>${n(x.load_mae_kw)}</td><td>${n(x.pv_mae_kw)}</td><td>${n(x.throughput_kwh)}</td><td>${n(x.clamped_intervals,0)}</td></tr>`).join('')}</tbody></table>`:'<div class="empty">No evaluated mature days stored yet.</div>';
};

loadModels=async function(){
 try{
  modelData=await api(`ui/models-comparison-v184?window=${modelWindow}`);
  const models=modelData.models||[];
  for(const m of models)if(modelEnabled[m.engine_id]===undefined)modelEnabled[m.engine_id]=true;
  $('modelPicker').innerHTML=models.map(m=>`<label title="${esc(m.note||'')}"><input type="checkbox" data-engine="${esc(m.engine_id)}" ${modelEnabled[m.engine_id]?'checked':''}> ${esc(m.engine_id)} <span class="model-state ${m.state==='learning'?'learning':''}">${esc(m.state||'')}</span></label>`).join('');
  $('modelPicker').onchange=e=>{if(e.target.dataset.engine){modelEnabled[e.target.dataset.engine]=e.target.checked;renderModels()}};
  renderModels();
 }catch(e){$('modelChart').innerHTML=`<div class="empty">Could not load model comparison: ${e.message}</div>`}
};
</script>
'''


def install_ui_v184(app: FastAPI) -> None:
    @app.middleware("http")
    async def ui_v184(request: Request, call_next):
        if request.url.path == "/ui":
            html = DASHBOARD_HTML.replace(
                "</body>",
                OVERVIEW_EXTENSION + V158_EXTENSION + V159_EXTENSION + V160_EXTENSION + V161_EXTENSION + V161_FIX_EXTENSION + V163_EXTENSION + V164_EXTENSION + V165_EXTENSION + V180_EXTENSION + V183_EXTENSION + V183_FIX_EXTENSION + V184_EXTENSION + "</body>",
            )
            return HTMLResponse(html)
        return await call_next(request)

    @app.get("/ui/models-comparison-v184", include_in_schema=False)
    async def models_comparison_v184(window: str = "7d"):
        data = _model_comparison(window)
        registry = registry_status()
        neural = neural_runtime_status()
        models = []
        seen = set()
        for item in registry.get("engines") or []:
            engine_id = str(item.get("engine_id") or "")
            if not engine_id:
                continue
            seen.add(engine_id)
            state = "available" if item.get("available") else "unavailable"
            note = ""
            if engine_id == "neural_v1":
                if neural.get("shadow_ready"):
                    state = "shadow ready"
                    note = "Neural model is trained and produces comparable shadow decisions."
                elif neural.get("model_exists"):
                    state = "learning"
                    note = f"Neural model is still learning; {neural.get('samples') or 0} samples available. No control promotion until shadow-ready."
                else:
                    state = "learning"
                    note = f"Neural training data is accumulating; {neural.get('samples') or 0} samples available."
            models.append({"engine_id": engine_id, "state": state, "note": note})
        # Keep engines already present in stored comparison data even if the
        # registry changes later.
        for engine_id in sorted(set(data.get("economics", {})) | set(data.get("behaviour", {}))):
            if engine_id not in seen:
                models.append({"engine_id": engine_id, "state": "historical", "note": "Stored historical model data."})
        data["models"] = models
        data["neural_runtime"] = neural
        return JSONResponse(data)
