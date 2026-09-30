/* One score vocabulary for LongSeq and original Ultrafast traces. */
export function longseqBenchmark(report, manifest) {
  if (manifest?.suite !== 'saas-bench') return null;
  const grade=report?.grade || {}, result=report?.result || {};
  return {
    suite:'saas-bench', task_id:manifest.task_id,
    status:!report ? 'pending' : grade.data_valid===true ? 'graded' : 'invalid',
    strict_success:grade.data_valid===true ? result.strict_success : null,
    score:grade.score, earned:grade.earned, total:grade.total,
    checks:grade.checks || [], verifier_errors:grade.verifier_errors || [],
    agent_status:result.status, reason:result.reason || '',
  };
}

const agentLabels={success:'完成',failed:'失败',budget_exhausted:'预算耗尽',
  needs_attention:'需要处理',environment_check:'环境检查',blocked:'阻塞',
  done:'自报完成',error:'运行错误',timeout:'超时',interrupted:'中断'};
const fraction=(earned,total)=>Number.isFinite(earned)&&Number.isFinite(total)&&total>0
  ? `${earned} / ${total}` : '—';

export function renderBenchmark(root, benchmark) {
  let panel=root.getElementById('benchmark-result');
  if(!benchmark){panel?.remove();return;}
  if(!panel){
    panel=document.createElement('section');
    panel.id='benchmark-result';panel.className='benchmark-result';
    panel.setAttribute('aria-label','SaaS-Bench 官方判分');
    panel.innerHTML=`<div class="benchmark-heading"><div><p class="benchmark-eyebrow">SAAS-BENCH · OFFICIAL VERIFIER</p><h2 id="benchmark-title"></h2></div><span id="benchmark-badge" class="benchmark-badge"></span></div>
      <div class="benchmark-grid"><div><span>严格成功</span><strong id="benchmark-success"></strong><small id="benchmark-validity"></small></div><div><span>官方部分得分</span><strong id="benchmark-score"></strong><small id="benchmark-percent"></small></div><div><span>Agent 状态</span><strong id="benchmark-agent"></strong><small id="benchmark-reason"></small></div></div>
      <p id="benchmark-note" class="benchmark-note"></p>
      <details id="benchmark-checks-wrap"><summary id="benchmark-checks-title"></summary><ol id="benchmark-checks"></ol></details>`;
    root.querySelector('.intro')?.after(panel);
  }
  const $=id=>root.getElementById(id);
  const valid=benchmark.status==='graded', invalid=benchmark.status==='invalid';
  panel.dataset.status=benchmark.status;
  const verdict=valid ? benchmark.strict_success===true?'通过':benchmark.strict_success===false?'未通过':'未判分'
    : invalid?'评分无效':'等待判分';
  $('benchmark-title').textContent=`SaaS-Bench · ${benchmark.task_id || '任务'}`;
  $('benchmark-badge').textContent=verdict;
  $('benchmark-badge').className=`benchmark-badge ${valid&&benchmark.strict_success===true?'passed':invalid?'invalid':valid?'failed':'pending'}`;
  $('benchmark-success').textContent=verdict;
  $('benchmark-validity').textContent=valid?'官方检查有效':invalid?'评分器检查出错，不计入成绩':'等待官方 verify.py';
  $('benchmark-score').textContent=valid?fraction(benchmark.earned,benchmark.total):invalid?'不计成绩':'—';
  $('benchmark-percent').textContent=valid&&Number.isFinite(benchmark.score)
    ? `${(benchmark.score*100).toFixed(1)}% · 官方原始得分`
    : invalid?`原始输出 ${fraction(benchmark.earned,benchmark.total)}`:'任务结束后显示';
  $('benchmark-agent').textContent=agentLabels[benchmark.agent_status] || benchmark.agent_status || '—';
  $('benchmark-reason').textContent=benchmark.reason?.startsWith('Original Agent:')
    ? `原版 Agent ${agentLabels[benchmark.agent_status] || benchmark.agent_status || '已停止'}；${invalid?'官方评分器检查异常':'官方任务未通过'}`
    : benchmark.reason || 'Agent 状态与官方判分分别记录';
  $('benchmark-note').textContent=invalid
    ? `官方评分器有 ${benchmark.verifier_errors?.length || 0} 项检查异常；原始分数仅供排查，不能表示模型完成率。`
    :valid?'部分得分可能来自镜像初始数据；只有“严格成功”通过才算任务完成。'
    :'当前尚无可用的官方结果。';
  const checks=Array.isArray(benchmark.checks)?benchmark.checks:[];
  $('benchmark-checks-wrap').hidden=!checks.length;
  $('benchmark-checks-title').textContent=`官方检查项 · ${checks.length} 项`;
  const list=$('benchmark-checks');list.replaceChildren();
  for(const check of checks){
    const item=document.createElement('li');
    const exception=/^\s*(exception|error)\s*:/i.test(check.detail || '');
    item.className=exception?'check-error':check.passed?'check-pass':'check-fail';
    const heading=document.createElement('strong');
    heading.textContent=`${exception?'检查异常':check.passed?'通过':'未通过'} · ${check.label || '检查'}${Number.isFinite(check.weight)?` (${check.weight} 分)`:''}`;
    item.append(heading);
    if(check.detail){const detail=document.createElement('small');detail.textContent=check.detail;item.append(detail);}
    list.append(item);
  }
}
