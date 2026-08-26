from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from .dashboard import DASHBOARD_HTML
from .overview_extension import OVERVIEW_EXTENSION
from .ui_v158 import V158_EXTENSION
from .ui_v159 import V159_EXTENSION
from .ui_v160 import V160_EXTENSION
from .ui_v161 import V161_EXTENSION


V161_FIX_EXTENSION = r'''
<script>
// 1.0.61 hotfix: preserve missing values as null instead of Number(null) == 0.
lfNum=function(v){
  if(v===null||v===undefined||v==='')return null;
  const x=Number(v);return Number.isFinite(x)?x:null;
};

// Solinteg meter_active_power sign convention in this installation:
// negative = grid import, positive = grid export.
const renderLiveFlow161=renderLiveFlow;
renderLiveFlow=function(d){
  renderLiveFlow161(d);
  const grid=lfNum(d.grid_power_kw);
  const gridImport=grid!=null&&grid<-.05;
  const gridExport=grid!=null&&grid>.05;
  const sub=$('lfGridSub'),arrow=$('lfGridArrow');
  if(sub)sub.textContent=grid==null?'—':Math.abs(grid)<.05?'balanced':gridImport?'importing':'exporting';
  lfSetPath('lfGridPath',grid,gridExport);
  if(arrow)arrow.textContent=grid==null||Math.abs(grid)<.05?'·':gridImport?'←':'→';

  // Development-only physical balance check. With the configured sign
  // conventions, expected grid power is approximately PV + battery - load.
  const pv=lfNum(d.pv_power_kw),load=lfNum(d.house_load_kw),bat=lfNum(d.battery_power_kw);
  const note=document.querySelector('#liveFlowCard .lf-note');
  if(note&&pv!=null&&load!=null&&bat!=null&&grid!=null){
    const expectedGrid=pv+bat-load;
    const residual=Math.abs(grid-expectedGrid);
    note.textContent=residual>.6
      ?`EV is part of total house load and is not double-counted. Live balance residual ${n(residual,2)} kW — check source/sign conventions.`
      :'EV is shown as a branch of total house load and is not added again to the energy balance.';
  }
};

loadLiveFlow();
</script>
'''


def install_ui_v161_fix(app: FastAPI) -> None:
    @app.middleware("http")
    async def ui_v161_fix(request: Request, call_next):
        if request.url.path == "/ui":
            html = DASHBOARD_HTML.replace(
                "</body>",
                OVERVIEW_EXTENSION + V158_EXTENSION + V159_EXTENSION + V160_EXTENSION + V161_EXTENSION + V161_FIX_EXTENSION + "</body>",
            )
            return HTMLResponse(html)
        return await call_next(request)
