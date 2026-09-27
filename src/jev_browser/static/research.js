const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num = value => Number.isFinite(value) ? value.toLocaleString() : '—';
const sec = value => Number.isFinite(value) ? `${value.toFixed(1)} s` : '—';
const labels = {interrupted:'已中断',running:'运行中',blocked:'环境未就绪',stopped:'已停止',complete:'完成',success:'通过',failed:'未通过',budget_exhausted:'预算耗尽',baseline:'基线',keep:'保留',reject:'拒绝',inconclusive:'无法比较'};
const reasons = {public_data_unavailable:'公共网页数据校验失败，研究已停止','public-web_unavailable':'公共网页或模型配置尚未就绪',webarena_unavailable:'WebArena 环境尚未就绪',trial_budget:'已完成设定轮次',researcher_failed:'Codex 研究分析未完成',transport_instability:'模型连接失败，研究已停止',incomparable_trial:'对照条件或用量不足，无法比较',insufficient_time_for_a_comparable_trial:'剩余时间不足以完成同预算对照',no_supported_untried_improvement:'没有可继续验证的新提案',interrupted_or_runner_error:'研究中断或执行器出错'};
const fields = {brain_interval:'大脑反馈间隔',recent_evidence:'近期证据数',excerpt_chars:'证据摘录长度',prompt_variant:'提示词版本'};
const taskNames = {50:'全年已完成订单：数量与总支出',332:'第一季度：逐月购物支出'};
let selected = new URLSearchParams(location.search).get('study') || '', pending = '', data = null, busy = false, submitting = false, sidebarSignature = '', renderSignature = '';
async function get(url, options={}) {
  const response = await fetch(url, options);
  const value = await response.json();
  if (!response.ok) throw Error(value.error || '本地服务暂不可用');
  return value;
}
function error(message='') { $('error').textContent=message; $('error').hidden=!message; }
function badge(value) { return `<span class="badge ${['reject','failed','blocked'].includes(value)?value:''}">${esc(labels[value] || value || '等待')}</span>`; }
function usage(metrics) {
  if (!metrics) return '—';
  return `${num(metrics.known_input_tokens + metrics.known_output_tokens)}${metrics.unknown_usage_attempts ? ' + 未知' : ''}`;
}
async function refresh() {
  if (busy) return; busy=true;
  try {
    const [rows, launcher] = await Promise.all([get('/api/studies'),get('/api/launcher')]);
    if (pending && rows.some(r=>r.id===pending)) {selected=pending;pending='';}
    if (!selected || !rows.some(r=>r.id===selected)) selected=rows[0]?.id || '';
    const signature = JSON.stringify([rows,selected]);
    if (signature!==sidebarSignature) {
      $('studies').innerHTML=rows.map(r=>`<button class="study-choice ${r.id===selected?'active':''}" data-study="${esc(r.id)}" aria-pressed="${r.id===selected}"><strong>${r.suite==='public-web'?'公共网页 · 两任务':r.suite==='webarena'?'WebArena · 两任务':'目录任务'}</strong><small>${esc(labels[r.status] || r.status)} · ${r.trials} 轮</small><small>${esc(r.started_at ? new Date(r.started_at).toLocaleString() : r.id)}</small></button>`).join('') || '<p class="muted">还没有研究记录</p>';
      sidebarSignature=signature;
    }
    $('start').disabled=submitting || launcher.running;
    if (selected) {
      data=await get(`/api/study?${new URLSearchParams({id:selected})}`);
      history.replaceState(null,'',`/research?${new URLSearchParams({study:selected})}`);
      const nextSignature=JSON.stringify(data);
      if(nextSignature!==renderSignature) { render(data); renderSignature=nextSignature; } else updateElapsed(data);
      $('study').hidden=false; $('empty').hidden=true;
      $('start').disabled ||= data.status==='running' && data.process_alive;
    } else { $('study').hidden=true; $('empty').hidden=false; }
    $('updated').textContent=`更新于 ${new Date().toLocaleTimeString()} · 每 2 秒刷新`;
    error();
  } catch (e) { error(e.message); } finally {busy=false;}
}
function updateElapsed(s) {
  const snapshot=s.budget.updated_at?Date.parse(s.budget.updated_at):NaN;
  const extra=s.status==='running' && s.process_alive && Number.isFinite(snapshot)?Math.max(0,(Date.now()-snapshot)/1000):0;
  $('elapsed').textContent=sec((s.budget.elapsed_s ?? 0)+extra);
}
function render(s) {
  $('study-id').textContent=s.id;
  $('scope-trials').textContent=`${['webarena','public-web'].includes(s.config.suite)?2:1} 个任务 × ${s.limits?.max_trials || 2} 轮配置`;
  $('scope-budget').textContent=`每任务最多 ${s.limits?.max_seconds || s.config.max_seconds} 秒 · 研究预算 ${num(s.limits?.max_tokens || 600000)} tokens`;
  $('study-title').textContent=s.config.suite==='public-web'?'公共网页 · 两任务对照':s.config.suite==='webarena'?'WebArena · 两任务对照':'目录任务 · 配置研究';
  $('model-line').textContent=`执行 ${s.config.models?.PLANNER_MODEL || 'DeepSeek'} + ${s.config.models?.TYPESAFE_MODEL || 'Jev'} · 研究 ${s.config.researcher?.model || s.config.codex_model} / ${s.config.codex_effort}`;
  const interrupted=s.status==='running' && !s.process_alive;
  $('badge').textContent=interrupted?'进程已退出':s.stop_reason==='trial_budget'?'轮次完成':labels[s.status] || s.status;
  $('badge').className=`badge ${s.status==='blocked'||interrupted?'blocked':''}`;
  const pre=s.preflight, blocked=s.status==='blocked';
  $('notice').hidden=!(blocked || interrupted || (s.stop_reason && s.stop_reason!=='trial_budget'));
  $('notice-title').textContent=interrupted?'研究进程已退出，记录已保留':reasons[s.stop_reason] || s.stop_reason || '';
  $('notice-text').textContent=blocked?'尚未执行 benchmark，也没有消耗模型调用。配置就绪后可重试环境检查。':interrupted?'请检查原始状态和运行日志；不会自动重放未完成任务。':'此前的基线与有效配置仍然保留。失败或提前退出不会被算作性能提升。';
  const missing=[...(pre?.missing_environment || []),...(pre?.unreachable_sites || []).map(x=>`${x.site}: ${x.error}`),...Object.entries(pre?.model_keys_present || {}).filter(([,v])=>!v).map(([k])=>k)];
  $('missing').innerHTML=blocked?missing.map(x=>`<span>${esc(x)}</span>`).join(''):'';
  $('retry').hidden=!(blocked && !s.trials.length); $('retry').disabled=submitting;
  const completed=s.trials.filter(t=>t.status==='complete'), incumbent=s.trials.find(t=>t.id===s.incumbent);
  const passed=incumbent?.result?.passed_tasks ?? (incumbent?.result?.strict_success===true?1:incumbent?0:null);
  $('success').textContent=passed===null?'—':`${passed} / ${incumbent.result.total_tasks || 1}`;
  $('quality-note').textContent=incumbent?'独立判分 · 逐任务防止退化':s.trials.length?'轮次未完成，详见逐任务结果':'尚未运行任务';
  $('trials-count').textContent=`${completed.length} / ${s.limits?.max_trials || 2}`;
  $('trial-note').textContent=s.active_trial!==undefined && s.status==='running'?`当前第 ${s.active_trial+1} 轮`:'基线 + 候选对照';
  $('tokens').textContent=num(s.budget.known_tokens ?? 0);
  $('token-note').textContent=`未知用量 ${s.budget.unknown_usage_attempts || 0} 次 · 上限 ${num(s.limits?.max_tokens || 600000)}`;
  updateElapsed(s);
  $('call-note').textContent=`已派发 ${s.budget.attempts || 0} 次 · 已返回 ${s.budget.completed_attempts || 0} 次`;
  const phases=[['preflight','环境检查'],['benchmark','执行任务'],['analyzing','Codex 分析'],['evaluating','对照验证'],['finished','保留结果']];
  $('pipeline').innerHTML=phases.map(([key,label],i)=>`<li class="${s.phase===key?'active':''}" ${s.phase===key?'aria-current="step"':''}><em>0${i+1}</em>${label}</li>`).join('');
  $('benchmark-label').textContent=s.config.suite==='public-web'?'Books to Scrape · 独立精确判分':'WebArena · 官方判分';
  $('benchmark-scope').textContent=s.config.suite==='public-web'?'借鉴 WebArena 的任务类型，不是官方成绩。运行前后校验公开数据指纹；判分抓取开销与 agent 耗时分开记录。':'按跨页读取与汇总需求选取；尚未测定参考动作长度。结果仅代表这两个任务。';
  const tasks=s.tasks || pre?.selected_tasks || [];
  $('tasks').innerHTML=tasks.map(t=>`<article class="task-card"><b>${s.config.suite==='public-web'?'PUBLIC WEB · TYPE':'WEBARENA'} / ${esc(t.id)}</b><h3>${esc(t.name || taskNames[t.id] || `任务 ${t.id}`)}</h3><p>${esc(t.intent)}</p></article>`).join('') || '<p class="muted">任务清单尚未载入</p>';
  const open=[...document.querySelectorAll('.trial-detail[open]')].map(el=>el.dataset.trial);
  $('runs').innerHTML=s.trials.length?`<div class="table-scroll"><table><thead><tr><th>轮次 / 配置</th><th>独立判分</th><th>端到端耗时</th><th>tokens</th><th>选择</th></tr></thead><tbody>${s.trials.map(t=>`<tr><td><strong>${t.id===0?'基线':`候选 ${t.id}`}</strong><small>${esc(t.proposal?.change?.field || '初始配置')}</small></td><td>${t.status==='complete'?`${t.result?.passed_tasks ?? (t.result?.strict_success?1:0)} / ${t.result?.total_tasks || 1}`:esc(s.status!=='running'||!s.process_alive?'执行中断，结果不完整':t.progress.active_task?`正在执行 #${t.progress.active_task}`:'等待任务')}</td><td>${sec(t.elapsed_s)}</td><td>${usage(t.metrics)}</td><td>${badge(t.selection?.decision || (t.status==='running' && (s.status!=='running'||!s.process_alive)?'interrupted':t.status))}</td></tr>`).join('')}</tbody></table></div>${s.trials.map(t=>`<details class="trial-detail" data-trial="${t.id}" ${open.includes(String(t.id))?'open':''}><summary>第 ${t.id+1} 轮 · 逐任务成绩与 trace ${t.progress.active_task?` · ${s.status==='running'?'当前':'中断于'} #${t.progress.active_task}`:''}</summary><div class="table-scroll"><table><thead><tr><th>任务</th><th>状态</th><th>耗时</th><th>tokens / 调用</th><th>轨迹</th></tr></thead><tbody>${t.tasks.map(task=>`<tr><td>#${esc(task.id)}</td><td>${task.result?badge(task.result.strict_success?'success':'failed'):badge('running')}${task.inflight?`<small>${task.inflight} 次调用等待响应</small>`:''}</td><td>${sec(task.elapsed_s)}</td><td>${usage(task.metrics)}<small>${num(task.metrics.attempts)} 次调用</small>${Object.entries(task.components || {}).map(([name,m])=>`<small>${esc(name==='brain'?'DS 大脑':name==='jev'?'Jev 小脑':name)} · ${usage(m)} tokens · ${num(m.attempts)} 次 · ${sec(m.request_time_s)}</small>`).join('')}</td><td><a href="/?${new URLSearchParams({run:task.run_id})}">打开 trace ↗</a></td></tr>`).join('')}</tbody></table></div><p class="footnote">${esc(t.selection?.reason || '等待运行与判分')}</p>${t.tasks.filter(task=>task.result).map(task=>`<p class="footnote">#${esc(task.id)} · ${esc(task.result.reason || '')}${task.grade?.data_valid!==undefined?` · 数据校验 ${task.grade.data_valid?'通过':'失败'} · 独立抓取 ${sec(task.grade.oracle_elapsed_s)}`:''}</p>`).join('')}${t.findings.length?`<h3>观测发现</h3><ul>${t.findings.map(f=>`<li><strong>${esc(f.code)}</strong><p class="footnote">${esc(f.hypothesis)}</p></li>`).join('')}</ul>`:''}</details>`).join('')}`:'<div class="empty-inline">暂无运行结果。环境检查通过后，基线的两个任务会依次开始。</div>';
  const improvements=s.trials.filter(t=>t.proposal);
  $('improvements').innerHTML=improvements.length?improvements.map(t=>`<article class="improvement"><div class="section-label"><strong>候选 ${t.id}</strong>${badge(t.selection?.decision || (t.status==='running' && (s.status!=='running'||!s.process_alive)?'interrupted':t.status))}</div><div class="change">${esc(fields[t.proposal.change.field] || t.proposal.change.field)}: ${esc(t.proposal.change.before)} → ${esc(t.proposal.change.after)}</div><p>${esc(t.proposal.hypothesis)}</p><p class="muted">${esc(t.selection?.reason || '等待验证')}</p>${Number.isFinite(t.selection?.relative_improvement)?`<p>目标指标改善 ${(100*t.selection.relative_improvement).toFixed(1)}%</p>`:''}<p class="footnote">证据：${esc(t.proposal.evidence_codes.join('、') || '观测汇总')}</p></article>`).join(''):'<p class="muted">Codex 会在基线完成后读取观测，提出一次可验证的配置变更。尚无改进提案。</p>';
  $('profile').innerHTML=Object.entries(s.best_profile || {}).map(([key,value])=>`<div><dt>${esc(fields[key] || key)}</dt><dd>${esc(value)}</dd></div>`).join('');
  $('profile-note').textContent=s.best_validated?'已通过任务判分；小规模对照不代表整体 benchmark 收益。':'当前为初始配置，尚未通过本次任务验证。';
  $('research-usage').textContent=`Codex 调用 ${s.research_usage.codex_invocations} 次 · ${usage(s.research_usage)} tokens · ${sec(s.research_usage.request_time_s)}`;
  const activeTrial=s.trials.find(t=>t.id===s.active_trial);
  const preview=activeTrial?.tasks.find(t=>t.id===activeTrial.progress.active_task && t.has_preview) || s.trials.flatMap(t=>t.tasks).filter(t=>t.has_preview).at(-1);
  $('preview').hidden=!preview;
  if(preview) { $('preview-image').src=`/api/image?${new URLSearchParams({id:preview.run_id,frame:'live',v:Math.floor(Date.now()/2000)})}`; $('trace-link').href=`/?${new URLSearchParams({run:preview.run_id})}`; $('preview-note').textContent=preview.result?'任务已结束 · 最后记录画面':'运行中的浏览器画面；截图不发送给模型。'; }
  $('raw').textContent=JSON.stringify({status:s.status,phase:s.phase,stop_reason:s.stop_reason,config:s.config,limits:s.limits,budget:s.budget,preflight:s.preflight,next_proposal:s.next_proposal},null,2);
}
async function launch(mode='new') {
  if(submitting) return; submitting=true; $('start').disabled=true; $('retry').disabled=true; error();
  $('request-status').textContent=mode==='new'?'正在启动研究，首先检查环境…':'正在重新检查环境…';
  try {
    const result=await get('/api/research',{method:'POST',headers:{'Content-Type':'application/json','X-Demo-Token':document.querySelector('meta[name="demo-token"]').content},body:JSON.stringify({mode,id:selected})});
    pending=result.id; $('request-status').textContent=`已提交研究：${result.id}`; await refresh();
  } catch(e) {error(e.message);$('request-status').textContent='';} finally {submitting=false;$('start').disabled=false;}
}
$('studies').addEventListener('click',e=>{const button=e.target.closest('[data-study]');if(button){selected=button.dataset.study;refresh();}});
$('start').addEventListener('click',()=>launch());$('retry').addEventListener('click',()=>launch('resume'));$('refresh').addEventListener('click',refresh);
refresh();setInterval(refresh,2000);
