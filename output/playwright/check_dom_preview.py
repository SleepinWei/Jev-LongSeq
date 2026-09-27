import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright
from jev_browser.browser import PlaywrightBackend
from jev_browser.observability import write_json
from jev_browser.protocol import Task, now

async def main():
    out = Path('runs/dom-preview-ui-check')
    out.mkdir(exist_ok=True)
    task = Task(id='dom-preview-ui-check', objective='DOM 标注界面验证：本地搜索控件样例，不调用模型。', control_mode='dynamic', sandbox=True)
    write_json(out/'manifest.json', {'task_id':task.id, 'started_at':now(), 'policy':'diagnostic', 'backend':'playwright'})
    write_json(out/'task.json', task.model_dump(mode='json'))
    async with PlaywrightBackend(task, output=out, live_preview=False) as b:
        await b.load_html('''<style>body{font:22px system-ui;padding:48px;background:#faf9f5}textarea{display:block;width:700px;height:80px;margin:24px 0} .search{display:inline-block;cursor:pointer;padding:12px;border:1px solid #333;background:#fff}p{color:#666}</style><h1>搜索控件识别验证</h1><p>输入框和自定义搜索按钮应带有对应的元素编号。</p><textarea aria-label="搜索小红书">Spark Labs</textarea><div class="search"><img alt="搜索" width=30 height=30></div>''')
        obs = await b.observe()
        (out/'trajectory.jsonl').write_text(json.dumps({'kind':'observation','time':now(),'observation':obs.model_dump(mode='json')},ensure_ascii=False)+'\n')
        image, _, meta = await b.capture_preview()
        assert len(meta['overlays']) == 2
        (out/'preview').mkdir(exist_ok=True)
        (out/'preview/000000.jpg').write_bytes(image)
        import time
        (out/'frames.jsonl').write_text(json.dumps({**meta,'time':time.time(),'resource':'preview/000000.jpg'})+'\n')
    result={'status':'success','reason':'本地 DOM 标注样例','actions':0,'strict_success':None}
    write_json(out/'result.json',result)
    with (out/'trajectory.jsonl').open('a') as f:
        f.write(json.dumps({'kind':'result','time':now(),'result':result})+'\n')
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            page = await browser.new_page(viewport={'width':1500,'height':1200})
            errors=[]
            page.on('pageerror',lambda e: errors.append(str(e)))
            await page.goto('http://127.0.0.1:8767/?run=dom-preview-ui-check')
            await page.locator('#targets .target').first.wait_for(timeout=15000)
            assert await page.locator('#targets .target').count() == 2
            assert await page.locator('#targets .editable').count() == 1
            bounds = await page.locator('#targets .editable').bounding_box()
            viewport = await page.locator('.viewport').bounding_box()
            assert abs((bounds['x']-viewport['x'])/viewport['width'] - 56/1280) < .005
            assert abs(bounds['width']/viewport['width'] - 700/1280) < .005
            await page.locator('.workspace').screenshot(path='output/playwright/dom-preview-ui.png')
            await page.locator('#overlays').uncheck()
            assert await page.locator('#targets').is_hidden()
            await page.locator('#overlays').check()
            assert await page.locator('#targets').is_visible()
            await page.locator('#run').select_option('ui-prompt-1790501451315254000/task')
            await page.wait_for_function("document.querySelector('#dom-status').textContent.includes('未记录')")
            assert await page.locator('#targets').is_hidden()
            assert not errors, errors
            print('UI checks passed: correct IDs, input styling, toggle, historical fallback, no JS errors')
        finally:
            await browser.close()

asyncio.run(main())
