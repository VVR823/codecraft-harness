import {createSelection, renderText, canResume, resumePayload} from './state.mjs';
const $ = id => document.getElementById(id);
let selected = '', current = null, packs = [], offset = 0;
async function api(path, body) {
  const token = $('token').value.trim();
  const headers = token ? {Authorization: 'Bearer '+token} : {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch('/api/'+path, {method: body === undefined ? 'GET':'POST',headers,body:body===undefined?undefined:JSON.stringify(body)});
  const value = await response.json();
  if (!response.ok) throw Error(typeof value.detail === 'string' ? value.detail : '参数不合法，请检查输入（HTTP '+response.status+'）');
  return value;
}
function message(text) { renderText($('notice'), text); }
function element(tag, text, className) {
  const el = document.createElement(tag);renderText(el,text);
  if (className) el.className = className;return el;
}
function fillPacks() {
  const mode = $('mode').value;
  $('pack').replaceChildren(...packs.filter(p=>mode!=='offline_demo'||p.offline_demo).map(p=>{
    const option=element('option',p.task_id+' · '+p.desc);option.value=p.task_id;return option;
  }));
  $('local-fields').hidden = mode === 'offline_demo';
  renderText($('mode-help'),mode==='offline_demo'?'固定脚本演示 T1/T2/T3 的工具、验证与断点流程；不调用模型。':'使用服务端配置的本机模型端点。模型与耗时取决于你已有的本地服务。');
}
async function loadRuns() {
  const {runs}=await api('runs');
  $('runs').replaceChildren(...runs.map(run=>{
    const button=element('button',`${run.task_id}\n${run.run_id} · ${run.status} · ${run.mode}`,'run');
    button.setAttribute('aria-current',String(run.run_id===selected));
    button.onclick=()=>choose(run.run_id);return button;
  }));
  if (!runs.length) $('runs').append(element('p','还没有运行记录。先试一次离线演示。','muted'));
}
async function ensurePacks() {
  if (!packs.length) {
    packs=(await api('packs')).packs;
    fillPacks();
    message('');
  }
}
function render(run) {
  current=run;$('empty').hidden=true;$('detail').hidden=false;
  renderText($('run-title'),run.task_id);
  renderText($('run-id'),run.run_id);
  renderText($('evidence'),run.mode==='offline_demo'?'离线固定演示 · 无模型调用':run.mode==='legacy'?'历史记录 · 缺少可恢复基线':'本地模型任务');
  renderText($('status'),run.status);$('status').dataset.status=run.status;
  renderText($('verified'),run.verified?'验证通过':'尚未验证通过');
  renderText($('steps'),run.steps);
  renderText($('usage'),`${run.known_tokens} / ${run.options.token_budget ?? '未知'}`);
  renderText($('unknown'),run.unknown_calls);
  renderText($('workspace'),run.workspace_path || '历史记录无隔离工作区');
  renderText($('error'),run.error_code?`${run.error_code}：${run.error_message}`:'暂无持久化错误');
  renderText($('approvals'),run.approvals.length?run.approvals.map(a=>`${a.action} · ${a.note}`).join('\n'):'暂无审批记录');
  $('resume').hidden=!canResume(run);$('budget-row').hidden=run.status!=='budget_paused';
  renderText($('resume'),run.status==='running'?'检查中断并尝试恢复':'人工续跑一段');
  if (document.activeElement!==$('new-budget')) $('new-budget').value=Math.min(1000000,Math.max(run.options.token_budget || 0,run.known_tokens)+30000);
  $('download').disabled=run.active;
}
const selection=createSelection(id=>api('tasks/'+encodeURIComponent(id)),render);
async function loadTraces(id) {
  const versionOffset=offset;
  const data=await api(`tasks/${encodeURIComponent(id)}/traces?limit=50&offset=${versionOffset}`);
  if (id!==selected||versionOffset!==offset) return;
  $('traces').replaceChildren(...data.traces.map(t=>{
    const item=element('li','');item.append(element('strong',`#${t.step} ${t.tool||'状态'} · ${t.verdict}`),element('pre',t.output_summary));return item;
  }));
  renderText($('trace-page'),`${offset+1}–${Math.min(offset+50,data.total)} / ${data.total}`);
  $('prev').disabled=offset===0;$('next').disabled=offset+50>=data.total;
}
async function choose(id) {
  selected=id;offset=0;message('');
  try { await Promise.all([selection.load(id),loadTraces(id),loadRuns()]); } catch(e) {message(e.message);}
}
$('mode').onchange=fillPacks;
$('refresh').onclick=async()=>{try{await ensurePacks();await loadRuns();message('');if(selected)await choose(selected);}catch(e){message(e.message);}};
$('token').onchange=()=> $('refresh').onclick();
$('new-run').onsubmit=async e=>{
  e.preventDefault();$('submit').disabled=true;message('正在创建私有工作区…');
  try {
    const body={task_id:$('pack').value,mode:$('mode').value,token_budget:Number($('budget').value),use_plan:$('plan').checked,context_compress:$('compress').checked};
    if(body.mode==='local') {if($('model').value.trim())body.model=$('model').value.trim();body.goal=$('goal').value;}
    const run=await api('tasks',body);await choose(run.run_id);
  } catch(e) {message(e.message);} finally {$('submit').disabled=false;}
};
$('resume').onclick=async()=>{
  const id=selected;const run=current;$('resume').disabled=true;
  try {await api('tasks/'+encodeURIComponent(id)+'/resume',resumePayload(run,$('new-budget').value));if(id===selected)await choose(id);}
  catch(e){message(e.message);}finally{$('resume').disabled=false;}
};
for(const [button,delta] of [['prev',-50],['next',50]]) $(button).onclick=async()=>{offset=Math.max(0,offset+delta);try{await loadTraces(selected);}catch(e){message(e.message);}};
$('download').onclick=async()=>{
  try{
    const result=await api('tasks/'+encodeURIComponent(selected)+'/result');
    const url=URL.createObjectURL(new Blob([JSON.stringify(result,null,2)],{type:'application/json'}));
    const a=document.createElement('a');a.href=url;a.download=`codecraft-${selected}.json`;a.click();URL.revokeObjectURL(url);
  }catch(e){message(e.message);}
};
async function poll(){
  try{await ensurePacks();await loadRuns();if(selected){const id=selected;await Promise.all([selection.load(id),loadTraces(id)]);}}
  catch(e){message(e.message);}finally{setTimeout(poll,3000);}
}
try{await ensurePacks();await loadRuns();}catch(e){message(e.message);}
setTimeout(poll,3000);
