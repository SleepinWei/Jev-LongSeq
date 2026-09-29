import re
import threading
from unittest.mock import Mock

import httpx
import pytest

from jev_browser.inspector import Store, make_server
from jev_browser.observability import Observer, write_json
from jev_browser.research_ui import launch_research, studies, study_data


def study(root):
    path = root / "study-one"
    path.mkdir()
    write_json(
        path / "study.json",
        {"status": "blocked", "phase": "preflight", "trials": [], "config": {"suite": "webarena"}},
    )
    return path


def test_research_projection_has_live_usage_and_no_path_escape(tmp_path):
    root = study(tmp_path)
    observer = Observer(root / "researcher")
    observer.model_call(
        {
            "kind": "research_analysis",
            "transport": "codex_cli",
            "input_tokens": 12,
            "output_tokens": 5,
            "cost_usd": None,
        }
    )
    store = Store(tmp_path)
    value = study_data(store, "study-one")
    assert value["research_usage"]["codex_invocations"] == 1
    assert value["research_usage"]["cost_usd"] is None
    assert studies(store)[0]["status"] == "blocked"
    with pytest.raises(ValueError):
        study_data(store, "../study-one")
    (root / "budget.json").symlink_to(tmp_path.parent / "outside.json")
    with pytest.raises(ValueError):
        study_data(store, "study-one")


def test_launch_is_bounded_and_keeps_ds_jev_separate_from_codex(tmp_path, monkeypatch):
    child = Mock()
    child.poll.return_value = None
    spawn = Mock(return_value=child)
    monkeypatch.setattr("jev_browser.research_ui.subprocess.Popen", spawn)
    store = Store(tmp_path)
    result = launch_research(store, {"mode": "new"})
    command = spawn.call_args.args[0]
    assert command[command.index("--brain") + 1] == "api"
    assert command[command.index("--task-ids") + 1 : command.index("--task-ids") + 3] == [
        "50",
        "332",
    ]
    assert command[command.index("--max-trials") + 1] == "2"
    assert command[command.index("--codex-effort") + 1] == "high"
    assert not (tmp_path / result["id"]).exists()  # runner creates an empty study directory
    with pytest.raises(RuntimeError):
        launch_research(store, {})


def test_research_http_routes_preserve_local_post_protection(tmp_path, monkeypatch):
    study(tmp_path)
    server = make_server(tmp_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(
        "jev_browser.research_ui.launch_research", lambda store, body: {"id": "test-only"}
    )
    try:
        with httpx.Client(base_url=base, trust_env=False) as client:
            page = client.get("/research")
            assert page.status_code == 200 and "Autoresearch" in page.text
            token = re.search(r'name="demo-token" content="([^"]+)"', page.text)[1]
            assert client.get("/research.js").status_code == 200
            assert client.get("/api/studies").json()[0]["id"] == "study-one"
            assert (
                client.get("/api/study", params={"id": "study-one"}).json()["status"] == "blocked"
            )
            assert client.post("/api/research", json={}).status_code == 403
            assert (
                client.post(
                    "/api/research",
                    json={},
                    headers={"X-Demo-Token": token, "Origin": "https://other.test"},
                ).status_code
                == 403
            )
            assert (
                client.post("/api/research", json={}, headers={"X-Demo-Token": token}).json()["id"]
                == "test-only"
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_source_snapshot_pins_study_code_outside_trial_output(tmp_path):
    from pathlib import Path

    from jev_browser.research_ui import frozen_source
    first = frozen_source(tmp_path)
    assert (first / 'jev_browser' / '__main__.py').exists()
    assert first == frozen_source(tmp_path)
    import jev_browser.research_ui as module
    assert (first / 'jev_browser' / 'research_ui.py').read_text() == Path(module.__file__).read_text()


@pytest.mark.parametrize("body", [{"max_trials": True}, {"max_trials": 7},
                                    {"max_trials": 1}, {"metric": "cost"}, {"metric": []}])
def test_invalid_study_controls_never_dispatch(tmp_path, monkeypatch, body):
    spawn = Mock()
    monkeypatch.setattr("jev_browser.research_ui.subprocess.Popen", spawn)
    with pytest.raises(ValueError):
        launch_research(Store(tmp_path), body)
    spawn.assert_not_called()


def test_multiround_study_shares_attempt_and_token_caps(tmp_path, monkeypatch):
    spawn = Mock()
    monkeypatch.setattr("jev_browser.research_ui.subprocess.Popen", spawn)
    launch_research(Store(tmp_path), {"max_trials": 4, "metric": "latency"})
    args = spawn.call_args.args[0]
    assert args[args.index("--max-trials")+1] == "4"
    assert args[args.index("--metric")+1] == "latency"
    assert args[args.index("--max-tokens")+1] == "600000"
    assert args[args.index("--max-model-attempts")+1] == "300"
    assert args[args.index("--study-seconds")+1] == "2700"


async def test_studio_tabs_preserve_drafts_and_research_without_document_navigation(tmp_path):
    from playwright.async_api import async_playwright, expect

    study(tmp_path)
    server = make_server(tmp_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                page = await browser.new_page(viewport={"width": 1400, "height": 1000})
                errors, documents = [], []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: documents.append(request.url)
                        if request.resource_type == 'document' else None)
                await page.goto(f'http://127.0.0.1:{server.server_port}/research?study=study-one')
                nav = page.get_by_role('navigation', name='Studio 视图')
                research = page.locator('.studio-view[data-view=research]')
                await research.locator('#max-trials').select_option('6')
                await research.locator('#study-id').filter(has_text='study-one').wait_for()
                await nav.get_by_role('link', name='LongSeq', exact=True).click()
                longseq = page.locator('.studio-view[data-view=longseq]')
                await longseq.locator('#prompt').fill('保留这个尚未提交的 prompt')
                await nav.get_by_role('link', name='Ultrafast', exact=True).click()
                original = page.locator('.studio-view[data-view=ultrafast]')
                await original.locator('#prompt').fill('另一份独立草稿')
                await nav.get_by_role('link', name='Autoresearch').click()
                await expect(research).to_be_visible()
                await expect(research.locator('#max-trials')).to_have_value('6')
                assert 'study=study-one' in page.url
                await page.go_back()
                await expect(original).to_be_visible()
                await expect(original.locator('#prompt')).to_have_value('另一份独立草稿')
                await nav.get_by_role('link', name='LongSeq', exact=True).click()
                await expect(longseq.locator('#prompt')).to_have_value('保留这个尚未提交的 prompt')
                await expect(nav.get_by_role('link', name='LongSeq', exact=True)).to_have_attribute('aria-current', 'page')
                assert len(documents) == 1  # Switching views never reloads the document.
                assert not errors
                await page.set_viewport_size({"width": 375, "height": 850})
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert await nav.get_by_role('link', name='Autoresearch').is_visible()
            finally:
                await browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


async def test_research_ui_evidence_and_multiround_form(tmp_path, monkeypatch):
    from playwright.async_api import async_playwright

    root = tmp_path / 'memory-test'
    output = root / 'trial-000'
    output.mkdir(parents=True)
    write_json(root / 'study.json', {
        'status': 'stopped', 'phase': 'finished', 'stop_reason': 'trial_budget',
        'config': {'suite': 'public-web', 'max_seconds': 300, 'codex_model': 'test-model',
                   'codex_effort': 'high'}, 'best_profile': {}, 'best_validated': False,
        'incumbent': 0, 'trials': [{'id': 0, 'directory': 'trial-000', 'status': 'complete',
            'profile': {}, 'selection': {'decision': 'baseline'},
            'result': {'strict_success': False, 'passed_tasks': 0, 'total_tasks': 2}}]})
    write_json(output / 'observability.json', {'quality': {'strict_success': False},
        'findings': [{'code': 'task_incomplete', 'hypothesis': '<img src=x onerror=alert(1)>',
                      'evidence': {'actions': 3}}]})
    submitted = []
    monkeypatch.setattr('jev_browser.research_ui.launch_research',
                        lambda store, body: submitted.append(body) or {'id': 'memory-test'})
    server = make_server(tmp_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                page = await browser.new_page(viewport={'width': 1400, 'height': 1100})
                await page.goto(f'http://127.0.0.1:{server.server_port}/research?study=memory-test')
                await page.locator('#conclusion-summary').filter(has_text='已完成 1 轮').wait_for()
                await page.get_by_text('查看可追溯证据', exact=False).click()
                await page.get_by_text('t0/study/finding-0 · task_incomplete', exact=True).click()
                assert '/findings/0' in await page.locator('#research-evidence').inner_text()
                assert await page.locator('#research-evidence img').count() == 0
                await page.locator('#max-trials').select_option('4')
                await page.locator('#metric').select_option('latency')
                assert '45 分钟' in await page.locator('#launch-budget').inner_text()
                await page.locator('#start').click()
                await page.locator('#request-status').filter(has_text='已提交').wait_for()
                assert submitted == [{'mode': 'new', 'id': 'memory-test', 'max_trials': 4, 'metric': 'latency'}]
                await page.set_viewport_size({'width': 480, 'height': 900})
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            finally:
                await browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
