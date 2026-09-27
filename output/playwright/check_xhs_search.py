import asyncio
import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from jev_browser.chrome import ChromeBackend
from jev_browser.dynamic import generate_dynamic
from jev_browser.observability import Observer, write_json
from jev_browser.protocol import Operation, Task, now

async def main():
    output = Path('runs/xhs-search-dom-check-02')
    output.mkdir(parents=True, exist_ok=False)
    task = Task(id='xhs-search-dom-check', objective='诊断验证：识别搜索图标并搜索 Spark Labs；不验证模型决策或结果内容。', control_mode='dynamic', start_url='https://www.xiaohongshu.com/explore', allowed_origins=['https://www.xiaohongshu.com'])
    write_json(output / 'manifest.json', {'started_at': now(), 'policy':'diagnostic', 'backend':'chrome', 'task_id':task.id, 'benchmark':'bound-action-check-no-model'})
    write_json(output / 'task.json', task.model_dump(mode='json'))
    observer = Observer(output)
    cycle = 0
    actions = 0
    started = time.monotonic()
    def event(kind, **fields):
        observer.append('trajectory.jsonl', {'time':now(), 'kind':kind, 'cycle':cycle, **fields})
    async def observe(browser):
        nonlocal cycle
        cycle += 1
        obs = await browser.observe()
        event('observation', observation=obs.model_dump(mode='json'))
        return obs
    result = {'status':'failed', 'reason':'verification not completed', 'strict_success':None}
    try:
        async with ChromeBackend(task, output=output, live_preview=True) as browser:
            browser.observer = observer
            await browser.page.locator('textarea:visible,input:visible').first.wait_for(timeout=12000)
            for op in [Operation.FILL, Operation.CLICK]:
                for attempt in range(3):
                    obs = await observe(browser)
                    candidates = generate_dynamic(obs, task, limit=250)
                    targets = [e for e in obs.elements if (e.editable if op == Operation.FILL else e.name in {'搜索', 'search (icon control)'})]
                    if len(targets) != 1:
                        raise AssertionError(f'ambiguous {op}: {[(e.id,e.name) for e in targets]}')
                    target = targets[0]
                    action = next(a for a in candidates if a.operation == op and a.element_ref == target.id)
                    if op == Operation.FILL:
                        action.bound_value = 'Spark Labs'
                    else:
                        assert any(e.editable and e.value == 'Spark Labs' for e in obs.elements)
                    receipt = await browser.execute(action)
                    actions += 1
                    event('action', action=action.model_dump(mode='json'), receipt=receipt.model_dump(mode='json'))
                    print(op, target.name, receipt.status, flush=True)
                    if receipt.status != 'stale':
                        assert receipt.status == 'ok', receipt.detail
                        break
                else:
                    raise AssertionError('target kept becoming stale')
            await browser.page.wait_for_url('**/search_result?**', timeout=12000)
            await browser.page.wait_for_timeout(700)
            obs = await observe(browser)
            assert parse_qs(urlsplit(obs.url).query).get('keyword') == ['Spark Labs'], obs.url
            await asyncio.sleep(1.2)
            result.update(status='success', reason='绑定搜索图标点击通过；观察到 Spark Labs 搜索结果 URL。仅验证识别与提交，不是完整模型任务验收。')
            write_json(output / 'search-check.json', {'url':obs.url, 'query':'Spark Labs', 'scope':'DOM recognition + bound click + result URL; no model or result-content grading'})
    except Exception as exc:
        result['reason'] = f'{type(exc).__name__}: {exc}'
    finally:
        result.update(actions=actions, cycles=cycle, elapsed_s=time.monotonic()-started)
        write_json(output / 'result.json', result)
        event('result', result=result)
        print(json.dumps(result, ensure_ascii=False))

asyncio.run(main())
