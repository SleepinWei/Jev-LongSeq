"""Shared run navigation, deep links, and mobile drawer without model calls."""
import threading
import re

from playwright.async_api import async_playwright, expect

from jev_browser.inspector import make_server
from jev_browser.observability import write_json


def fixture(root):
    for name in ("run-one", "run-two"):
        path = root / name
        path.mkdir()
        write_json(path / "manifest.json", {"task_id": name, "started_at": "2026-09-29T08:00:00Z", "policy": "jev"})
        write_json(path / "task.json", {"objective": f"Goal for {name}"})
        write_json(path / "report.json", {"result": {"status": "failed", "actions": 0,
            "strict_success": False, "reason": "Fixture"}, "grade": {}, "model_calls": []})
    native = root / "ultrafast" / "native-one"
    native.mkdir(parents=True)
    write_json(native / "meta.json", {"id": "native-one", "prompt": "Read a page",
        "url": "https://example.test/", "started_at": "2026-09-29T08:05:00Z"})
    write_json(native / "state.json", {"status": "blocked", "history": [], "decisions": [],
        "page": {"url": "https://example.test/", "title": "Example", "text": "Example", "actions": []},
        "elements": [], "overlays": [], "elapsed_ms": 1})
    study = root / "study-one"
    trial = study / "trial-000" / "task-50"
    trial.mkdir(parents=True)
    write_json(study / "study.json", {"status": "stopped", "phase": "finished",
        "config": {"suite": "public-web", "max_seconds": 300, "codex_model": "fixture-model",
                   "codex_effort": "high"}, "incumbent": 0, "best_profile": {},
        "trials": [{"id": 0, "directory": "trial-000", "status": "complete", "profile": {},
                    "result": {"strict_success": False, "passed_tasks": 0, "total_tasks": 1}}]})
    write_json(trial / "manifest.json", {"task_id": 50, "started_at": "2026-09-29T08:10:00Z", "policy": "jev"})
    write_json(trial / "task.json", {"objective": "Research task"})
    write_json(trial / "report.json", {"result": {"status": "failed", "actions": 0,
        "strict_success": False, "reason": "Fixture"}, "grade": {}, "model_calls": []})


async def test_shared_sidebar_switches_runs_and_keeps_trace_deep_links(tmp_path):
    fixture(tmp_path)
    server = make_server(tmp_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                page = await browser.new_page(viewport={"width": 1440, "height": 900})
                errors, documents = [], []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: documents.append(request.url)
                        if request.resource_type == 'document' else None)
                await page.goto(f'http://127.0.0.1:{server.server_port}/?run=run-one')
                sidebar = page.locator('#record-sidebar')
                await expect(sidebar.get_by_role('link', name='run-two', exact=False)).to_be_visible()
                await page.locator('#record-search').fill('run-one')
                await expect(sidebar.get_by_role('link', name='run-two', exact=False)).to_have_count(0)
                await page.locator('#record-search').fill('')
                await sidebar.get_by_role('link', name='run-two', exact=False).click()
                await expect(page).to_have_url(re.compile(r'/\?run=run-two$'))
                await expect(sidebar.get_by_role('link', name='run-two', exact=False)).to_have_attribute('aria-current', 'page')
                assert (await page.locator('.studio-view[data-view=longseq] .workspace').bounding_box())['y'] < 120
                await page.locator('#records-toggle').click()
                await expect(page.locator('#records-toggle')).to_have_attribute('aria-expanded', 'false')
                await page.reload()
                await expect(page.locator('#records-toggle')).to_have_attribute('aria-expanded', 'false')
                await page.locator('#records-toggle').click()
                await page.get_by_role('navigation', name='Studio 视图').get_by_role('link', name='Ultrafast').click()
                await expect(sidebar.get_by_role('link', name='Read a page', exact=False)).to_be_visible()
                await sidebar.get_by_role('link', name='Read a page', exact=False).click()
                await expect(page).to_have_url(re.compile(r'/ultrafast\?run=native-one$'))
                await page.get_by_role('navigation', name='Studio 视图').get_by_role('link', name='Autoresearch').click()
                await expect(sidebar.get_by_role('link', name='公共网页', exact=False)).to_be_visible()
                await sidebar.get_by_role('link', name='公共网页', exact=False).click()
                await expect(sidebar.get_by_role('link', name='基线 · 50', exact=False)).to_be_visible()
                await sidebar.get_by_role('link', name='基线 · 50', exact=False).click()
                await expect(page).to_have_url(re.compile(r'/\?run=study-one%2Ftrial-000%2Ftask-50$'))
                assert len(documents) == 2  # initial page plus deliberate reload; navigation stays mounted
                assert not errors
                await page.set_viewport_size({"width": 375, "height": 850})
                await expect(page.locator('#records-toggle')).to_have_attribute('aria-expanded', 'false')
                await page.locator('#records-toggle').click()
                await expect(sidebar).to_be_visible()
                await page.keyboard.press('Escape')
                await expect(page.locator('#records-toggle')).to_have_attribute('aria-expanded', 'false')
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            finally:
                await browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
