"""Offline checks for historical instruction/action alignment; no browser needed."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_replay_preserves_action_instruction_when_seeking_backwards():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for replay JavaScript checks")
    module = Path(__file__).resolve().parents[1] / "src/jev_browser/static/app.js"
    script = r"""
import assert from 'node:assert/strict';
const {replayContext, nextReplayStep}=await import(process.argv[2]);
const events=[
  {kind:'observation', index:0},
  {kind:'feedback', index:1, feedback:{next_goal:'Fill the date',
    inputs:[{name:'Date',value:'2026-06-30'}]}},
  {kind:'decision', index:2, decision:{choice:'fill'}},
  {kind:'input_binding', index:3, action:{bound_value:'2026-06-30'}},
  {kind:'action', index:4, action:{operation:'fill',bound_value:'2026-06-30'},
    receipt:{status:'ok'}},
  {kind:'observation', index:5},
  {kind:'feedback', index:6, feedback:{next_goal:'Save',inputs:[]}},
  {kind:'action_started', index:7, action:{operation:'click'}},
  {kind:'action', index:8, action:{operation:'click'},receipt:{status:'rejected'}},
  {kind:'invalid_feedback', index:9},
  {kind:'result', index:10},
];
let context=replayContext(events,0,'Original task');
assert.equal(context.goal,'Original task');
assert.equal(context.action,null);
context=replayContext(events,3,'Original task');
assert.equal(context.action,null,'Candidates and input binding are not executions');
assert.equal(context.goal,'Fill the date');
assert.equal(context.inputs[0].value,'2026-06-30');
context=replayContext(events,7,'Original task');
assert.equal(context.goal,'Save');
assert.equal(context.action.index,4,'An action started is not a completed action');
assert.equal(context.actionGoal,'Fill the date','Never relabel an older action with a new goal');
assert.notEqual(context.instruction,context.actionInstruction);
context=replayContext(events,8,'Original task');
assert.equal(context.action.receipt.status,'rejected','Keep failed executions visible');
assert.equal(context.actionGoal,'Save');
context=replayContext(events,4,'Original task');
assert.equal(context.goal,'Fill the date','Seeking back must discard later feedback');
assert.equal(context.action.index,4);
assert.equal(context.instruction,context.actionInstruction);
assert.deepEqual(replayContext([],0,'New run').inputs,[]);
assert.equal(replayContext([],0,'New run').action,null,'Switching runs must clear history');
const legacy=[{kind:'plan',plan:{subtasks:[{id:'s1',objective:'Read price'}]}},
  {kind:'action',action:{operation:'extract'}}];
assert.equal(replayContext(legacy,1,'Original').actionGoal,'s1: Read price');
let cursor=0, stops=[];
while(cursor<events.length-1) {cursor=nextReplayStep(events,cursor);stops.push(cursor);}
assert.deepEqual(stops,[1,4,6,8,9,10],'Step playback must retain every instruction and execution');
assert.equal(nextReplayStep(events,10),10);
assert.equal(nextReplayStep([{kind:'observation'},{kind:'observation'}],0),1);
"""
    completed = subprocess.run(
        [node, "--input-type=module", "-",
         module.as_uri()], input=script, text=True, capture_output=True, timeout=20,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_scrubbing_coalesces_and_stale_image_responses_cannot_publish():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for replay JavaScript checks")
    module = Path(__file__).resolve().parents[1] / "src/jev_browser/static/app.js"
    script = r"""
import assert from 'node:assert/strict';
const {replayScrubber,replayImages}=await import(process.argv[2]);
let callback=null, renders=[];
const scrub=replayScrubber(value=>renders.push(value), fn=>{callback=fn;return 1;}, ()=>{callback=null;});
for(let i=0;i<100;i++) scrub.queue(i);
callback(); assert.deepEqual(renders,[99]);
scrub.queue(50); scrub.flush(); assert.deepEqual(renders,[99,50]);
scrub.queue(90); scrub.cancel(); assert.equal(callback,null,'A pending seek must not survive another navigation');
const tick=()=>new Promise(resolve=>setTimeout(resolve,5));
const loads=[], shown=[], failures=[], disposed=[];
const loader=replayImages({delay:0,capacity:2,
  load:(url,signal)=>new Promise((resolve,reject)=>loads.push({url,signal,resolve,reject})),
  display:(frame,metadata,url)=>shown.push({url,metadata}),
  failed:(error,url)=>failures.push(url)});
const frame=url=>({src:url,dispose:()=>disposed.push(url)});
loader.request('old',{event:1}); await tick();
loader.request('new',{event:99}); await tick();
assert.equal(loads[0].signal.aborted,true);
loads[1].resolve(frame('new')); await tick();
loads[0].resolve(frame('old')); await tick();
assert.deepEqual(shown,[{url:'new',metadata:{event:99}}],'Late images cannot relabel the current instruction');
assert.deepEqual(disposed,['old']);
loader.request('obsolete',{}); await tick();
loader.request('new',{event:100});
loads[2].reject(new Error('late failed image')); await tick();
assert.deepEqual(failures,[],'An old error cannot blank a newer cached frame');
assert.equal(loads.length,3,'Revisiting a cached frame must not request it again');
assert.equal(shown.at(-1).metadata.event,100);
loader.request('other-run',{}); await tick();
loader.reset(); loads[3].resolve(frame('other-run')); await tick();
assert.equal(shown.at(-1).url,'new','Run reset must discard in-flight screenshots');
assert.ok(disposed.includes('new') && disposed.includes('other-run'));
"""
    completed = subprocess.run(
        [node, "--input-type=module", "-", module.as_uri()], input=script,
        text=True, capture_output=True, timeout=20,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
