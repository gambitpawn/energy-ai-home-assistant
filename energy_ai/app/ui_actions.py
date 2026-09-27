from __future__ import annotations

ACTIONS_EXTENSION = r'''
<style>
.actions-grid{display:grid;grid-template-columns:minmax(320px,.8fr) minmax(420px,1.2fr);gap:12px}.action-form{display:grid;gap:12px}.action-form label{display:grid;gap:5px;color:var(--muted);font-size:12px}.action-form input[type=datetime-local]{border:1px solid var(--line);background:var(--panel2);color:var(--text);border-radius:9px;padding:9px 10px}.action-warning{border-color:#5b4930;background:#1e1a13}.action-status-line{font-size:12px;color:var(--muted);margin-top:8px}.action-risk{color:var(--warn)}.action-error{color:var(--bad)}.action-buttons{display:flex;gap:7px;flex-wrap:wrap}.btn.danger{border-color:#6a3434;color:#ff9b9b}.btn.warn{border-color:#6f582a;color:#ffd178}.action-meta{font-size:11px;color:var(--muted);line-height:1.5}.action-phase{font-weight:750}.action-phase.active{color:var(--good)}.action-phase.ending,.action-phase.preparing{color:var(--warn)}@media(max-width:900px){.actions-grid{grid-template-columns:1fr}}
</style>
<script>
let actionsTimer=null;
function actionDateValue(d){const z=n=>String(n).padStart(2,'0');return d.getFullYear()+'-'+z(d.getMonth()+1)+'-'+z(d.getDate())+'T'+z(d.getHours())+':'+z(d.getMinutes())}
function installActionsTab(){
  const tabs=$('tabs');if(!tabs||$('actions'))return;
  tabs.insertAdjacentHTML('beforeend','<button class="tab" data-view="actions">Actions</button>');
  const footer=document.querySelector('.footer');if(!footer)return;
  footer.insertAdjacentHTML('beforebegin','<section id="actions" class="view"><div class="notice action-warning" style="margin-bottom:12px"><strong>Extraordinary actions have control priority.</strong> A scheduled self-sufficiency window authorizes battery preparation and Solinteg <code>EMS Off-Grid</code>. A PAUSED fault is never auto-cleared. Normal operation resumes only after three grid-voltage confirmations.</div><div class="actions-grid"><div class="card"><h2>Schedule self-sufficiency</h2><div class="action-form"><label>Start (Europe/Stockholm)<input id="actionStart" type="datetime-local"></label><label>End (Europe/Stockholm)<input id="actionEnd" type="datetime-local"></label><div class="action-meta">Use for a planned power outage. Before the window, normal engine decisions are constrained by a hard battery-readiness floor. During the window, normal battery commands are suppressed.</div><div><button class="btn" id="scheduleSelfSufficiency">Schedule action</button></div><div id="actionFormStatus" class="action-status-line"></div></div></div><div class="card"><h2>Scheduled & recent actions</h2><div id="actionsList" class="table-wrap"><div class="empty">Loading actions…</div></div></div></div></section>');
  const now=new Date(),start=new Date(now.getTime()+60*60*1000),end=new Date(now.getTime()+3*60*60*1000);
  start.setMinutes(Math.ceil(start.getMinutes()/15)*15,0,0);end.setMinutes(Math.ceil(end.getMinutes()/15)*15,0,0);
  $('actionStart').value=actionDateValue(start);$('actionEnd').value=actionDateValue(end);
  $('scheduleSelfSufficiency').addEventListener('click',scheduleSelfSufficiency);
}
function actionLocalIso(v){if(!v)return null;const d=new Date(v);return Number.isNaN(d.getTime())?null:d.toISOString()}
function actionAssessmentHtml(a){
  const x=a.assessment||{};
  if(!x.available)return '<span class="action-risk">'+(x.reason||'Forecast coverage not available yet')+'</span>';
  const target=x.required_start_soc_pct==null?'—':n(x.required_start_soc_pct,1)+'%';
  const now=x.current_soc_pct==null?'—':n(x.current_soc_pct,1)+'%';
  let risk='';
  if(x.power_feasible===false)risk=' · <span class="action-error">forecast peak exceeds battery discharge limit</span>';
  else if(x.energy_feasible===false)risk=' · <span class="action-error">required energy exceeds usable battery capacity</span>';
  return 'Target SOC '+target+' · current '+now+' · forecast coverage '+n(100*(x.coverage_fraction||0),0)+'%'+risk;
}
function renderActions(d){
  const list=(d.actions||[]).slice().reverse();
  if(!list.length){$('actionsList').innerHTML='<div class="empty">No extraordinary actions scheduled.</div>';return}
  let body='';
  list.forEach(a=>{
    const s=new Date(a.starts_at).toLocaleString('sv-SE'),e=new Date(a.ends_at).toLocaleString('sv-SE'),phase=a.phase||a.status||'—',rt=a.runtime||{};
    const terminal=['completed','cancelled','failed'].includes(a.status);
    let controls='';
    if(!terminal){
      controls='<div class="action-buttons"><button class="btn danger" onclick="cancelExtraAction('+a.action_id+')">'+((phase==='active'||phase==='ending')?'Request end':'Cancel')+'</button>';
      if(phase==='active'||phase==='ending')controls+='<button class="btn warn" onclick="forceEndExtraAction('+a.action_id+')">Force end</button>';
      controls+='</div>';
    }
    const detail=rt.last_error?'<div class="action-error">'+String(rt.last_error).slice(0,180)+'</div>':'';
    const conf=rt.grid_confirmations?'<div class="muted">grid confirmations '+rt.grid_confirmations+'/3</div>':'';
    body+='<tr><td>'+s+'<br><span class="muted">→ '+e+'</span></td><td><span class="action-phase '+phase+'">'+phase+'</span>'+conf+'</td><td>'+actionAssessmentHtml(a)+detail+'</td><td>'+controls+'</td></tr>';
  });
  $('actionsList').innerHTML='<table class="tbl"><thead><tr><th>Window</th><th>Phase</th><th>Readiness</th><th>Control</th></tr></thead><tbody>'+body+'</tbody></table>';
}
async function loadActions(){try{renderActions(await api('actions'))}catch(e){if($('actionsList'))$('actionsList').innerHTML='<div class="empty action-error">Could not load actions: '+e.message+'</div>'}}
async function scheduleSelfSufficiency(){
  const status=$('actionFormStatus'),start=actionLocalIso($('actionStart').value),end=actionLocalIso($('actionEnd').value);
  if(!start||!end){status.textContent='Enter a valid start and end time.';status.className='action-status-line action-error';return}
  if(new Date(end)<=new Date(start)){status.textContent='End must be after start.';status.className='action-status-line action-error';return}
  status.textContent='Scheduling and reconciling action…';status.className='action-status-line';
  try{
    const r=await api('actions/self-sufficiency',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({starts_at:start,ends_at:end})});
    status.innerHTML='<strong>Scheduled.</strong> Action #'+((r.action||{}).action_id||'—')+' · '+((r.action||{}).phase||'');
    await loadActions();
  }catch(e){status.textContent='Scheduling failed: '+e.message;status.className='action-status-line action-error'}
}
async function cancelExtraAction(id){try{await api('actions/'+id,{method:'DELETE'});await loadActions()}catch(e){alert('Action could not be cancelled: '+e.message)}}
async function forceEndExtraAction(id){
  if(!confirm('Force end bypasses grid-voltage confirmation. Continue only if you know the grid has returned.'))return;
  try{await api('actions/'+id+'/force-end',{method:'POST'});await loadActions()}catch(e){alert('Force end failed: '+e.message)}
}
installActionsTab();loadActions();
$('tabs')?.addEventListener('click',e=>{if(e.target.closest('.tab')?.dataset.view==='actions')loadActions()});
if(actionsTimer)clearInterval(actionsTimer);actionsTimer=setInterval(()=>{if(document.querySelector('#actions.view.active'))loadActions()},10000);
</script>
'''
