from __future__ import annotations

from typing import Any

from fastapi import FastAPI


EVALUATION_EXTENSION = r'''
<style>
#overviewPlan{border-radius:10px;background-repeat:no-repeat}
.eval-period-header{display:flex;justify-content:space-between;align-items:end;gap:12px;flex-wrap:wrap;margin-bottom:12px}
.eval-period-header h2{margin:0;font-size:18px}.eval-period-header .muted{font-size:12px}
.eval-opportunity-legend{display:flex;gap:18px;flex-wrap:wrap;color:var(--muted);font-size:12px;margin:4px 0 8px}.eval-opportunity-legend span{display:flex;align-items:center;gap:6px}.eval-opportunity-legend i{display:inline-block;width:14px;height:8px;border-radius:2px}
#evalOpportunityChart{height:280px}.eval-quality-note{font-size:11px;color:var(--muted);margin-top:8px}
.eval-day-link{cursor:pointer;color:var(--blue);font-weight:650}.eval-day-link:hover{text-decoration:underline}
.eval-day-heading{font-size:18px;font-weight:750;margin:22px 0 10px}.eval-section-gap{margin-top:12px}
</style>
<script>
// Preserve the existing Overview "now" shading. It is independent of evaluation
// data and was already part of the current UI before this consolidation.
const drawOverviewBeforeUnifiedEvaluation=drawOverview;
drawOverview=function(){
  drawOverviewBeforeUnifiedEvaluation();
  const el=$('overviewPlan'),actual=overviewRealized.rows||[],planned=pRows();
  const stamps=[...actual.map(r=>Date.parse(r.start)),...planned.map(r=>Date.parse(r.start||r.start_utc))].filter(Number.isFinite);
  const now=Date.parse(overviewRealized.now||new Date().toISOString());
  if(stamps.length&&Number.isFinite(now)){
    const lo=Math.min(...stamps),hi=Math.max(...stamps),pctNow=Math.max(0,Math.min(100,100*(now-lo)/Math.max(1,hi-lo)));
    el.style.background=`linear-gradient(to right, transparent 0%, transparent ${pctNow}%, rgba(79,179,255,.045) ${pctNow}%, rgba(79,179,255,.045) 100%)`;
  }
};

(()=>{
  const OPPORTUNITY_EPS_SEK=0.05;

  // History is now part of Evaluation. Keep the old DOM in place so the base
  // dashboard's already-bound handlers cannot fail, but remove it from navigation.
  const historyTab=document.querySelector('.tab[data-view="history"]');
  if(historyTab)historyTab.style.display='none';
  const historyView=$('history');
  if(historyView)historyView.style.display='none';

  const evalView=$('evaluation');
  const dayToolbar=evalView?.querySelector('.toolbar');
  if(evalView&&dayToolbar&&!$('evalPeriodSummary')){
    dayToolbar.insertAdjacentHTML('beforebegin',`
      <div id="evalPeriodSummary">
        <div class="eval-period-header">
          <div><h2>Actual control performance</h2><div class="muted">Stored evaluations only. No counterfactual or decomposition jobs are run when this page loads.</div></div>
          <div class="toolbar" style="margin:0"><label>Period <select id="evalPeriod"><option value="7" selected>7 days</option><option value="30">30 days</option><option value="90">90 days</option></select></label><button class="btn" id="reloadEvaluationPeriod">Reload</button><span class="muted" id="evalPeriodMeta"></span></div>
        </div>
        <div class="grid kpis" id="evalPeriodKpis"></div>
        <div class="card eval-section-gap"><h2>Daily opportunity captured</h2><div class="eval-opportunity-legend"><span><i style="background:var(--good)"></i>Captured saving</span><span><i style="background:var(--warn)"></i>Remaining gap</span><span><i style="background:var(--bad)"></i>Negative saving</span></div><div id="evalOpportunityChart"></div><div class="eval-quality-note">Complete days only. Opportunity = saving versus zero-battery baseline + gap to perfect hindsight.</div></div>
        <div class="card eval-section-gap"><h2>Evaluated days</h2><div id="evalPeriodTable" class="table-wrap"></div></div>
        <div class="eval-day-heading">Day detail</div>
      </div>`);
  }

  function captureText(v){return v==null?'—':`${n(100*Number(v),1)}%`}
  function dayEconomics(x){
    const saving=x?.saving_sek;
    const gap=x?.perfect_information_gap_sek;
    const opportunity=saving!=null&&gap!=null?Number(saving)+Number(gap):null;
    const capture=x?.status==='ok'&&opportunity!=null&&opportunity>OPPORTUNITY_EPS_SEK?Number(saving)/opportunity:null;
    return {saving:saving==null?null:Number(saving),gap:gap==null?null:Number(gap),opportunity,capture};
  }
  function periodEconomics(days){
    const complete=(days||[]).filter(x=>x.status==='ok');
    const comparable=complete.map(x=>({row:x,e:dayEconomics(x)})).filter(x=>x.e.saving!=null&&x.e.gap!=null&&x.e.opportunity!=null);
    const saving=comparable.reduce((a,x)=>a+x.e.saving,0);
    const gap=comparable.reduce((a,x)=>a+x.e.gap,0);
    const opportunity=comparable.reduce((a,x)=>a+x.e.opportunity,0);
    return {
      complete:complete.length,
      partial:(days||[]).filter(x=>x.status!=='ok').length,
      comparable:comparable.length,
      saving:comparable.length?saving:null,
      gap:comparable.length?gap:null,
      opportunity:comparable.length?opportunity:null,
      capture:comparable.length&&opportunity>OPPORTUNITY_EPS_SEK?saving/opportunity:null,
    };
  }

  function opportunityBars(days){
    const el=$('evalOpportunityChart');if(!el)return;
    const data=(days||[]).filter(x=>x.status==='ok').map(x=>({row:x,e:dayEconomics(x)})).filter(x=>x.e.opportunity!=null);
    if(!data.length){el.innerHTML='<div class="empty">No complete evaluated days with economic data.</div>';return}
    const W=1000,H=260,p={l:52,r:18,t:18,b:52},base=H-p.b,usable=base-p.t;
    const max=Math.max(1,...data.map(x=>Math.max(0,x.e.opportunity||0)),...data.map(x=>Math.max(0,-(x.e.saving||0))));
    const slot=(W-p.l-p.r)/data.length,bw=Math.max(5,Math.min(42,slot*.58));
    let svg=`<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">`;
    for(let j=0;j<5;j++){const v=max*(4-j)/4,y=p.t+usable*j/4;svg+=`<line x1="${p.l}" y1="${y}" x2="${W-p.r}" y2="${y}" stroke="#263647"/><text x="4" y="${y+4}" fill="#91a2b3" font-size="10">${n(v,1)}</text>`}
    data.forEach((x,i)=>{
      const cx=p.l+slot*(i+.5),saving=x.e.saving||0,opp=Math.max(0,x.e.opportunity||0),captured=Math.max(0,Math.min(opp,saving)),remaining=Math.max(0,opp-captured),hCaptured=usable*captured/max,hRemaining=usable*remaining/max;
      const title=`${x.row.local_date}: opportunity ${n(x.e.opportunity)} SEK · saving ${n(x.e.saving)} SEK · captured ${captureText(x.e.capture)} · remaining ${n(x.e.gap)} SEK`;
      if(hRemaining>0)svg+=`<rect x="${cx-bw/2}" y="${base-hCaptured-hRemaining}" width="${bw}" height="${hRemaining}" fill="#ffbf5a"><title>${title}</title></rect>`;
      if(hCaptured>0)svg+=`<rect x="${cx-bw/2}" y="${base-hCaptured}" width="${bw}" height="${hCaptured}" fill="#51d88a"><title>${title}</title></rect>`;
      if(saving<0){const hn=Math.min(usable*.3,usable*(-saving)/max);svg+=`<rect x="${cx-bw/2}" y="${base}" width="${bw}" height="${hn}" fill="#ff6b6b"><title>${title}</title></rect>`}
      svg+=`<text x="${cx}" y="${H-20}" fill="#91a2b3" font-size="9" text-anchor="middle">${String(x.row.local_date).slice(5)}</text>`;
    });
    svg+=`<line x1="${p.l}" y1="${base}" x2="${W-p.r}" y2="${base}" stroke="#52687c"/></svg>`;
    el.innerHTML=svg;
  }

  function renderEvaluationPeriod(data){
    const h=data||{},days=h.days||[],m=periodEconomics(days),k=$('evalPeriodKpis'),table=$('evalPeriodTable');
    if(k)k.innerHTML=card('Saving',sek(m.saving),'Comparable complete days only',m.saving>0?'good':m.saving<0?'bad':'')+card('Available opportunity',sek(m.opportunity),'Saving + remaining gap')+card('Opportunity captured',captureText(m.capture),'Share of available opportunity',m.capture>=.7?'good':m.capture!=null&&m.capture<.4?'warn':'')+card('Remaining gap',sek(m.gap),'Gap to perfect hindsight',m.gap>0?'warn':'')+card('Complete days',n(m.complete,0),`${n(m.partial,0)} partial / other`)+card('Data quality',`${n(m.comparable,0)}/${n(m.complete,0)}`,'Complete days with full economics');
    opportunityBars(days);
    if(table)table.innerHTML=days.length?`<table class="tbl"><thead><tr><th>Date</th><th>Status</th><th>Coverage</th><th>Saving SEK</th><th>Opportunity SEK</th><th>Captured</th><th>Load MAE kW</th><th>PV MAE kW</th></tr></thead><tbody>${days.map(x=>{const e=dayEconomics(x),complete=x.status==='ok';return `<tr><td><span class="eval-day-link" data-eval-date="${x.local_date}">${x.local_date}</span></td><td><span class="pill ${complete?'ok':'partial'}">${complete?'complete':x.status}</span></td><td>${pct(x.coverage)}</td><td>${n(e.saving)}</td><td>${n(e.opportunity)}</td><td>${complete?captureText(e.capture):'—'}</td><td>${n(x.load_mae_kw)}</td><td>${n(x.pv_mae_kw)}</td></tr>`}).join('')}</tbody></table>`:'<div class="empty">No evaluated days stored yet.</div>';
    const meta=$('evalPeriodMeta');if(meta)meta.textContent=`${n(days.length,0)} stored · ${n(m.complete,0)} complete`;
  }

  let periodRequestId=0;
  async function loadEvaluationPeriod(){
    const requestId=++periodRequestId,days=$('evalPeriod')?.value||'7',meta=$('evalPeriodMeta');
    try{
      if(meta)meta.textContent='Loading…';
      const data=await api(`ui/history?days=${days}`);
      if(requestId!==periodRequestId)return;
      renderEvaluationPeriod(data);
    }catch(e){
      if(requestId!==periodRequestId)return;
      if(meta)meta.textContent=e.message;
      const table=$('evalPeriodTable');if(table)table.innerHTML=`<div class="empty">${e.message}</div>`;
    }
  }

  // Keep the base dashboard's history loader and renderer untouched. It can finish
  // its startup request against the hidden History DOM without racing this view.
  const baseRenderEval=renderEval;
  renderEval=function(){
    baseRenderEval();
    const e=state.eval||{};if(!e.local_date)return;
    const c=e.comparison||{},d=e.data||{},rt=e.realtime_counterfactual||{},saving=c.realtime_economic_saving_vs_zero_battery_sek,gap=c.perfect_information_gap_sek,opp=saving!=null&&gap!=null?Number(saving)+Number(gap):null,capture=e.status==='ok'&&opp!=null&&opp>OPPORTUNITY_EPS_SEK?Number(saving)/opp:null;
    $('evalKpis').innerHTML=card('Saving',sek(saving),'vs zero-battery baseline',saving>0?'good':saving<0?'bad':'')+card('Available opportunity',sek(opp),'Saving + gap to hindsight')+card('Opportunity captured',captureText(capture),'Complete days only',capture>=.7?'good':capture!=null&&capture<.4?'warn':'')+card('Remaining gap',sek(gap),'Gap to perfect hindsight',gap>0?'warn':'')+card('Plan coverage',pct(d.plan_action_coverage_fraction),'≥90% for complete day',d.plan_action_coverage_fraction>=.9?'good':'warn')+card('Battery throughput',rt.battery_throughput_kwh!=null?`${n(rt.battery_throughput_kwh)} kWh`:'—','Realized replay');
  };

  $('reloadEvaluationPeriod').onclick=loadEvaluationPeriod;
  $('evalPeriod').onchange=loadEvaluationPeriod;
  $('evalPeriodTable').addEventListener('click',e=>{const link=e.target.closest?.('[data-eval-date]');if(!link)return;const localDate=link.dataset.evalDate;$('evalDate').value=localDate;loadEval(localDate);dayToolbar.scrollIntoView({behavior:'smooth',block:'start'})});
  $('tabs').addEventListener('click',e=>{const b=e.target.closest('.tab');if(b?.dataset?.view==='evaluation')loadEvaluationPeriod()});
})();
</script>
'''


def install_evaluation_routes(app: FastAPI, cfg: dict[str, Any]) -> None:
    """Iteration 1 is display-only and intentionally registers no evaluation routes.

    The consolidated Evaluation tab reads the existing persisted /ui/history and
    /optimizer/evaluation/day endpoints. Heavy decomposition remains outside the
    page-load path and is deferred to a later iteration.
    """
    return None
