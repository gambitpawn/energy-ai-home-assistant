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
from .ui_v183_fix import V183_FIX_EXTENSION
from .ui_v184 import V184_EXTENSION

V186_EXTENSION = r'''
<script>
// 1.0.86: expected_soc_pct is an interval END state. Plot the interval START
// state at row.start, otherwise the SOC curve is shifted 15 minutes to the left.
function planSocStart(rs,i){
 const r=rs[i]||{};
 if(r.soc_start_pct!=null)return Number(r.soc_start_pct);
 if(i===0){const p=state.plan||{};if(p.initial_soc_pct!=null)return Number(p.initial_soc_pct)}
 const prev=rs[i-1]||{};
 return prev.expected_soc_pct!=null?Number(prev.expected_soc_pct):null;
}
function planEndTime(r){
 if(r.end)return r.end;
 const s=Date.parse(r.start||r.start_utc||'');
 const h=Number(r.duration_hours??.25);
 return Number.isFinite(s)?new Date(s+h*3600000).toISOString():null;
}

planData=function(which){
 const rs=pRows(),ps=pick[which],pts=rs.map((r,i)=>({r,time:r.start||r.start_utc,soc:planSocStart(rs,i)}));
 if(rs.length){const last=rs[rs.length-1],end=planEndTime(last);if(end)pts.push({r:null,time:end,soc:last.expected_soc_pct??last.soc_end_pct})}
 return {times:pts.map(x=>x.time),series:[
  {label:'Load forecast',kind:'plan',axis:'power',color:C.load,values:pts.map(x=>x.r?(x.r.load_kw??x.r.forecast_load_kw):null),on:ps.load,dashed:true},
  {label:'PV forecast',kind:'plan',axis:'power',color:C.pv,values:pts.map(x=>x.r?(x.r.pv_kw??x.r.forecast_pv_kw):null),on:ps.pv,dashed:true},
  {label:'Battery action',kind:'plan',axis:'power',color:C.battery,values:pts.map(x=>x.r?(x.r.battery_action_kw??x.r.action_kw):null),on:ps.battery,dashed:true},
  {label:'Spot price',kind:'forward',axis:'price',color:C.price,values:pts.map(x=>x.r?(x.r.price_ore_kwh??x.r.forecast_price_ore_kwh):null),on:ps.price,dashed:true},
  {label:'SOC',kind:'plan',axis:'soc',color:C.soc,values:pts.map(x=>x.soc),on:ps.soc,dashed:true}
 ]};
};

function drawOverviewV186(){
 const actual=overviewRealized.rows||[],nowMs=timeMs(overviewRealized.now)||Date.now(),base=pRows();
 const planned=base.map((r,i)=>({...r,_soc_start:planSocStart(base,i)})).filter(r=>{const ms=timeMs(r.start||r.start_utc);return ms!=null&&ms>=nowMs-15*60*1000});
 const points=[];
 actual.forEach(r=>points.push({kind:'actual',start:r.start,...r}));
 planned.forEach(r=>points.push({kind:'plan',start:r.start||r.start_utc,...r}));
 if(planned.length){const last=planned[planned.length-1],end=planEndTime(last);if(end)points.push({kind:'plan_terminal',start:end,_soc_start:last.expected_soc_pct??last.soc_end_pct})}
 points.sort((a,b)=>Date.parse(a.start)-Date.parse(b.start)||(a.kind==='actual'?-1:1));
 const times=points.map(r=>r.start),ps=pick.overview,vals=(k,fn)=>points.map(r=>r.kind===k?fn(r):null);
 const planVals=fn=>points.map(r=>r.kind==='plan'?fn(r):null);
 const planSoc=points.map(r=>(r.kind==='plan'||r.kind==='plan_terminal')?r._soc_start:null);
 const series=[
  {label:'Load',kind:'actual',axis:'power',color:C.load,values:vals('actual',r=>r.load_kw),on:ps.load},{label:'Load',kind:'plan',axis:'power',color:C.load,values:planVals(r=>r.load_kw??r.forecast_load_kw),on:ps.load,dashed:true},
  {label:'PV',kind:'actual',axis:'power',color:C.pv,values:vals('actual',r=>r.pv_kw),on:ps.pv},{label:'PV',kind:'plan',axis:'power',color:C.pv,values:planVals(r=>r.pv_kw??r.forecast_pv_kw),on:ps.pv,dashed:true},
  {label:'Battery',kind:'actual',axis:'power',color:C.battery,values:vals('actual',r=>r.battery_kw),on:ps.battery},{label:'Battery',kind:'plan',axis:'power',color:C.battery,values:planVals(r=>r.battery_action_kw??r.action_kw),on:ps.battery,dashed:true},
  {label:'Spot price',kind:'actual',axis:'price',color:C.price,values:vals('actual',r=>r.price_ore_kwh),on:ps.price},{label:'Spot price',kind:'forward',axis:'price',color:C.price,values:planVals(r=>r.price_ore_kwh??r.forecast_price_ore_kwh),on:ps.price,dashed:true},
  {label:'SOC',kind:'actual',axis:'soc',color:C.soc,values:vals('actual',r=>r.soc_pct),on:ps.soc},{label:'SOC',kind:'plan',axis:'soc',color:C.soc,values:planSoc,on:ps.soc,dashed:true}
 ];
 interactiveTimeChart($('overviewPlan'),series,times,{now:overviewRealized.now});
}
drawOverview=drawOverviewV186;

const _renderPlan186=renderPlan;
renderPlan=function(){_renderPlan186();const d=planData('plan');interactiveTimeChart($('planChart'),d.series,d.times);drawOverviewV186()};
</script>
'''


def install_ui_v186(app: FastAPI) -> None:
    @app.middleware("http")
    async def ui_v186(request: Request, call_next):
        if request.url.path == "/ui":
            html = DASHBOARD_HTML.replace(
                "</body>",
                OVERVIEW_EXTENSION
                + V158_EXTENSION
                + V159_EXTENSION
                + V160_EXTENSION
                + V161_EXTENSION
                + V161_FIX_EXTENSION
                + V163_EXTENSION
                + V164_EXTENSION
                + V165_EXTENSION
                + V180_EXTENSION
                + V183_EXTENSION
                + V183_FIX_EXTENSION
                + V184_EXTENSION
                + V186_EXTENSION
                + "</body>",
            )
            return HTMLResponse(html)
        return await call_next(request)
