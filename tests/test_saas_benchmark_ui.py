"""Benchmark verdicts on native and LongSeq traces are distinct from agent status."""
import re
import threading

from playwright.async_api import async_playwright, expect

from jev_browser import ultrafast
from jev_browser.inspector import Store, make_server
from jev_browser.observability import write_json


def trace(root, task, *, valid):
    run_id=f"native-{task}"
    native=root / "ultrafast" / run_id
    native.mkdir(parents=True)
    output=root / "pilot" / f"saas-bench-{task}"
    output.mkdir(parents=True)
    ultrafast.write_json(native / "meta.json", {"id":run_id,"prompt":"A benchmark task",
        "url":"https://example.test/","started_at":"2026-09-29T08:00:00Z",
        "benchmark_output":str(output)})
    ultrafast.write_json(native / "state.json", {"status":"error" if valid else "blocked","history":[],"decisions":[],
        "page":{"url":"https://example.test/","title":"Example","text":"Example","actions":[]},
        "elements":[],"overlays":[],"elapsed_ms":100})
    report={"manifest":{"suite":"saas-bench","task_id":task,"original_trace_id":run_id},
        "result":{"status":"success" if valid else "failed", "strict_success":False if valid else None,
                  "reason":"Official verifier rejected completion" if valid else "Verifier SQL failure"},
        "grade":{"data_valid":valid,"score":.1 if valid else 0,"earned":2 if valid else 0,
                 "total":20 if valid else 15,
                 "checks":[{"label":"<img src=x onerror=alert(1)>","weight":2,
                            "passed":valid,"detail":"fixture present" if valid else "exception: Unknown column"}],
                 "verifier_errors":[] if valid else [{"label":"DB", "detail":"exception: Unknown column"}]}}
    write_json(output / "report.json",report)
    return run_id, output, report


def test_native_projection_validates_report_identity_and_path(tmp_path):
    good, _, _=trace(tmp_path,"business_023",valid=True)
    bad, output, report=trace(tmp_path,"business_031",valid=False)
    store=Store(tmp_path)
    assert ultrafast.data(store,good)["benchmark"]["earned"]==2
    assert ultrafast.data(store,bad)["benchmark"]["status"]=="invalid"
    assert ultrafast.data(store,bad)["benchmark"]["strict_success"] is None
    assert ultrafast.data(store,bad)["benchmark"]["agent_status"]=="blocked"
    assert next(row for row in ultrafast.listing(store) if row["id"]==bad)["benchmark"]["status"]=="invalid"
    report["manifest"]["original_trace_id"]="another-run"
    write_json(output / "report.json",report)
    assert ultrafast.data(store,bad)["benchmark"] is None
    native=tmp_path / "ultrafast" / bad
    meta=ultrafast.read_json(native / "meta.json")
    meta["benchmark_output"]=str(tmp_path.parent / "outside")
    ultrafast.write_json(native / "meta.json",meta)
    assert ultrafast.data(store,bad)["benchmark"] is None


async def test_benchmark_ui_shows_valid_partial_score_and_invalid_verifier(tmp_path):
    good, _, _=trace(tmp_path,"business_023",valid=True)
    bad, _, _=trace(tmp_path,"business_031",valid=False)
    longseq=tmp_path / "longseq-task"
    longseq.mkdir()
    write_json(longseq / "manifest.json",{"suite":"saas-bench","task_id":"business_023",
        "started_at":"2026-09-29T08:00:00Z","policy":"jev"})
    write_json(longseq / "task.json",{"objective":"Benchmark fixture"})
    write_json(longseq / "report.json",{"result":{"status":"failed","strict_success":False,
        "actions":0,"reason":"Official verifier rejected completion"},
        "grade":{"data_valid":True,"score":.1,"earned":2,"total":20,"checks":[]},
        "model_calls":[]})
    server=make_server(tmp_path,0)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        async with async_playwright() as pw:
            browser=await pw.chromium.launch()
            try:
                page=await browser.new_page(viewport={"width":1440,"height":900})
                errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                await page.goto(f'http://127.0.0.1:{server.server_port}/ultrafast?run={good}')
                native=page.locator('.studio-view[data-view=ultrafast]')
                await expect(native.locator('#benchmark-success')).to_have_text('未通过')
                await expect(native.locator('#benchmark-score')).to_have_text('2 / 20')
                await expect(page.locator('#record-list')).to_contain_text('官方未通过 · 2/20')
                await native.locator('#benchmark-checks-wrap summary').click()
                assert await native.locator('#benchmark-checks img').count()==0
                await page.locator('#record-list').get_by_role('link',name=re.compile(bad)).click()
                await expect(native.locator('#benchmark-success')).to_have_text('评分无效')
                await expect(native.locator('#benchmark-score')).to_have_text('不计成绩')
                await expect(native.locator('#benchmark-agent')).to_have_text('阻塞')
                await expect(native.locator('#benchmark-percent')).to_have_text('原始输出 0 / 15')
                await expect(page.locator('#record-list')).to_contain_text('评分无效')
                await page.goto(f'http://127.0.0.1:{server.server_port}/?run=longseq-task')
                long=page.locator('.studio-view[data-view=longseq]')
                await expect(long.locator('#benchmark-success')).to_have_text('未通过')
                await expect(long.locator('#benchmark-score')).to_have_text('2 / 20')
                assert not errors
            finally:
                await browser.close()
    finally:
        server.shutdown();server.server_close();thread.join()
