export function mount(root, {location, history, isActive}) {
/* Native element ranking and DOM overlays adapted from Jev Ultrafast (MIT).
 * See NOTICE.md and ULTRAFAST-LICENSE.txt. */
const $ = id => root.getElementById(id);
const token = document.querySelector('meta[name="demo-token"]').content;
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {starting:'启动中',ready:'运行中',predicted:'已选择',done:'DONE · 自报完成',blocked:'BLOCKED',needs_input:'需要输入',noop:'NONE · 已暂停',error:'运行失败',timeout:'达到时限',interrupted:'已中断'};
const sec = n => Number.isFinite(n) ? `${n.toFixed(2)} s` : '—';
let current = new URLSearchParams(location.search).get('run') || '', state = {}, meta = {}, comparison = null;
let timeline = [], timelineKey = '', activeRun = false;
let configured = false, busy = false, submitting = false, polling = false, imageVersion = '', exportUrl = '';
async function get(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw Error(data.error || '本地服务不可用');
  return data;
}
function error(id, text='') { $(id).hidden = !text; $(id).textContent = text; }
function controls() { $('launch').disabled = !configured || busy || submitting; }
async function loadRuns(preferred) {
  const entries = await get('/api/ultrafast/runs');
  $('run').innerHTML = entries.length ? entries.map(r => `<option value="${esc(r.id)}">${esc(r.prompt.slice(0,60))} · ${esc(r.started_at)}</option>`).join('') : '<option value="">暂无记录</option>';
  const next = preferred || current || entries[0]?.id || '';
  if (next !== current) { $('follow-decision').checked=true; previewKey=''; previewRequest++; }
  current = next;
  $('run').value = current;
  if (current) history.replaceState(null, '', `?${new URLSearchParams({run:current})}`);
}
async function loadComparisons() {
  const entries = await get('/api/runs');
  const previous = $('comparison').value;
  $('comparison').innerHTML = '<option value="">选择一条 LongSeq 记录</option>' + entries.map(r => `<option value="${esc(r.id)}">${esc(r.id)} · ${esc(r.status)} · ${esc(r.policy)}</option>`).join('');
  $('comparison').value = previous;
}
function renderComparison() {
  const report = comparison?.report, result = report?.result || comparison?.result;
  const calls = report?.model_calls || [];
  const successfulCalls = calls.filter(c => c.status >= 200 && c.status < 300);
  const rows = [
    ['状态', labels[state.status] || state.status || '—', result?.status || '—'],
    ['执行动作', state.history?.length ?? '—', result?.actions ?? '—'],
    ['Jev / 本地策略调用', state.decisions?.length ?? '—', report ? successfulCalls.filter(c=>['jev','llm_policy'].includes(c.kind)).length : '—'],
    ['文本辅助 / 规划等调用', state.text_calls?.length ?? '—', report ? successfulCalls.filter(c=>!['jev','llm_policy','connection_preparation'].includes(c.kind)).length : '—'],
    ['总耗时', sec(state.wall_elapsed_s), sec(report?.end_to_end_s ?? result?.elapsed_s)],
    ['独立严格判分', '未判分', result?.strict_success == null ? '未判分' : result.strict_success ? '通过' : '未通过'],
  ];
  $('comparison-body').innerHTML = rows.map(row => `<tr>${row.map(v=>`<td>${esc(v)}</td>`).join('')}</tr>`).join('');
  if (comparison) {
    const same = meta.prompt && comparison.task?.objective?.trim() === meta.prompt;
    $('comparison-note').textContent = `${same ? '✓ prompt 一致。' : '当前 prompt 尚未匹配，请确认两侧任务一致。'} LongSeq 模型：${comparison.manifest?.policy || '—'} / ${comparison.manifest?.feedback_model || comparison.manifest?.planner || '—'}；HTTP 尝试 ${calls.length} 次。总耗时仅作参考，请结合最终网页与结果判断。`;
  } else $('comparison-note').textContent = '选择相同任务的 LongSeq 记录，对照状态、动作数、调用数和总耗时。';
}
function render(data) {
  meta = data.meta; state = data.state; timeline = data.timeline || []; activeRun = data.active;
  $('outcome').textContent = labels[state.status] || state.status || '—';
  $('actions').textContent = state.history?.length ?? '—';
  $('calls').textContent = `Jev ${state.decisions?.length ?? 0} 次 · 文本 ${state.text_calls?.length ?? 0} 次`;
  $('elapsed').textContent = sec(Number.isFinite(state.elapsed_ms) ? state.elapsed_ms/1000 : undefined);
  $('wall').textContent = sec(state.wall_elapsed_s);
  $('status').textContent = data.connection_message || state.error || state.last_error?.message || (data.active ? '原始 Agent 正在运行 · 自动更新每轮观察' : '运行已停止 · 保留原生记录与最后截图');
  $('stop').disabled = !data.active;
  updateDecisionPicker();
  renderSnapshots();
  updatePreview();
  const decisions = state.decisions || [];
  $('step-count').textContent = `${decisions.length} 次决策 · ${state.history?.length || 0} 个动作`;
  $('history').innerHTML = decisions.map((d,i) => `<button class="event-row ${String(i)===previewIndex ? 'selected' : ''}" data-decision="${i}"><span class="number">${i+1}<small>${sec(d.elapsed_ms/1000)}</small></span><span>${esc(d.operation)} ${d.target ? '· DOM ['+esc(d.target)+']' : ''}<small>${esc(d.choice)}</small></span><span>${sec(d.latency_ms/1000)}</span></button>`).join('') || '<p class="muted">暂无模型决策。</p>';
  $('metadata').textContent = JSON.stringify(meta,null,2); $('raw').textContent = JSON.stringify(state,null,2); $('run-id').textContent = current;
  if (exportUrl) URL.revokeObjectURL(exportUrl);
  exportUrl = URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));
  $('export').href = exportUrl; $('export').download = `${current}.json`; $('export').hidden=false;
  renderComparison();
}
// Keep each decision's screenshot, element indices and probabilities together.
let previewIndex = 'page', previewKey = '', previewContext = null, previewRequest = 0;
let inspectedIndex = null;
const percent = value => Number.isFinite(value) ? `${(value*100).toFixed(value<.01 ? 1 : 0)}%` : '—';
const effect = action => action.page_changed === true ? '页面已变化' : action.page_changed === false ? '未观察到页面变化' : '尚未确认页面变化';
function renderSnapshots() {
  const decisions = state.decisions || [];
  const entries = decisions.map((d,i) => {
    const entry = timeline[i] || {}, action = entry.action;
    return {index:String(i), frame:entry.frame, title:`#${i+1} · ${d.operation}`,
      subtitle:action ? `已执行 · ${action.action}` : ['DONE','NONE','BLOCKED'].includes(d.operation) ? '结束判断 · 未执行动作' : '未记录执行',
      time:sec(d.elapsed_ms/1000)};
  });
  if (state.page?.url) entries.push({index:'page',frame:state.frame,title:activeRun ? '最新页面' : '最终页面',subtitle:state.page.title || state.page.url,time:''});
  const key = JSON.stringify([current,entries]);
  if (key !== timelineKey) {
    timelineKey = key;
    const strip=$('snapshot-strip'), scroll=strip.scrollLeft, focused=strip.contains(root.activeElement) ? root.activeElement.dataset.decision : null;
    strip.innerHTML=entries.map(entry=>`<button type="button" class="snapshot-card" data-decision="${entry.index}" aria-label="${esc(entry.title+' · '+entry.subtitle)}"><span class="snapshot-image">${entry.frame ? `<img loading="lazy" alt="" src="/api/ultrafast/image?${esc(new URLSearchParams({id:current,frame:entry.frame}).toString())}">` : '未保存截图'}</span><strong>${esc(entry.title)}</strong><small>${esc(entry.subtitle)}</small><small>${esc(entry.time || '本次运行的最后观察')}</small></button>`).join('') || '<p class="muted">每次决策的页面快照会保留在这里。</p>';
    strip.scrollLeft=scroll;
    if (focused != null) [...strip.children].find(el=>el.dataset.decision===focused)?.focus({preventScroll:true});
  }
  syncReplaySelection();
}
function syncReplaySelection() {
  const index=$('decision-picker').value || 'page', decisions=state.decisions || [];
  const position=index==='page' ? decisions.length : Number(index);
  const previous=$('snapshot-strip').querySelector('.selected')?.dataset.decision;
  root.querySelectorAll('#snapshot-strip [data-decision], #history [data-decision]').forEach(el=>{
    const selected=el.dataset.decision===index;
    el.classList.toggle('selected',selected); el.setAttribute('aria-pressed',String(selected));
    if(selected && el.classList.contains('snapshot-card') && previous!==index) {
      const strip=$('snapshot-strip');
      const left=el.getBoundingClientRect().left-strip.getBoundingClientRect().left;
      if(left<0)strip.scrollLeft+=left-2;
      else if(left+el.offsetWidth>strip.clientWidth)strip.scrollLeft+=left+el.offsetWidth-strip.clientWidth+2;
    }
  });
  $('previous-snapshot').disabled=position<=0 || !decisions.length;
  $('next-snapshot').disabled=index==='page' || !state.page?.url;
  $('replay-position').textContent=index==='page' ? (activeRun ? '最新观察' : '最终页面') : `${$('follow-decision').checked ? '跟随最新' : '历史回看'} · ${position+1} / ${decisions.length}`;
  const d=decisions[position], action=timeline[position]?.action;
  $('execution-detail').innerHTML=index==='page' ? `<p>${activeRun ? '当前最新观察' : '运行结束后的最后观察'} · 共 ${decisions.length} 次决策，${state.history?.length || 0} 个执行动作</p><small>点选历史快照，查看动作选择前的页面与对应 DOM。</small>` : action ? `<p><b>动作 ${esc(action.step)} · ${esc(action.action)}</b> · ${esc(d?.operation)}${d?.target != null ? ' · DOM ['+esc(d.target)+']' : ''}</p>${action.text != null ? `<p>输入：<b>“${esc(action.text)}”</b></p>` : ''}<small>${esc(effect(action))} · 决策 ${esc(action.latency_ms)} ms · 执行于 ${sec(action.executed_ms/1000)}${action.text_helper ? ' · 文本辅助 '+esc(action.text_helper) : ''}</small>` : `<p>${esc(d?.operation || '等待决策')} · ${['DONE','NONE','BLOCKED'].includes(d?.operation) ? '结束判断，无浏览器动作' : '尚无对应执行记录'}</p><small>快照显示本次决策选择时的页面。</small>`;
}
function updateDecisionPicker() {
  const decisions=state.decisions || [], picker=$('decision-picker');
  const desired=$('follow-decision').checked && decisions.length ? String(decisions.length-1) : picker.value || 'page';
  picker.innerHTML='<option value="page">最新页面 · 当前 DOM</option>'+decisions.map((d,i)=>`<option value="${i}">#${i+1} · ${esc(d.operation)}${d.target ? ' · DOM ['+esc(d.target)+']' : ''}</option>`).join('');
  picker.value=[...picker.options].some(o=>o.value===desired) ? desired : 'page';
}
function highlightDOM(index) {
  inspectedIndex=index;
  root.querySelectorAll('#targets .target, #choices .choice').forEach(el=>el.classList.toggle('inspected',el.dataset.element===index));
}
function renderNative(context) {
  previewContext=context; inspectedIndex=null;
  const page=context.page || {}, d=context.decision;
  const chosen=(page.actions || []).find(a=>a.id===d?.choice);
  const selected=d?.target == null ? null : String(d.target).split(':')[0];
  $('choice').textContent=chosen?.label || (context.elements || []).find(e=>String(e.index)===selected)?.label || (d ? `${d.operation}${d.target ? ' ['+d.target+']' : ''}` : '当前页面 DOM');
  $('latency').textContent=d ? `${d.latency_ms} ms` : '—';
  $('target-confidence').textContent=percent(d?.target_confidence);
  $('operation').textContent=d?.operation || '—';
  $('ranking-note').textContent=d ? 'Ranked by Jev' : 'Unranked';
  $('operation-choices').innerHTML=Object.entries(d?.operation_probabilities || {}).sort((a,b)=>b[1]-a[1]).map(([op,p])=>`<span class="operation-choice ${op===d.operation ? 'best' : ''}">${esc(op)} <b>${percent(p)}</b></span>`).join('');
  const probability=e=>d?.target_probabilities?.[e.index] ?? Math.max(-1,...(e.options || []).map(o=>d?.target_probabilities?.[o.index] ?? -1));
  const elements=[...(context.elements || [])];
  if (d) elements.sort((a,b)=>probability(b)-probability(a));
  $('choices').innerHTML=elements.map(e=>{
    const p=probability(e), index=String(e.index);
    return `<button type="button" class="choice ${selected===index ? 'best' : ''}" data-element="${esc(index)}" aria-label="DOM ${esc(index)} ${esc(e.label)}"><span class="choice-id">[${esc(index)}]</span><span class="choice-label">${esc(e.label)}<small>${esc(e.role)} · ${esc((e.operations || []).join(' / '))}${e.value ? ' · '+esc(e.value) : ''}</small>${p>=0 ? '<span class="bar"></span>' : ''}</span><span class="probability">${p>=0 ? percent(p) : '—'}</span></button>`;
  }).join('') || '<p class="muted">此记录未保存对应的 DOM 元素。</p>';
  [...$('choices').children].forEach((el,i)=>{ const bar=el.querySelector('.bar'); if(bar) bar.style.width=`${Math.max(0,Math.min(100,probability(elements[i])*100))}%`; });
  $('url').textContent=page.url || '未保存决策时的页面'; $('page-title').textContent=page.title || '—';
  $('preview-mode').textContent=previewIndex==='page' ? 'LATEST PAGE' : `DECISION #${Number(previewIndex)+1}`;
  $('context-note').textContent=context.context_available===false ? '旧记录缺少此 decision 的原始观察，不能将目标编号关联到后续页面。' : !context.frame ? '此旧记录保留了决策对应的 DOM，但未保留当时截图；新运行可逐条回看。' : previewIndex==='page' ? '当前观察的 DOM；选择一条 decision 查看其选择时的页面。' : '选择时的页面与 DOM · 橙色框为 Jev 选中目标，悬停或点击候选可定位元素。';
  $('targets').hidden=true; $('targets').replaceChildren();
  if (page.w>0 && page.h>0) $('screenshot').parentElement.style.aspectRatio=`${page.w}/${page.h}`;
  if (!context.frame) {
    imageVersion=''; $('screenshot').hidden=true; $('empty').hidden=false;
    $('empty-hint').textContent='此决策未保留对应截图，可在右侧查看已记录的 DOM 与概率。';
    renderTargets(false); return;
  }
  const version=`${current}/${context.frame}/${context.frame==='latest' ? state.updated_at : ''}`;
  if (imageVersion===version && !$('screenshot').hidden) { renderTargets(true); return; }
  imageVersion=version; $('screenshot').hidden=true; $('empty').hidden=false;
  $('empty-hint').textContent='正在加载此观察的截图…';
  $('screenshot').onload=()=>{ if(imageVersion!==version)return; $('screenshot').hidden=false; $('empty').hidden=true; renderTargets(true); };
  $('screenshot').onerror=()=>{ if(imageVersion!==version)return; $('screenshot').hidden=true; $('empty').hidden=false; $('empty-hint').textContent='此观察的截图不可用。'; renderTargets(false); };
  $('screenshot').src=`/api/ultrafast/image?${new URLSearchParams({id:current,frame:context.frame,v:context.frame==='latest' ? state.updated_at : ''})}`;
}
function renderTargets(imageReady) {
  const c=previewContext || {}, page=c.page || {}, selected=c.decision?.target == null ? null : String(c.decision.target).split(':')[0];
  $('targets').replaceChildren();
  let count=0;
  for (const box of c.overlays || []) {
    const r=box.rect;
    if (!r || ![r.x,r.y,r.w,r.h,page.w,page.h].every(Number.isFinite) || page.w<=0 || page.h<=0 || r.w<=0 || r.h<=0) continue;
    const el=document.createElement('button'), label=document.createElement('span'), index=String(box.index);
    el.type='button'; el.className=`target ${box.editable ? 'editable' : ''} ${index===selected ? 'selected' : ''}`;
    el.dataset.element=index; el.title=box.label || ''; el.setAttribute('aria-label',`DOM ${index} ${box.label || ''}`);
    el.style.left=`${100*r.x/page.w}%`; el.style.top=`${100*r.y/page.h}%`;
    el.style.width=`${100*r.w/page.w}%`; el.style.height=`${100*r.h/page.h}%`;
    label.textContent=`${index}${box.editable ? ' 输入' : ''}`; el.append(label); $('targets').append(el); count++;
  }
  $('dom-status').textContent=`${(c.elements || []).length} 个 DOM 元素 · ${count} 个可见框${selected ? ' · 已选 ['+selected+']' : ''}`;
  $('targets').hidden=!imageReady || !$('overlays').checked;
  highlightDOM(inspectedIndex);
}
async function updatePreview() {
  const index=$('decision-picker').value || 'page'; previewIndex=index;
  syncReplaySelection();
  const key=`${current}/${index}/${index==='page' ? state.updated_at : (state.decisions || [])[Number(index)]?.fingerprint}`;
  if (key===previewKey) return;
  previewKey=key; const request=++previewRequest, id=current;
  $('targets').hidden=true; $('screenshot').hidden=true; $('empty').hidden=false;
  $('choice').textContent='正在读取决策…'; $('choices').replaceChildren();
  try {
    let context;
    if(index==='page') context={page:state.page,elements:state.elements,overlays:state.overlays,frame:state.frame || (state.page?.url ? 'latest' : null),context_available:!!state.page};
    else context=await get(`/api/ultrafast/decision?${new URLSearchParams({id,index})}`);
    if(request!==previewRequest || current!==id)return;
    renderNative(context);
  } catch(e) { if(request===previewRequest) {previewKey=''; error('error',e.message);} }
}
function chooseDecision(index) {
  $('follow-decision').checked=false; $('decision-picker').value=String(index); updatePreview();
  root.querySelectorAll('#history [data-decision]').forEach(el=>el.classList.toggle('selected',el.dataset.decision===String(index)));
}
function moveSnapshot(delta) {
  const count=(state.decisions || []).length, currentIndex=$('decision-picker').value;
  const next=Math.max(0,Math.min(count,(currentIndex==='page' ? count : Number(currentIndex))+delta));
  chooseDecision(next===count ? 'page' : String(next));
}
$('previous-snapshot').onclick=()=>moveSnapshot(-1);
$('next-snapshot').onclick=()=>moveSnapshot(1);
$('return-live').onclick=()=>{ $('follow-decision').checked=true; updateDecisionPicker(); updatePreview(); };
$('snapshot-strip').onclick=e=>{const row=e.target.closest('[data-decision]');if(row)chooseDecision(row.dataset.decision);};
$('snapshot-strip').onkeydown=e=>{if(['ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();moveSnapshot(e.key==='ArrowLeft' ? -1 : 1);$('snapshot-strip').querySelector('.selected')?.focus({preventScroll:true});}};
$('decision-picker').onchange=()=>chooseDecision($('decision-picker').value);
$('follow-decision').onchange=()=>{updateDecisionPicker();updatePreview();};
$('overlays').onchange=()=>renderTargets(!$('screenshot').hidden);
$('history').onclick=e=>{const row=e.target.closest('[data-decision]');if(row)chooseDecision(row.dataset.decision);};
for(const id of ['choices','targets']) {
  $(id).onpointerover=e=>{const el=e.target.closest('[data-element]');if(el)highlightDOM(el.dataset.element);};
  $(id).onpointerleave=()=>highlightDOM(null);
  $(id).onfocusin=e=>{const el=e.target.closest('[data-element]');if(el)highlightDOM(el.dataset.element);};
  $(id).onclick=e=>{const el=e.target.closest('[data-element]');if(!el)return;highlightDOM(el.dataset.element);if(id==='targets'){const row=[...$('choices').children].find(x=>x.dataset.element===el.dataset.element);row?.scrollIntoView({block:'nearest'});}};
}
async function poll() {
  if (polling) return;
  polling=true;
  try {
    const launcher = await get('/api/launcher'); busy=launcher.running; controls();
    if (current) {
      const id=current, data=await get(`/api/ultrafast/run?${new URLSearchParams({id})}`);
      if (current===id) render(data);
    }
    error('error');
  } catch(e) { error('error',e.message); }
  finally { polling=false; }
}
$('prompt-form').onsubmit = async e => {
  e.preventDefault(); if (submitting || busy) return;
  submitting=true; controls(); error('launch-error'); $('launch-status').textContent='正在启动原始 Agent…';
  try {
    const run = await get('/api/ultrafast/launch',{method:'POST',headers:{'Content-Type':'application/json','X-Demo-Token':token},body:JSON.stringify({prompt:$('prompt').value,url:$('start-url').value})});
    await loadRuns(run.id); $('screenshot').hidden=true; $('empty').hidden=false;
    $('launch-status').textContent='已启动；切换视图不会停止任务。'; await poll();
  } catch(e) { error('launch-error',e.message); $('launch-status').textContent=''; }
  finally { submitting=false; controls(); }
};
$('run').onchange = async () => { current=$('run').value; imageVersion=''; previewKey=''; previewContext=null; previewRequest++; $('follow-decision').checked=true; $('targets').hidden=true; $('screenshot').hidden=true; $('empty').hidden=false; history.replaceState(null,'',`?${new URLSearchParams({run:current})}`); await poll(); };
$('refresh').onclick = async () => { try { await Promise.all([loadRuns(),loadComparisons()]); await poll(); } catch(e) { error('error',e.message); } };
$('example').onclick = () => { $('prompt').value='打开 https://example.com，查看页面标题后停止。'; $('start-url').value='https://example.com'; };
$('stop').onclick = async () => {
  $('stop').disabled=true;
  try {
    await get('/api/ultrafast/stop',{method:'POST',headers:{'Content-Type':'application/json','X-Demo-Token':token},body:JSON.stringify({id:current})});
    await poll();
  } catch(e) { error('error',e.message); }
};
$('comparison').onchange = async () => {
  const id=$('comparison').value;
  try {
    comparison = id ? await get(`/api/run?${new URLSearchParams({id})}`) : null;
    $('reuse').disabled=!comparison;
    $('open-longseq').href=id ? `/?${new URLSearchParams({run:id})}` : '/';
    renderComparison();
  } catch(e) { error('error',e.message); }
};
$('reuse').onclick = () => {
  $('prompt').value=comparison.task?.objective || '';
  $('start-url').value=comparison.manifest?.start_selection?.url || comparison.task?.start_url || '';
  $('prompt').focus(); $('prompt-form').scrollIntoView({block:'start',behavior:'smooth'});
};
(async () => {
  try {
    const config=await get('/api/ultrafast/config'); configured=config.available && config.configured;
    $('configuration').textContent=config.message || `${config.model} · 文本辅助 ${config.text_model} · 本地原始源码`;
    await Promise.all([loadRuns(),loadComparisons()]); renderComparison(); await poll();
  } catch(e) { error('error',e.message); }
  controls(); setInterval(()=>{if(isActive()) poll();},1200);
})();

return {navigate: async url => {const id=url.searchParams.get("run");if(id && id!==current){await loadRuns(id);await poll();}}, activate:poll};
}
