import argparse
import asyncio
import json
import re
import threading
import time
import zipfile
from unittest.mock import Mock

import httpx
import pytest

from jev_browser.browser import PlaywrightBackend
from jev_browser.cli import common, run_trial
from jev_browser.fixture import demo_task
from jev_browser.inspector import Store, events, make_server
from jev_browser.protocol import RunResult


async def test_custom_prompt_uses_catalog_without_fixed_goal_grading(tmp_path, monkeypatch):
    parser = argparse.ArgumentParser()
    common(parser, demo=True)
    args = parser.parse_args(["--mode", "dynamic", "--policy", "jev", "--records", "2"])
    args.command, args.goal = "demo", "读取 item-001 的价格，不要保存。"
    args.output = str(tmp_path)
    monkeypatch.setattr("jev_browser.cli.adapters", lambda args: (None, None, []))

    class Controller:
        final_answer = "read-only test result"

        def __init__(self, task, browser, policy, **kwargs):
            self.task, self.browser = task, browser

        async def run(self):
            assert self.task.objective == args.goal
            assert "item-001" in (await self.browser.observe()).text
            return RunResult(task_id=self.task.id, status="success", reason="test",
                             actions=0, cycles=0, planner_calls=0, elapsed_s=0)

    monkeypatch.setattr("jev_browser.cli.DynamicController", Controller)
    report = await run_trial(args)
    assert report["result"]["strict_success"] is None
    assert report["grade"] == {}
    assert report["manifest"]["benchmark"] == "user-task-ungraded"
    assert "reference_actions" not in report["manifest"]
    assert json.loads((tmp_path / "task.json").read_text())["objective"] == args.goal


def test_prompt_launch_preserves_text_and_rejects_overlap(tmp_path, monkeypatch):
    monkeypatch.setattr("jev_browser.run_status.activities", lambda root: [])
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only")
    child = Mock()
    child.poll.return_value = None
    spawn = Mock(return_value=child)
    monkeypatch.setattr("jev_browser.inspector.subprocess.Popen", spawn)
    store = Store(tmp_path)
    prompt = '读取 item-001；不要保存。 $(echo unsafe) "quoted"'
    result = store.launch_prompt({"prompt": prompt, "records": 4, "brain": "codex"})
    command = spawn.call_args.args[0]
    assert command[command.index("--goal") + 1] == prompt
    assert command[command.index("--mode") + 1] == "dynamic"
    assert command[command.index("--policy") + 1] == "jev"
    assert "--live-preview" in command
    assert store.launch_status() == {"id": result["id"], "running": True, "exit_code": None,
                                     "activities": []}
    with pytest.raises(RuntimeError):
        store.launch_demo()
    with pytest.raises(RuntimeError):
        store.launch_prompt({"prompt": "another", "brain": "codex"})
    child.poll.return_value = 1
    assert store.launch_status()["exit_code"] == 1
    store.launch_prompt({"prompt": "read", "scenario": "web", "url": "https://example.com", "brain": "codex"})
    assert "browse" in spawn.call_args.args[0]
    command = spawn.call_args.args[0]
    assert command[command.index("--backend") + 1] == "chrome"


def test_prompt_defaults_to_configured_deepseek_api(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only")
    monkeypatch.setenv("TEXT_MODEL", "deepseek-flash")
    monkeypatch.setenv("TEXT_MODEL_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test-only")
    for key in ("PLANNER_MODEL", "PLANNER_ENDPOINT", "PLANNER_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    child = Mock()
    child.poll.return_value = None
    spawn = Mock(return_value=child)
    monkeypatch.setattr("jev_browser.inspector.subprocess.Popen", spawn)
    Store(tmp_path).launch_prompt({"prompt": "read only"})
    command = spawn.call_args.args[0]
    assert command[command.index("--brain") + 1] == "api"
    assert "test-only" not in command


@pytest.mark.parametrize("body", [None, [], {}, {"prompt": "  "}, {"prompt": 12},
    {"prompt": "x", "records": True}, {"prompt": "x", "records": 101},
    {"prompt": "x", "scenario": "web", "url": "file:///etc/passwd"},
    {"prompt": "x", "scenario": "other"}])
def test_invalid_prompt_cannot_launch(tmp_path, monkeypatch, body):
    spawn = Mock()
    monkeypatch.setattr("jev_browser.inspector.subprocess.Popen", spawn)
    with pytest.raises(ValueError):
        Store(tmp_path).launch_prompt(body)
    spawn.assert_not_called()


def run_folder(tmp_path):
    run = tmp_path / "sample"
    run.mkdir()
    (run / "manifest.json").write_text(json.dumps({"task_id": "sample", "policy": "rule"}))
    return run


def test_partial_trajectory_keeps_completed_lines(tmp_path):
    path = tmp_path / "trajectory.jsonl"
    path.write_text('{"kind":"observation"}\n{"kind":')
    assert events(path) == [{"kind": "observation"}]


def test_run_lookup_and_symlink_files_cannot_escape_root(tmp_path):
    root = tmp_path / "runs"
    root.mkdir()
    run = run_folder(root)
    secret = tmp_path / "secret"
    secret.write_text("private")
    (run / "final.png").symlink_to(secret)
    store = Store(root)
    with pytest.raises(ValueError):
        store.path("../secret")
    with pytest.raises(ValueError):
        store.image("sample", "final")


@pytest.mark.parametrize("legacy", [False, True])
def test_zip_frames_map_to_wall_time_without_extraction(tmp_path, legacy):
    run = run_folder(tmp_path)
    resource = "resources/frame.jpeg" if legacy else "screencast/frame.jpeg"
    frame = {"type": "screencast-frame", "timestamp": 520, "width": 1280, "height": 900}
    if legacy:
        frame["sha1"] = "frame.jpeg"
    else:
        frame.update(file=resource, frameSwapWallTime=1000020)
    context = {"type": "context-options", "wallTime": 1000000, "monotonicTime": 500}
    with zipfile.ZipFile(run / "trace.zip", "w") as z:
        z.writestr("trace.trace", json.dumps(context) + "\n" + json.dumps(frame))
        z.writestr(resource, b"jpeg-content")
    store = Store(tmp_path)
    data = store.data("sample")
    assert data["frames"] == [{"time": 1000.02, "width": 1280, "height": 900}]
    assert store.image("sample", "0") == (b"jpeg-content", "image/jpeg")
    with pytest.raises(ValueError):
        store.image("sample", "-1")
    assert not (run / "resources").exists()


def test_live_heartbeat_expires_and_terminal_result_wins(tmp_path):
    run = run_folder(tmp_path)
    store = Store(tmp_path)
    (run / "live.json").write_text(json.dumps({"active": True, "time": time.time()}))
    assert store.live("sample")["active"]
    (run / "live.json").write_text(json.dumps({"active": True, "time": time.time() - 10}))
    assert not store.live("sample")["active"]
    (run / "live.json").write_text(json.dumps({"active": True, "time": time.time()}))
    (run / "result.json").write_text('{"status":"success"}')
    assert not store.live("sample")["active"]


async def test_dom_overlay_geometry_under_strict_csp(tmp_path):
    from playwright.async_api import async_playwright

    run = run_folder(tmp_path)
    (run / 'preview').mkdir()
    frame = {'time': 1, 'resource': 'preview/000000.jpg', 'width': 1280, 'height': 900,
             'observation_id': 'obs-1', 'overlays': [
                 {'id': 'e7', 'label': '<img onerror=alert(1)>', 'editable': True,
                  'rect': {'x': 128, 'y': 90, 'w': 256, 'h': 90}}]}
    (run / 'frames.jsonl').write_text(json.dumps(frame) + '\n')
    (run / 'trajectory.jsonl').write_text(json.dumps({
        'time': '2026-09-27T09:00:00+00:00', 'kind': 'observation',
        'observation': {'observation_id': 'obs-1', 'title': 'Test', 'url': 'about:blank',
                        'tab_id': 'tab-0', 'tabs': {'tab-0': 'about:blank',
                                                  'tab-1': 'https://example.test/article'}},
    }) + '\n')
    server = make_server(tmp_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                page = await browser.new_page(viewport={'width': 1500, 'height': 1200})
                await page.set_content('<h1>Preview</h1>')
                await page.screenshot(path=str(run / frame['resource']), type='jpeg')
                await page.goto(f'http://127.0.0.1:{server.server_port}/?run=sample')
                target = page.locator('#targets .target')
                await target.wait_for()
                bounds = await target.bounding_box()
                viewport = await page.locator('.viewport').bounding_box()
                assert (bounds['x'] - viewport['x']) / viewport['width'] == pytest.approx(.1, abs=.002)
                assert bounds['width'] / viewport['width'] == pytest.approx(.2, abs=.002)
                assert (bounds['y'] - viewport['y']) / viewport['height'] == pytest.approx(.1, abs=.002)
                assert await target.locator('span').inner_text() == 'e7 输入'
                assert await target.locator('img').count() == 0
                await page.locator('#overlays').uncheck()
                assert await page.locator('#targets').is_hidden()
                await page.locator('[data-tab="pages"]').click()
                assert await page.locator('#inspector .fact-card').count() == 2
                assert '仅打开 · tab-1' in await page.locator('#inspector').inner_text()
                assert '当前页面 · 已观察 · tab-0' in await page.locator('#inspector').inner_text()
            finally:
                await browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_http_guards_cache_and_readonly_artifacts(tmp_path):
    run = run_folder(tmp_path)
    (run / "trajectory.jsonl").write_text('{"kind":"result"}\n')
    server = make_server(tmp_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}") as client:
            html = client.get("/")
            assert html.status_code == 200
            token = re.search(r'name="demo-token" content="([^"]+)"', html.text)[1]
            assert client.get("/api/runs", headers={"Host": "evil.test"}).status_code == 403
            assert client.post("/api/demo").status_code == 403
            assert client.post("/api/launch", json={"prompt": "hello"}).status_code == 403
            assert client.post("/api/launch", json={"prompt": "hello"}, headers={
                "X-Demo-Token": token, "Origin": "https://evil.test"}).status_code == 403
            assert client.post("/api/launch", json={"prompt": ""}, headers={
                "X-Demo-Token": token}).status_code == 400
            assert client.post("/api/launch", content="broken json", headers={
                "X-Demo-Token": token}).status_code == 400
            assert not client.get("/api/launcher").json()["running"]
            assert (
                client.post(
                    "/api/demo", headers={"X-Demo-Token": token, "Origin": "https://evil.test"}
                ).status_code
                == 403
            )
            assert client.get("/api/run", params={"id": "../secret"}).status_code == 404
            assert (
                client.get("/api/download", params={"id": "sample", "file": ".env"}).status_code
                == 404
            )
            response = client.get("/api/run?id=sample")
            assert response.json()["events"][0]["kind"] == "result"
            assert (
                client.get(
                    "/api/run?id=sample", headers={"If-None-Match": response.headers["etag"]}
                ).status_code
                == 304
            )
            (run / "trajectory.jsonl").write_text('{"kind":"result"}\n{"kind":"error"}\n')
            assert (
                client.get(
                    "/api/run?id=sample", headers={"If-None-Match": response.headers["etag"]}
                ).status_code
                == 200
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("backend_name", ["playwright", "browsergym"])
async def test_live_preview_updates_without_reobserving_or_mutating(tmp_path, backend_name):
    task = demo_task(2)
    if backend_name == "browsergym":
        pytest.importorskip("browsergym.core")
        from jev_browser.gym_backend import BrowserGymBackend

        backend = BrowserGymBackend(
            task, output=tmp_path, live_preview=True, records=2, popup=False, injection=False
        )
    else:
        backend = PlaywrightBackend(task, output=tmp_path, live_preview=True)
    async with backend:
        obs = await backend.observe()
        for _ in range(40):
            if (tmp_path / "live.json").exists():
                break
            await asyncio.sleep(0.05)
        first = json.loads((tmp_path / "live.json").read_text())
        assert first["active"]
        assert (tmp_path / "live.jpg").read_bytes().startswith(b"\xff\xd8")
        # Capture time is additional to the 0.5s interval; loaded machines may
        # take longer than a fixed 0.7s sleep to publish the next frame.
        deadline = time.monotonic() + 8
        second = first
        while second["time"] <= first["time"] and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            second = json.loads((tmp_path / "live.json").read_text())
        assert second["time"] > first["time"]
        assert backend.last.observation_id == obs.observation_id
        if backend_name == "browsergym":
            assert backend.env_steps == 0
    assert not json.loads((tmp_path / "live.json").read_text())["active"]
