from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from .dashboard import DASHBOARD_HTML
from .overview_extension import OVERVIEW_EXTENSION
from .ui_v158 import V158_EXTENSION
from .ui_v159 import V159_EXTENSION


PV_GRID_EXTENSION = r'''
<style>
.lf-pvgrid{stroke:#ffbf5a}.lf-pvgrid-label{fill:#91a2b3;font-size:10px;text-anchor:middle}
</style>
<script>
function installPvGridFlow(){
  const svg=document.querySelector('#liveFlowCard .live-flow-svg');
  if(!svg||document.getElementById('lfPvGridPath'))return;
  const ns='http://www.w3.org/2000/svg';
  const firstPath=svg.querySelector('path');
  const base=document.createElementNS(ns,'path');
  base.setAttribute('class','lf-base');
  base.setAttribute('d','M568 42 C680 42 750 55 810 101');
  const active=document.createElementNS(ns,'path');
  active.setAttribute('id','lfPvGridPath');
  active.setAttribute('class','lf-active lf-pvgrid idle');
  active.setAttribute('d','M568 42 C680 42 750 55 810 101');
  svg.insertBefore(base,firstPath);
  svg.insertBefore(active,firstPath);

  const arrow=document.createElementNS(ns,'text');
  arrow.setAttribute('id','lfPvGridArrow');
  arrow.setAttribute('class','lf-arrow');
  arrow.setAttribute('x','702');
  arrow.setAttribute('y','58');
  arrow.textContent='·';
  svg.appendChild(arrow);

  const label=document.createElementNS(ns,'text');
  label.setAttribute('class','lf-pvgrid-label');
  label.setAttribute('x','700');
  label.setAttribute('y','76');
  label.textContent='PV → grid';
  svg.appendChild(label);

  const note=document.querySelector('#liveFlowCard .lf-note');
  if(note)note.textContent='EV is part of total house load and is not double-counted. PV → Grid indicates net export while PV is producing; aggregate measurements cannot always separate PV export from simultaneous battery discharge.';
}

const renderLiveFlowBase=renderLiveFlow;
renderLiveFlow=function(d){
  installPvGridFlow();
  renderLiveFlowBase(d);
  const pv=lfNum(d.pv_power_kw),grid=lfNum(d.grid_power_kw);
  const exportKw=grid!=null?Math.max(0,-grid):0;
  const active=pv!=null&&pv>.05&&exportKw>.05;
  lfSetPath('lfPvGridPath',active?Math.min(pv,exportKw):0,true);
  const arrow=$('lfPvGridArrow');if(arrow)arrow.textContent=active?'↗':'·';
};

installPvGridFlow();
loadLiveFlow();
</script>
'''


def install_ui_v159_pvgrid(app: FastAPI) -> None:
    @app.middleware("http")
    async def ui_v159_pvgrid(request: Request, call_next):
        if request.url.path == "/ui":
            html = DASHBOARD_HTML.replace(
                "</body>",
                OVERVIEW_EXTENSION + V158_EXTENSION + V159_EXTENSION + PV_GRID_EXTENSION + "</body>",
            )
            return HTMLResponse(html)
        return await call_next(request)
