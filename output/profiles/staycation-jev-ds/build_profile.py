"""Reconstruct this recorded run without rerunning models or changing policies."""
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
RUN = ROOT / 'runs/staycation-ds-vs-jev-20260927/jev-ds-verified'
BASELINE = RUN.parent / 'pure-ds'

def read(name):
    return [json.loads(line) for line in (RUN / name).read_text().splitlines()]

def stamp(s):
    return datetime.fromisoformat(s).timestamp()

report = json.loads((RUN / 'report.json').read_text())
baseline = json.loads((BASELINE / 'report.json').read_text())
spans, events = read('spans.jsonl'), read('trajectory.jsonl')
calls = sorted(report['model_calls'], key=lambda c: c['started_at'])
epoch = stamp(report['manifest']['started_at'])
elapsed = report['end_to_end_s']
loop = report['result']['elapsed_s']
roots = [s for s in spans if not s.get('parent_id')]
root_totals = defaultdict(float)
for s in roots:
    root_totals[s['name']] += s['duration_s']
# Verify that the spans used in additive totals are non-overlapping.
ordered = sorted(roots, key=lambda s: s['started_at'])
for a,b in zip(ordered, ordered[1:]):
    assert stamp(a['started_at']) + a['duration_s'] <= stamp(b['started_at']) + .005
request_by_kind = defaultdict(float)
for c in calls:
    request_by_kind[c['kind']] += c['latency_s']
request_total = sum(request_by_kind.values())
browser = root_totals['browser.observe'] + root_totals['browser.execute']
other_loop = loop - request_total - browser
assert other_loop >= 0
breakdown = [
    ('loop外：准备与收尾，未细分', elapsed-loop),
    ('DS 初始指导', next(c['latency_s'] for c in calls if c['kind']=='dynamic_feedback')),
    ('DS 输入助手', request_by_kind['dynamic_input']),
    ('DS 搜索结果读回指导', next(c['latency_s'] for c in calls if c['kind']=='dynamic_feedback' and c['cycle']==6)),
    ('DS 最终复核与总结', request_by_kind['dynamic_finish']),
    ('Jev 候选决策', request_by_kind['jev']),
    ('DOM 观察与浏览器执行', browser),
    ('loop内其余框架开销', other_loop),
]
assert abs(sum(v for _,v in breakdown)-elapsed)<1e-6
first_span = min(stamp(s['started_at']) for s in spans)-epoch
last_event = max(stamp(e['time']) for e in events)-epoch
jev = [c for c in calls if c['kind']=='jev']
first_tls = sum(n['duration_s'] for n in jev[0]['network_phases'] if n['phase']=='start_tls')
profile = {
    'source': str(RUN), 'run_id': report['manifest']['run_id'],
    'scope': 'Recorded wall-clock request/span profiling, not CPU sampling; no rerun.',
    'end_to_end_s': elapsed, 'controller_loop_s': loop,
    'outside_loop_s': elapsed-loop,
    'manifest_to_first_observe_s_approx': first_span,
    'manifest_to_last_event_s_approx': last_event,
    'tail_to_reported_end_s_approx': elapsed-last_event,
    'timing_limit': 'Manifest uses wall clock after the monotonic run timer starts. Start and tail splits are approximate; outside_loop is exact subtraction of recorded durations.',
    'additive_breakdown': [{'name':k,'seconds':v,'percent_e2e':100*v/elapsed} for k,v in breakdown],
    'root_spans_s': dict(root_totals),
    'model_requests_s':request_total,
    'jev':{'calls':len(jev),'first_call_s':jev[0]['latency_s'],'first_tls_s':first_tls,'warm_calls':len(jev)-1,'warm_mean_s':mean(c['latency_s'] for c in jev[1:]),'all_median_s':median(c['latency_s'] for c in jev)},
    'baseline_comparison': {'pure_ds_e2e_s':baseline['end_to_end_s'],'pure_ds_loop_s':baseline['result']['elapsed_s'],'pure_ds_outside_loop_s':baseline['end_to_end_s']-baseline['result']['elapsed_s'],'loop_reduction_s':baseline['result']['elapsed_s']-loop,'outside_loop_increase_s':elapsed-loop-(baseline['end_to_end_s']-baseline['result']['elapsed_s'])},
    'model_calls': [{**{k:c.get(k) for k in ['kind','cycle','trigger','latency_s','input_tokens','output_tokens','network_phases','raw_usage','payload_sizes']},'start_s':stamp(c['started_at'])-epoch} for c in calls],
}
(OUT/'profile.json').write_text(json.dumps(profile,ensure_ascii=False,indent=2)+'\n')
trace=[]
for tid,name in [(1,'Controller root spans'),(2,'Browser observation detail'),(3,'Model HTTP requests'),(4,'Recorded network phases'),(5,'Trajectory markers')]:
    trace.append({'ph':'M','pid':1,'tid':tid,'name':'thread_name','args':{'name':name}})
def span_event(name,start,duration,tid,args=None,cat='profile'):
    trace.append({'ph':'X','pid':1,'tid':tid,'name':name,'cat':cat,'ts':round(start*1e6),'dur':round(duration*1e6),'args':args or {}})
span_event('Before first observation (unattributed)',0,first_span,1,{'inferred':True,'known_scope':'setup, CDP attachment, navigation; no detailed historical spans'})
for s in spans:
    span_event(s['name'],stamp(s['started_at'])-epoch,s['duration_s'],2 if s.get('parent_id') else 1,{'cycle':s.get('cycle'),'status':s.get('status')})
for c in calls:
    start=stamp(c['started_at'])-epoch
    span_event(c['kind'],start,c['latency_s'],3,{k:c.get(k) for k in ['cycle','input_tokens','output_tokens','status','cost_usd']},'model')
    # Network phases have durations but no individual timestamps: do not fabricate positions.
for e in events:
    if e['kind'] not in {'observation','decision'}:
        trace.append({'ph':'i','s':'t','pid':1,'tid':5,'name':e['kind'],'ts':round((stamp(e['time'])-epoch)*1e6),'args':{'cycle':e.get('cycle'),'reason':e.get('reason'),'receipt_status':e.get('receipt',{}).get('status')}})
(OUT/'trace.json').write_text(json.dumps({'traceEvents':trace,'displayTimeUnit':'ms','metadata':{'source':str(RUN),'note':'Nested spans and HTTP calls overlap. Sum only exclusive categories in profile.json. Network subphase timestamps were not recorded.'}},ensure_ascii=False,indent=2)+'\n')
print(json.dumps({k:v for k,v in profile.items() if k not in ['model_calls']},ensure_ascii=False,indent=2))
