import {longseqBenchmark, renderBenchmark} from './benchmark-ui.js';

// Only use events up to the selected point: later instructions must not leak
// into an earlier action, including when the user scrubs backwards.
export function replayContext(events, selected, objective = '') {
  let instruction=null, action=null, actionInstruction=null;
  for (const e of events.slice(0, selected+1)) {
    if (e.kind === 'feedback' && e.feedback?.next_goal) instruction=e;
    if (e.kind === 'plan') instruction=e;
    if (e.kind === 'action') { action=e; actionInstruction=instruction; }
  }
  const goal = event => event?.feedback?.next_goal ||
    event?.plan?.subtasks?.map(s=>`${s.id}: ${s.objective}`).join('\n') || objective;
  return {instruction, goal:goal(instruction), action, actionGoal:goal(actionInstruction),
    actionInstruction, inputs:instruction?.feedback?.inputs || []};
}

export function nextReplayStep(events, selected) {
  const stops=new Set(['feedback','plan','action','error','invalid_feedback','result']);
  for (let i=selected+1; i<events.length; i++) if (stops.has(events[i].kind)) return i;
  return Math.max(0,events.length-1);
}

export function mount(root, {location, history, isActive}) {
/* Browser Use / Jev Ultrafast (MIT) inspector helpers and rendering pattern,
 * adapted for LongSeq artifacts, timeline replay and continuous live screenshots.
 * See ULTRAFAST-LICENSE.txt. */
const $ = (id) => root.getElementById(id);
const token = document.querySelector('meta[name="demo-token"]').content;
const escape = (value) => String(value ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"})[c]);
const labels = {observation:"页面观察", decision:"模型选择", action:"执行动作", extraction:"记录证据",
  plan:"规划契约", replan_requested:"请求规划", invalid_plan:"规划校验失败", subtask_completed:"子任务完成",
  write_confirmed:"写入已读回", result:"运行结束", error:"运行错误", finish_observation:"完成前核验",
  permission_denied:"权限拒绝", feedback:"Brain 指令", input_binding:"绑定输入",
  action_started:"动作开始", invalid_feedback:"指令校验失败"};
const statuses = {success:"已完成", failed:"失败", budget_exhausted:"预算耗尽", needs_attention:"需要处理", unfinished:"未结束"};
let state = null, currentId = "", selected = 0, tab = "choices", etag = "", loading = false;
let live = null, playing = false, playbackStart = 0, playbackTime = 0, lastImage = "", pendingId = "";
let submitting = false, launcherBusy = false, watchedLaunch = "";
let frameMetadata = null;
let replayRequested = new URLSearchParams(location.search).get('replay') === '1';
const n = value => Number.isFinite(value) ? value.toLocaleString() : "—";
const seconds = value => Number.isFinite(value) ? `${value.toFixed(1)} s` : "—";
const endpoint = (path, extra={}) => `/api/${path}?${new URLSearchParams({id:currentId, ...extra})}`;
function error(message) { $("error").textContent = message || ""; $("error").hidden = !message; }
async function get(url, options={}) {
  const response = await fetch(url, options);
  if (!response.ok) throw Error((await response.json()).error || "无法连接本地服务");
  return response.json();
}
async function runs(preferred) {
  const entries = await get("/api/runs");
  $("run").innerHTML = entries.length ? entries.map(r => `<option value="${escape(r.id)}">${escape(r.id)} · ${escape(statuses[r.status] || r.status)} · ${escape(r.policy)}</option>`).join("") : '<option value="">暂无运行记录</option>';
  const requested = preferred || pendingId || currentId || new URLSearchParams(location.search).get("run");
  const match = entries.find(r => r.id === requested) || (!requested ? entries.find(r => r.policy === "jev" && r.strict_success) : null) || entries[0];
  if (pendingId && !entries.some(r => r.id === pendingId)) return;
  if (match) {
    $("run").value = match.id;
    if (match.id !== currentId) await selectRun(match.id);
  } else $("status").textContent = "暂无记录 · 可运行本地演示";
}
async function selectRun(id) {
  stop(); currentId = id; pendingId = ""; state = null; etag = ""; live = null; lastImage = "";
  clearInstructions();
  $("page-warning").hidden=true;
  $("screenshot").hidden=true; $("empty").hidden=false;
  frameMetadata=null; renderTargets();
  $("follow").checked = !replayRequested;
  $("replay-view").checked = replayRequested;
  root.querySelector('main').classList.toggle('replay-mode',replayRequested);
  history.replaceState(null, "", `?${new URLSearchParams({run:id, ...(replayRequested ? {replay:'1'} : {})})}`);
  await update();
  if(replayRequested && state?.events.length) {selected=0; renderSelection();}
}
async function update() {
  if (!currentId || loading) return;
  loading = true;
  const id = currentId;
  try {
    const response = await fetch(endpoint("run"), {headers: etag ? {"If-None-Match":etag} : {}});
    if (id !== currentId) return;
    if (response.status !== 304) {
      if (!response.ok) throw Error("运行记录正在写入，请稍候刷新");
      const data = await response.json();
      if (id !== currentId) return;
      const wasEmpty = !state;
      state = data; etag = response.headers.get("ETag") || "";
      if (wasEmpty || $("follow").checked) selected = Math.max(0, state.events.length-1);
      renderSummary(); renderSelection(); renderTrail();
    }
    const incoming = await get(endpoint("live"));
    if (id !== currentId) return;
    live = incoming;
    if (live.active && $("follow").checked) showImage(live.frame_index != null ? String(live.frame_index) : "live", live.time, live);
    else if (!playing) showFrame(state?.events[selected]?.at);
    updateStatus(); error("");
  } catch (e) { error(e.message); }
  finally { loading = false; }
}
function renderSummary() {
  const r = state.report?.result || state.result || {}, grade = state.report?.grade || {};
  const benchmark=longseqBenchmark(state.report,state.manifest);
  renderBenchmark(root,benchmark);
  const count = state.events.filter(e => e.kind === "action").length;
  const extracted = new Set(state.events.filter(e => e.kind === "extraction").flatMap(e => e.facts.map(f => f.entity)));
  $("outcome").textContent = statuses[r.status] || "记录中";
  $("outcome").className = r.strict_success ? "success" : r.status === "failed" ? "failed" : "";
  $("grade").textContent = benchmark?.status==='invalid' ? 'SaaS-Bench 官方评分无效'
    : benchmark?.status==='graded' ? `SaaS-Bench 官方 ${grade.earned} / ${grade.total} · 严格${r.strict_success===true?'通过':'未通过'}`
    : r.strict_success === true ? "独立严格判分通过" : r.strict_success === false ? "独立严格判分未通过" : "未提供独立判分";
  $("actions").textContent = n(r.actions ?? count);
  $("reference").textContent = `参考 H ${n(state.manifest.reference_actions)} · 规划 ${n(r.planner_calls ?? state.events.filter(e=>e.kind==='plan').length)}`;
  const total = state.manifest.fixture?.records;
  $("coverage").textContent = `${n(grade.visited?.length ?? extracted.size)}${total ? ` / ${total}` : ""}`;
  $("saved").textContent = grade.actual ? `保存记录 ${grade.actual.length} / ${grade.expected.length} · 重复 ${grade.duplicates}` : `已记录 ${extracted.size} 个实体的证据`;
  $("elapsed").textContent = seconds(state.report?.end_to_end_s ?? r.elapsed_s ?? (state.events.at(-1)?.at - state.events[0]?.at));
  $("models").textContent = `${state.manifest.policy || "—"} + ${state.manifest.feedback_model || state.manifest.planner || "无规划"} · ${state.manifest.backend || "—"}`;
  $("goal").textContent = state.task.objective || "未记录任务目标";
  const start=state.manifest.start_selection;
  $("start-selection").hidden=!start;
  $("start-selection").textContent=start?.url ? `起始网页：${start.url} · ${start.reason}` : start ? (r.status ? '起始网页选择未完成，请查看运行结果。' : '正在根据 prompt 判断起始网页…') : '';
  $("answer-panel").hidden = !r.status;
  $("result-reason").textContent = r.reason || "";
  $("final-answer").textContent = state.report?.final_answer || "";
  $("manifest").textContent = JSON.stringify(state.manifest, null, 2);
  const calls = state.report?.model_calls || [];
  const input = calls.reduce((s,c)=>s+(c.input_tokens || 0),0), output = calls.reduce((s,c)=>s+(c.output_tokens || 0),0);
  $("usage").textContent = state.report ? `HTTP 尝试 ${calls.length} · 已知输入 ${n(input)} / 输出 ${n(output)} tokens · usage 未知 ${calls.filter(c=>c.input_tokens == null || c.output_tokens == null).length} 次 · 费用 ${state.report.total_cost_usd == null ? '未配置 / 未知' : '$'+state.report.total_cost_usd}` : "调用账本在运行结束后汇总。";
  $("download").href = endpoint("download");
  $("run-id").textContent = currentId;
  const option = [...$("run").options].find(o=>o.value===currentId);
  if (option) option.textContent = `${currentId} · ${statuses[r.status] || '记录中'} · ${state.manifest.policy}`;
}
function updateStatus() {
  const result = state?.report?.result || state?.result;
  $("preview-mode").textContent = live?.active && $("follow").checked ? "LIVE" : "REPLAY";
  $("preview-mode").classList.toggle("active", !!live?.active && $("follow").checked);
  $("status").textContent = live?.active ? ($("follow").checked ? "实时连接 · 自动跟随新事件" : "正在运行 · 当前查看历史事件") : result ? `历史回放 · ${statuses[result.status] || result.status}` : live?.time ? "预览已停止或连接中断 · 保留最后记录" : "读取轨迹中 · 此运行未开启实时截图";
}
function title(event) {
  if (!event) return "等待事件";
  if (event.kind === "action") return event.action.description || event.action.operation;
  if (event.kind === "decision") return event.candidates.find(c=>c.id===event.decision.choice)?.description || event.decision.choice;
  if (event.kind === "extraction") return [...new Set(event.facts.map(f=>f.entity))].join(", ") || "无新字段";
  if (event.kind === "subtask_completed") return event.subtask_id;
  if (event.kind === "plan") return `${event.plan.subtasks.length} 个子任务 · ${event.reason}`;
  if (event.kind === "result") return event.result.reason;
  if (event.kind === "invalid_plan") return event.errors.map(e=>e.loc.join(".")+": "+e.type).join("; ");
  if (event.kind === "error") return `${event.error_type}: ${event.detail}`;
  if (event.kind === "write_confirmed") return event.source?.quote || '写入已读回';
  if (event.kind === "feedback") return event.feedback?.next_goal || '未记录新指令';
  return event.reason || event.observation?.title || event.kind;
}
function eventState() {
  let obs=null, decision=null, candidates=[], plan=null, action=null;
  const facts = {}, done = new Set(), pages = new Map();
  for (const e of state.events.slice(0,selected+1)) {
    if (e.observation) {
      obs=e.observation; decision=null; candidates=[];
      const tabs={...obs.tabs, [obs.tab_id]:obs.url};
      for(const url of Object.values(tabs)) if(!pages.has(url)) pages.set(url,{url,observed:false,title:''});
      for(const p of pages.values()) p.tabs=Object.entries(tabs).filter(([,url])=>url===p.url).map(([id])=>id);
      Object.assign(pages.get(obs.url),{observed:true,title:obs.title});
    }
    if (e.kind === "decision") { decision=e.decision; candidates=e.candidates; }
    if (e.kind === "action") { action=e.action; if (action.id.startsWith("internal-")) {decision=null; candidates=[];} }
    if (e.kind === "plan") { plan=e.plan; done.clear(); }
    if (e.kind === "subtask_completed") done.add(e.subtask_id);
    if (e.kind === "extraction") for (const f of e.facts) (facts[f.entity] ||= {})[f.field] = f;
  }
  return {obs,decision,candidates,plan,facts,done,action,pages:[...pages.values()]};
}
function renderSelection() {
  if (!state?.events.length) {clearInstructions(); return;}
  selected = Math.min(selected,state.events.length-1);
  const e = state.events[selected], s = eventState();
  renderInstructions(e);
  $("scrubber").max = state.events.length-1; $("scrubber").value = selected;
  $("position").textContent = `${selected+1} / ${state.events.length}`;
  $("selected-title").textContent = `${labels[e.kind] || e.kind} · ${title(e)}`;
  $("selected-time").textContent = `+${seconds(e.at-state.events[0].at)}`;
  $("url").textContent = s.obs?.url || "尚未观察页面"; $("page-title").textContent = s.obs?.title || "—";
  const navigation=(state.navigations || []).filter(n=>n.time<=e.at && n.url===s.obs?.url).at(-1);
  const httpStatus=s.obs?.http_status ?? navigation?.status;
  $("page-warning").hidden=!(httpStatus>=400);
  $("page-warning").textContent=httpStatus>=400 ? `网页加载失败 · HTTP ${httpStatus}。${httpStatus===403 ? '网站拒绝了任务浏览器的访问。' : ''}下方是该任务实际收到的页面截图，空白不表示仍在加载。${state.manifest.backend === "chrome" ? "当前使用 Chrome 登录会话，请检查该标签页的访问状态。" : "任务浏览器使用独立会话，不继承你当前浏览器的登录状态。"}` : '';
  const chosen = s.candidates.find(a=>a.id===s.decision?.choice);
  $("choice-title").textContent = chosen?.description || s.action?.description || s.action?.operation || "等待局部决策";
  $("confidence").textContent = s.decision?.confidence != null ? `${(s.decision.confidence*100).toFixed(0)}%` : "—";
  const panel=$("inspector");
  if (tab === "choices") panel.innerHTML = s.candidates.length ? s.candidates.map(a=>`<div class="candidate ${a.id===s.decision?.choice?'best':''}"><span>${escape(a.id)}</span><div>${escape(a.description)}<small>${escape(a.operation)} · ${escape(a.effect)}${a.element_ref?' · '+escape(a.element_ref):''}</small></div></div>`).join("") : `<p class="muted">${s.action?.id.startsWith('internal-') ? '控制器执行证据抽取或读回，无模型候选选择。' : '此观察尚无模型决策。选择轨迹中的“模型选择”查看候选。'}</p>`;
  if (tab === "plan") panel.innerHTML = s.plan ? s.plan.subtasks.map(c=>`<article class="plan-card ${s.done.has(c.id)?'complete':''}"><strong>${s.done.has(c.id)?'✓':'○'} ${escape(c.id)}</strong><p>${escape(c.objective)}</p><small>预算 ${c.max_actions} 动作 · 依赖 ${escape(c.depends_on.join(', ') || '无')}</small></article>`).join("") : '<p class="muted">尚未生成有效规划。</p>';
  if (tab === "facts") panel.innerHTML = Object.entries(s.facts).map(([entity,fields])=>`<article class="fact-card"><strong>${escape(entity)}</strong><dl>${Object.values(fields).map(f=>`<dt>${escape(f.field)}</dt><dd>${escape(f.value)}${f.valid?'':' · 无效'}</dd>`).join('')}</dl>${Object.values(fields).map(f=>`<small>${escape(f.source.quote)} · ${escape(f.source.pointer)}</small>`).join('')}</article>`).join('') || '<p class="muted">当前时间点尚未抽取证据。</p>';
  if (tab === "raw") { panel.innerHTML='<pre></pre>'; panel.firstChild.textContent=JSON.stringify(e,null,2); }
  if (tab === "pages") panel.innerHTML=s.pages.map(p=>`<article class="fact-card"><strong>${escape(p.title || '尚未观察内容')}</strong><p>${p.url===s.obs?.url ? '当前页面 · ' : ''}${p.observed ? '已观察' : '仅打开'} · ${escape(p.tabs.length ? p.tabs.join(', ') : '已离开 / 关闭')}</p><small>${escape(p.url)}</small></article>`).join('') || '<p class="muted">尚未记录网页</p>';
  if (!live?.active || !$("follow").checked) showFrame(e.at);
  updateStatus();
  root.querySelectorAll('.event-row').forEach(el=>el.classList.toggle('selected',Number(el.dataset.index)===selected));
}
function clearInstructions() {
  $("instruction-source").textContent='等待事件';
  $("step-instruction").textContent=state?.task?.objective || '正在读取此运行的指令…';
  $("instruction-inputs").replaceChildren(); $("instruction-inputs").hidden=true;
  $("replay-action-source").textContent='尚未执行动作';
  $("replay-action").textContent=''; $("replay-receipt").textContent='';
  $("action-instruction-wrap").hidden=true; $("action-instruction").textContent='';
  $("instruction-memory").textContent='此时间点未记录 working_memory。';
}
function renderInstructions(event) {
  const context=replayContext(state.events,selected,state.task.objective);
  const instruction=context.instruction, actionEvent=context.action, action=actionEvent?.action;
  const position=e=>`事件 ${e.index+1} · +${seconds(e.at-state.events[0].at)}`;
  $("instruction-source").textContent=instruction ? `${instruction.kind==='feedback' ? 'Brain' : '规划'} · ${instruction.phase || 'plan'} · ${position(instruction)}` : '原始任务 · 尚无阶段指令';
  $("step-instruction").textContent=context.goal || '未记录指令';
  $("instruction-inputs").hidden=!context.inputs.length;
  $("instruction-inputs").replaceChildren();
  for(const input of context.inputs) {
    const row=document.createElement('div'), name=document.createElement('dt'), value=document.createElement('dd');
    name.textContent=[input.name,input.grid_ref,input.row_ref].filter(Boolean).join(' · ');
    value.textContent=input.value ?? ''; row.append(name,value); $("instruction-inputs").append(row);
  }
  $("replay-action-source").textContent=actionEvent ? `${actionEvent===event ? '本事件已执行' : '最近已执行'} · ${position(actionEvent)}` : '尚未执行动作';
  $("replay-action").textContent=action ? `${action.operation} · ${action.description || action.id}${action.element_ref ? '\n元素：'+action.element_ref : ''}${action.bound_value != null ? '\n实际输入：'+action.bound_value : ''}` : '当前时间点没有已执行动作。';
  $("replay-receipt").textContent=actionEvent ? `执行结果：${actionEvent.receipt?.status || '未记录回执'}${actionEvent.receipt?.detail ? ' · '+actionEvent.receipt.detail : ''}` : '';
  const different=!!actionEvent && context.actionInstruction!==instruction;
  $("action-instruction-wrap").hidden=!different;
  $("action-instruction").textContent=different ? context.actionGoal : '';
  $("instruction-memory").textContent=instruction?.feedback?.working_memory || '此时间点未记录 working_memory。';
}
function renderTargets() {
  const meta=frameMetadata, boxes=meta?.overlays || [], s=state?.events.length ? eventState() : {};
  const chosen=s.candidates?.find(a=>a.id===s.decision?.choice) || s.action;
  const selectedRef=chosen?.observation_id === meta?.observation_id ? chosen?.element_ref : null;
  $("targets").replaceChildren();
  for(const b of boxes) {
    if(!b.rect || ![b.rect.x,b.rect.y,b.rect.w,b.rect.h,meta.width,meta.height].every(Number.isFinite) || meta.width<=0 || meta.height<=0) continue;
    const box=document.createElement('div'), label=document.createElement('span');
    box.className=`target ${b.editable ? 'editable' : ''} ${b.id===selectedRef ? 'selected' : ''}`;
    box.title=b.label || '';
    // Property assignments work with the inspector's strict style-src CSP.
    box.style.left=`${100*b.rect.x/meta.width}%`; box.style.top=`${100*b.rect.y/meta.height}%`;
    box.style.width=`${100*b.rect.w/meta.width}%`; box.style.height=`${100*b.rect.h/meta.height}%`;
    label.textContent=`${b.id}${b.editable ? ' 输入' : ''}`;
    box.append(label); $("targets").append(box);
  }
  $("targets").hidden=!$("overlays").checked || !boxes.length;
  $("dom-status").textContent=meta?.observation_id ? `${boxes.length} 个元素 · 编号对应观察记录` : '此帧未记录 DOM 标注';
}
function showImage(frame, version="", metadata=null) {
  const url = endpoint("image", {frame, v:version});
  if (url === lastImage) { renderTargets(); return; }
  const meta=metadata || state.frames[frame] || {};
  lastImage=url; frameMetadata=null; renderTargets();
  $("screenshot").onload=()=>{
    if(lastImage!==url) return;
    frameMetadata=meta;
    const w=meta.width || $("screenshot").naturalWidth, h=meta.height || $("screenshot").naturalHeight;
    $("screenshot").parentElement.style.aspectRatio=`${w}/${h}`;
    renderTargets();
  };
  $("screenshot").src=url; $("screenshot").hidden=false; $("empty").hidden=true;
  $("frame-note").textContent = metadata ? `实时截图 · ${new Date(version*1000).toLocaleTimeString()}` : frame === "final" ? "最终截图 · 无可用过程画面" : `录制截图 · ${new Date(meta.time*1000).toLocaleTimeString()}`;
}
function showFrame(at) {
  if (!state) return;
  let index=-1;
  for (let i=0;i<state.frames.length && state.frames[i].time<=at;i++) index=i;
  if (index>=0) showImage(String(index));
  else if (state.has_final && !state.frames.length) showImage("final");
  else {
    $("screenshot").hidden=true; $("empty").hidden=false;
    $("empty-hint").textContent=state.frames.length ? "此时间点尚未记录首帧，可前进查看。" : "等待实时截图或 trace.zip 完成写入。";
    lastImage="";
    frameMetadata=null; renderTargets();
  }
}
function renderTrail() {
  if (!state) return;
  const filter=$("filter").value, query=$("search").value.toLowerCase();
  const important=new Set(['action','feedback','plan','invalid_plan','invalid_feedback','error','result','write_confirmed','subtask_completed','replan_requested']);
  const filtered=state.events.filter(e=>{
    const group=filter==='all'||(filter==='important'&&important.has(e.kind))||e.kind===filter||
      (filter==='plan'&&['replan_requested','invalid_plan','subtask_completed'].includes(e.kind))||
      (filter==='extraction'&&e.kind==='write_confirmed');
    return group && `${e.kind} ${title(e)} ${e.action?.entity || ''}`.toLowerCase().includes(query);
  });
  const container=$("history"), old=container.scrollTop;
  container.innerHTML=filtered.map(e=>{
    const outcome=e.receipt?.status || (e.kind==='subtask_completed'?'verified':e.result?.status || e.kind);
    const good=['ok','verified','success','write_confirmed'].includes(outcome), bad=['error','failed','invalid_plan','rejected'].includes(outcome);
    return `<button class="event-row ${e.index===selected?'selected':''}" data-index="${e.index}"><span class="number">${String(e.index+1).padStart(3,'0')}<small>+${seconds(e.at-state.events[0].at)}</small></span><span>${escape(title(e))}<small>${escape(labels[e.kind] || e.kind)}${e.action?.entity?' · '+escape(e.action.entity):''}</small></span><span class="badge ${good?'ok':bad?'bad':''}">${escape(e.receipt?.status || statuses[e.result?.status] || labels[e.kind] || outcome)}</span></button>`;
  }).join('') || '<p class="muted">没有匹配的事件。</p>';
  container.scrollTop=$("follow").checked ? container.scrollHeight : old;
  $("step-count").textContent=`${filtered.length} / ${state.events.length} 事件`;
}
function seek(index) { stop(); $("follow").checked=false; selected=index; renderSelection(); }
function stop() { playing=false; $("play").textContent='播放'; }
function play(fromStart=false) {
  if (!state?.events.length) return;
  $("follow").checked=false;
  if(fromStart || selected>=state.events.length-1) selected=0;
  playbackTime=state.events[selected].at; playbackStart=performance.now();
  playing=true; $("play").textContent='暂停'; renderSelection();
}
$("run").addEventListener('change',()=>selectRun($("run").value));
$("refresh").addEventListener('click',()=>runs().catch(e=>error(e.message)));
$("scrubber").addEventListener('input',()=>seek(Number($("scrubber").value)));
$("previous").addEventListener('click',()=>seek(Math.max(0,selected-1)));
$("next").addEventListener('click',()=>seek(Math.min((state?.events.length || 1)-1,selected+1)));
$("history").addEventListener('click',e=>{const row=e.target.closest('[data-index]'); if(row) seek(Number(row.dataset.index));});
$("filter").addEventListener('change',renderTrail); $("search").addEventListener('input',renderTrail);
$("follow").addEventListener('change',()=>{stop(); if($("follow").checked && state) selected=state.events.length-1; renderSelection(); update();});
root.querySelectorAll('[data-tab]').forEach(button=>button.addEventListener('click',()=>{
  tab=button.dataset.tab; root.querySelectorAll('[data-tab]').forEach(b=>b.setAttribute('aria-selected',String(b===button))); renderSelection();
}));
$("play").addEventListener('click',()=>{
  if (!state?.events.length) return;
  if(playing) return stop();
  play();
});
$("replay-start").addEventListener('click',()=>play(true));
$("playback-rate").addEventListener('change',()=>{if(playing) play();});
$("replay-view").addEventListener('change',()=>{
  replayRequested=$("replay-view").checked;
  root.querySelector('main').classList.toggle('replay-mode',replayRequested);
  history.replaceState(null,'',`?${new URLSearchParams({run:currentId,...(replayRequested ? {replay:'1'} : {})})}`);
});
setInterval(()=>{
  if(!playing || !state) return;
  const rate=$("playback-rate").value;
  if(rate==='steps') {
    if(performance.now()-playbackStart<2000) return;
    selected=nextReplayStep(state.events,selected); playbackStart=performance.now();
    renderSelection(); if(selected>=state.events.length-1) stop();
    return;
  }
  const at=playbackTime+(performance.now()-playbackStart)/1000*Number(rate);
  let next=selected; while(next+1<state.events.length && state.events[next+1].at<=at) next++;
  if(next!==selected) {selected=next; renderSelection();}
  showFrame(at); if(selected>=state.events.length-1) stop();
},100);
function launchError(message) {
  $("launch-error").textContent=message; $("launch-error").hidden=!message;
}
function launchButtons() {
  $("demo").disabled=$("launch").disabled=submitting || launcherBusy;
  $("launch").textContent=submitting ? '正在启动…' : launcherBusy ? '任务运行中…' : '运行 prompt ↗';
}
async function launcherStatus() {
  try {
    const data=await get('/api/launcher'); launcherBusy=data.running; launchButtons();
    if(data.id?.startsWith('ultrafast/')) {
      $("launch-status").textContent=data.running ? '原始 Ultrafast 正在运行，请切换到“原始 Ultrafast”视图查看。' : '原始 Ultrafast 已结束，可在其独立视图查看记录。';
      return;
    }
    if(data.running) $("launch-status").textContent=`正在执行 · ${data.id} · 可在下方查看实时轨迹`;
    else if(data.id && (watchedLaunch===data.id || !watchedLaunch)) {
      watchedLaunch=data.id;
      $("launch-status").textContent=data.exit_code===0 ? '运行已完成，可查看结果和轨迹。' : '运行已结束，请查看结果中的原因。';
      if(pendingId===data.id) {
        const entries=await get('/api/runs');
        if(!entries.some(r=>r.id===data.id)) {
          pendingId=''; launchError('进程未生成运行记录，请检查该运行目录中的 console.log 后重试。');
        }
      }
    }
  } catch(e) { launchError(e.message); }
}
async function launchTask(path, body) {
  if(submitting || launcherBusy) return;
  submitting=true; launchButtons(); launchError('');
  try {
    const data=await get(path,{method:'POST',headers:{'X-Demo-Token':token,'Content-Type':'application/json'}, ...(body ? {body:JSON.stringify(body)} : {})});
    pendingId=data.id; watchedLaunch=data.id; launcherBusy=true;
    $("launch-status").textContent='任务已提交，正在启动浏览器…';
    for(let i=0;i<20 && pendingId;i++) {await new Promise(r=>setTimeout(r,400)); await runs(pendingId);}
    if(pendingId) $("launch-status").textContent='任务启动中，记录生成后会自动显示。';
  } catch(e) {launchError(e.message);} finally {submitting=false; launchButtons(); await launcherStatus();}
}
$("demo").addEventListener('click',()=>launchTask('/api/demo'));
$("prompt-form").addEventListener('submit',e=>{
  e.preventDefault();
  if(!$("prompt").value.trim()) {launchError('请输入任务 prompt。'); $("prompt").focus(); return;}
  launchTask('/api/launch',{prompt:$("prompt").value,scenario:$("scenario").value,brain:$("brain").value,records:Number($("records").value),url:$("start-url").value});
});
$("prompt").addEventListener('keydown',e=>{if((e.metaKey || e.ctrlKey) && e.key==='Enter') $("prompt-form").requestSubmit();});
$("scenario").addEventListener('change',()=>{
  const web=$("scenario").value==='web';
  $("url-field").hidden=!web;
  $("start-url").disabled=!web;
  $("records-field").hidden=web; $("records").disabled=web;
  $("prompt-hint").textContent=web ? '复用当前 Chrome 的登录状态；网址留空时根据 prompt 自动选择。任务标签页结束后保留，最多运行 5 分钟。' : '目录包含 Rating、Price、Saved，可浏览、翻页、保存。Jev 执行动作，所选模型负责规划；最多 5 分钟。';
});
$("example").addEventListener('click',()=>{
  $("prompt").value=$("scenario").value==='web' ? '打开 example.com，告诉我页面标题和主要内容。' : '查看 item-001 的评分（Rating）和价格（Price），告诉我结果，不要保存或删除任何记录。';
  $("prompt").focus();
});
$("overlays").addEventListener('change',renderTargets);
$("screenshot").addEventListener('error',()=>{lastImage=''; frameMetadata=null; renderTargets(); $("screenshot").hidden=true; $("empty").hidden=false; $("empty-hint").textContent='截图暂不可用，等待下一帧。';});
runs().catch(e=>error(e.message));
launcherStatus();
setInterval(()=>{if(isActive()) launcherStatus();},2000);
setInterval(()=>{if(isActive()) update();},1000);
setInterval(()=>{if(pendingId) runs(pendingId).catch(e=>error(e.message));},2000);

return {navigate: async url => {
  const id=url.searchParams.get("run");
  replayRequested=url.searchParams.get('replay')==='1';
  $("replay-view").checked=replayRequested;
  root.querySelector('main').classList.toggle('replay-mode',replayRequested);
  if(id && id!==currentId) await runs(id);
}, activate: () => {launcherStatus();update();}, deactivate:stop};
}
