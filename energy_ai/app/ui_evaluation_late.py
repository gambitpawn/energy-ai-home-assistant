from __future__ import annotations


EVALUATION_LATE_EXTENSION = r'''
<style>
.eval-breakdown-late{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:10px}
.eval-breakdown-late .piece{background:#0f1720;border:1px solid var(--line);border-radius:10px;padding:11px}
.eval-breakdown-late .name{color:var(--muted);font-size:11px}.eval-breakdown-late .amount{font-size:20px;font-weight:750;margin-top:3px}.eval-breakdown-late .share{font-size:11px;color:var(--muted);margin-top:2px}
.opportunity-legend-late{display:flex;gap:18px;flex-wrap:wrap;color:var(--muted);font-size:12px;margin:4px 0 8px}.opportunity-legend-late span{display:flex;align-items:center;gap:6px}.opportunity-legend-late i{display:inline-block;width:14px;height:8px;border-radius:2px}
#opportunityChartLate{height:280px}
@media(max-width:720px){.eval-breakdown-late{grid-template-columns:1fr}}
</style>
<script>
(()=>{
  try{
    const byId=id=>document.getElementById(id);
    const fmtN=(v,d=2)=>v==null||Number.isNaN(+v)?'—':Number(v).toLocaleString('sv-SE',{maximumFractionDigits:d});
    const fmtSek=v=>v==null?'—':`${fmtN(v,2)} SEK`;
    const fmtPct=v=>v==null?'—':`${fmtN(100*Number(v),1)}%`;
    const capText=v=>v==null?'—':`${fmtN(100*Number(v),1)}%`;
    const kpi=(label,value,hint='',cls='')=>`<div class="card kpi"><div class="label">${label}</div><div class="value ${cls}">${value}</div><div class="hint">${hint}</div></div>`;

    const hist=document.querySelector('#history .grid.two');
    if(hist){
      hist.className='';
      hist.innerHTML='<div class="card" style="margin-top:12px"><h2>Daily opportunity captured</h2><div class="opportunity-legend-late"><span><i style="background:var(--good)"></i>Captured saving</span><span><i style="background:var(--warn)"></i>Remaining gap</span><span><i style="background:var(--bad)"></i>Negative saving</span></div><div id="opportunityChartLate"></div><div class="quality-note">Only complete days are included. Total bar height is saving + gap to hindsight.</div></div>';
    }

    const evalGrid=document.querySelector('#evaluation .grid.two');
    if(evalGrid&&!byId('evalRegretCardLate')){
      evalGrid.insertAdjacentHTML('beforebegin','<div class="card" id="evalRegretCardLate" style="margin-top:12px"><h2>Where did the remaining opportunity go?</h2><div id="evalRegretBreakdownLate" class="notice">Decomposition is calculated for complete, mature days.</div></div>');
    }

    function opportunityBars(days){
      const el=byId('opportunityChartLate');if(!el)return;
      const data=(days||[]).filter(x=>x.status==='ok'&&x.opportunity_sek!=null);
      if(!data.length){el.innerHTML='<div class="empty">No complete evaluated days.</div>';return}
      const W=1000,H=260,p={l:52,r:18,t:18,b:52};
      const max=Math.max(1,...data.map(x=>Math.max(0,Number(x.opportunity_sek||0))),...data.map(x=>Math.max(0,-Number(x.saving_sek||0))));
      const base=H-p.b,usable=base-p.t,slot=(W-p.l-p.r)/data.length,bw=Math.max(5,Math.min(42,slot*.58));
      let svg=`<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">`;
      for(let j=0;j<5;j++){const v=max*(4-j)/4,y=p.t+usable*j/4;svg+=`<line x1="${p.l}" y1="${y}" x2="${W-p.r}" y2="${y}" stroke="#263647"/><text x="4" y="${y+4}" fill="#91a2b3" font-size="10">${fmtN(v,1)}</text>`}
      data.forEach((x,i)=>{
        const cx=p.l+slot*(i+.5),saving=Number(x.saving_sek||0),opp=Math.max(0,Number(x.opportunity_sek||0)),cap=Math.max(0,Math.min(opp,saving));
        const hCap=usable*cap/max,hGap=usable*Math.max(0,opp-cap)/max;
        const title=`${x.local_date}: opportunity ${fmtN(x.opportunity_sek)} SEK · saving ${fmtN(x.saving_sek)} SEK · captured ${capText(x.capture_fraction)} · remaining ${fmtN(x.remaining_gap_sek)} SEK`;
        if(hGap>0)svg+=`<rect x="${cx-bw/2}" y="${base-hCap-hGap}" width="${bw}" height="${hGap}" fill="#ffbf5a"><title>${title}</title></rect>`;
        if(hCap>0)svg+=`<rect x="${cx-bw/2}" y="${base-hCap}" width="${bw}" height="${hCap}" fill="#51d88a"><title>${title}</title></rect>`;
        if(saving<0){const hn=Math.min(usable*.3,usable*(-saving)/max);svg+=`<rect x="${cx-bw/2}" y="${base}" width="${bw}" height="${hn}" fill="#ff6b6b"><title>${title}</title></rect>`}
        svg+=`<text x="${cx}" y="${H-20}" fill="#91a2b3" font-size="9" text-anchor="middle">${String(x.local_date).slice(5)}</text>`;
      });
      svg+=`<line x1="${p.l}" y1="${base}" x2="${W-p.r}" y2="${base}" stroke="#52687c"/></svg>`;el.innerHTML=svg;
    }

    window.renderHistory=function(){
      const h=(window.state||state).history||{},days=h.days||[],s=h.evaluation_summary||{};
      byId('historyKpis').innerHTML=kpi('Complete days',fmtN(h.complete_days,0),`${fmtN(h.partial_days,0)} partial`)+kpi('Total saving',fmtSek(s.total_saving_sek),'Complete days only',s.total_saving_sek>0?'good':'')+kpi('Available opportunity',fmtSek(s.total_opportunity_sek),'Saving + remaining gap')+kpi('Opportunity captured',capText(s.capture_fraction),'Aggregate complete days',s.capture_fraction>=.7?'good':s.capture_fraction!=null&&s.capture_fraction<.4?'warn':'')+kpi('Remaining gap',fmtSek(s.total_remaining_gap_sek),'Gap to hindsight')+kpi('Data quality',`${fmtN(h.complete_days,0)}/${fmtN(h.stored_days,0)}`,'Complete / stored days');
      opportunityBars(days);
      byId('historyTable').innerHTML=days.length?`<table class="tbl"><thead><tr><th>Date</th><th>Quality</th><th>Saving SEK</th><th>Opportunity SEK</th><th>Captured</th><th>Forecast gap</th><th>Unpublished price</th><th>Planner / policy</th><th>Load MAE kW</th><th>PV MAE kW</th></tr></thead><tbody>${days.map(x=>`<tr><td>${x.local_date}</td><td><span class="pill ${x.status==='ok'?'ok':'partial'}">${x.status==='ok'?'complete':`${x.status} · ${fmtPct(x.coverage)}`}</span></td><td>${fmtN(x.saving_sek)}</td><td>${fmtN(x.opportunity_sek)}</td><td>${capText(x.capture_fraction)}</td><td>${fmtN(x.forecast_gap_sek)}</td><td title="Prices not yet published at decision time; published prices themselves do not change.">${fmtN(x.unpublished_price_horizon_sek)}</td><td>${fmtN(x.planner_policy_gap_sek)}</td><td>${fmtN(x.load_mae_kw)}</td><td>${fmtN(x.pv_mae_kw)}</td></tr>`).join('')}</tbody></table>`:'<div class="empty">No evaluated days stored yet.</div>';
    };

    window.loadHistory=async function(){
      try{(window.state||state).history=await api(`ui/evaluation-history?days=${byId('historyDays').value}`);window.renderHistory()}
      catch(e){byId('historyTable').innerHTML=`<div class="empty">${e.message}</div>`}
    };
    if(byId('reloadHistory'))byId('reloadHistory').onclick=window.loadHistory;
    if(byId('historyDays'))byId('historyDays').onchange=window.loadHistory;

    function piece(name,value,total,title){const share=value!=null&&total>0?value/total:null;return `<div class="piece" title="${title}"><div class="name">${name}</div><div class="amount">${fmtSek(value)}</div><div class="share">${share==null?'—':`${fmtN(100*share,1)}% of remaining gap`}</div></div>`}
    function renderEvalLate(){
      const e=(window.state||state).eval||{},m=e.evaluation_summary||{},d=e.data||{},rt=e.realtime_counterfactual||{},fe=e.forecast_error_on_executed_intervals||{},r=e.regret_decomposition_ui||{};
      if(e.local_date&&byId('evalKpis'))byId('evalKpis').innerHTML=kpi('Saving',fmtSek(m.saving_sek),'vs zero-battery baseline',m.saving_sek>0?'good':m.saving_sek<0?'bad':'')+kpi('Available opportunity',fmtSek(m.opportunity_sek),'Saving + gap to hindsight')+kpi('Opportunity captured',capText(m.capture_fraction),'Share of available opportunity',m.capture_fraction>=.7?'good':m.capture_fraction!=null&&m.capture_fraction<.4?'warn':'')+kpi('Remaining gap',fmtSek(m.remaining_gap_sek),'Gap to hindsight',m.remaining_gap_sek>0?'warn':'')+kpi('Plan coverage',fmtPct(d.plan_action_coverage_fraction),'≥90% for complete day',d.plan_action_coverage_fraction>=.9?'good':'warn')+kpi('Data quality',m.comparable?'Complete':'Partial',m.comparable?'Included in period KPIs':'Excluded from period KPIs',m.comparable?'good':'warn');
      const diag=byId('diagnostics');if(diag&&e.local_date)diag.innerHTML=rows({'Status':`<span class="pill ${e.status==='ok'?'ok':'partial'}">${e.status}</span>`,'Load MAE':fe.load?.mae_kw!=null?`${fmtN(fe.load.mae_kw)} kW`:'—','PV MAE':fe.pv?.mae_kw!=null?`${fmtN(fe.pv.mae_kw)} kW`:'—','Net-load MAE':fe.net_load?.mae_kw!=null?`${fmtN(fe.net_load.mae_kw)} kW`:'—','Forecast economic gap':r.valid?fmtSek(r.forecast_gap_sek):'—','Battery throughput':rt.battery_throughput_kwh!=null?`${fmtN(rt.battery_throughput_kwh)} kWh`:'—','Clamped intervals':fmtN(rt.clamped_action_intervals,0),'Terminal SOC':rt.terminal_soc_pct!=null?`${fmtN(rt.terminal_soc_pct,1)}%`:'—'});
      const box=byId('evalRegretBreakdownLate');if(box){if(r.valid){const total=Number(r.total_gap_sek||0);box.className='eval-breakdown-late';box.innerHTML=piece('PV/load forecast gap',r.forecast_gap_sek,total,'Cost of using forecast rather than realized PV and house load, with historical price information unchanged.')+piece('Unpublished price horizon',r.unpublished_price_horizon_sek,total,'Value of knowing prices that had not yet been published at decision time. Already published prices do not change.')+piece('Planner / policy gap',r.planner_policy_gap_sek,total,'Residual from rolling horizon, terminal value, reserve and policy choices after perfect load, PV and price information.')}else{box.className='notice';box.textContent='Decomposition unavailable for this day.'}}
    }

    const baseRenderEval=window.renderEval;
    window.renderEval=function(){if(typeof baseRenderEval==='function')baseRenderEval();renderEvalLate()};
    window.loadEval=async function(localDate){try{(window.state||state).eval=await api(`ui/evaluation-day?local_date=${localDate}`);window.renderEval()}catch(e){if(byId('evalMeta'))byId('evalMeta').textContent=e.message}};

    document.addEventListener('click',e=>{const b=e.target.closest?.('.tab');if(!b)return;if(b.dataset.view==='history')window.loadHistory();if(b.dataset.view==='evaluation'&&byId('evalDate')?.value)window.loadEval(byId('evalDate').value)});
    window.__evaluation_iteration1_late_loaded=true;
  }catch(err){console.error('Evaluation iteration 1 late override failed',err);window.__evaluation_iteration1_late_error=String(err&&err.stack||err)}
})();
</script>
'''
