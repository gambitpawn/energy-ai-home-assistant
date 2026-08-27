from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from .dashboard import DASHBOARD_HTML
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
from .ui_v183 import V183_EXTENSION

V183_FIX_EXTENSION = r'''
<style>
.model-metric{display:none;border:1px solid var(--line);background:var(--panel2);color:var(--text);border-radius:9px;padding:7px 9px}
.model-metric.show{display:inline-block}
</style>
<script>
let modelBehaviourMetric='battery';
function installModelMetric(){
  const toolbar=document.querySelector('#models .models-toolbar');
  if(!toolbar||$('modelBehaviourMetric'))return;
  const sel=document.createElement('select');
  sel.id='modelBehaviourMetric';sel.className='model-metric';
  sel.innerHTML='<option value="battery">Battery action</option><option value="soc">Expected SOC</option>';
  sel.onchange=()=>{modelBehaviourMetric=sel.value;renderModels()};
  const picker=$('modelPicker');toolbar.insertBefore(sel,picker);
}
function modelMetricVisibility(){const s=$('modelBehaviourMetric');if(s)s.classList.toggle('show',modelMode==='behaviour')}

renderModels=function(){
  if(!modelData)return;installModelMetric();modelMetricVisibility();
  if(modelMode==='economic'){
    const ids=Object.keys(modelData.economics||{}).filter(id=>modelEnabled[id]);
    const allDates=[...new Set(ids.flatMap(id=>(modelData.economics[id]||[]).map(r=>r.date)))].sort();
    const palette=[C.load,C.pv,C.battery,C.price,C.gridImport];
    const series=ids.map((id,ix)=>{
      const map=Object.fromEntries((modelData.economics[id]||[]).map(r=>[r.date,r.cumulative_oracle_regret_sek]));
      let last=0;
      const values=allDates.map(d=>{if(map[d]!=null)last=Number(map[d]);return last});
      return {name:id,axis:'power',color:palette[ix%palette.length],on:true,values,width:2.4};
    });
    $('modelChartTitle').textContent='Cumulative realized oracle regret';
    $('modelNote').textContent='Values are SEK of realized regret relative to the perfect-information oracle on comparable days. Lower is better. The chart axis uses the generic numeric renderer; units here are SEK, not kW.';
    lineChart($('modelChart'),series,allDates.map(d=>`${d}T12:00:00Z`));
    return;
  }

  const ids=Object.keys(modelData.behaviour||{}).filter(id=>modelEnabled[id]);
  const all=[...new Set(ids.flatMap(id=>(modelData.behaviour[id]||[]).map(r=>r.start)))].sort();
  const palette=[C.load,C.pv,C.battery,C.price,C.gridImport];
  const isSoc=modelBehaviourMetric==='soc';
  const field=isSoc?'expected_soc_pct':'requested_action_kw';
  const series=ids.map((id,ix)=>{
    const map=Object.fromEntries((modelData.behaviour[id]||[]).map(r=>[r.start,r[field]]));
    return {name:id,axis:isSoc?'soc':'power',color:palette[ix%palette.length],on:true,values:all.map(t=>map[t]??null),width:2.1};
  });
  $('modelChartTitle').textContent=isSoc?'Expected SOC by model':'Battery action by model';
  $('modelNote').textContent=isSoc?'Expected battery SOC after each model decision on the same stored information vintages.':'Positive = discharge, negative = charge. Decisions share the same stored information vintages.';
  lineChart($('modelChart'),series,all);
};

setTimeout(()=>{installModelMetric();const mm=$('modelMode');if(mm)mm.addEventListener('click',()=>setTimeout(modelMetricVisibility,0))},0);
</script>
'''


def install_ui_v183_fix(app: FastAPI) -> None:
    @app.middleware("http")
    async def ui_v183_fix(request: Request, call_next):
        if request.url.path == "/ui":
            html = DASHBOARD_HTML.replace(
                "</body>",
                OVERVIEW_EXTENSION + V158_EXTENSION + V159_EXTENSION + V160_EXTENSION + V161_EXTENSION + V161_FIX_EXTENSION + V163_EXTENSION + V164_EXTENSION + V165_EXTENSION + V180_EXTENSION + V183_EXTENSION + V183_FIX_EXTENSION + "</body>",
            )
            return HTMLResponse(html)
        return await call_next(request)
