"""Offline coverage of the original Agent bridge; no paid model requests."""
import base64
import json
import re
import subprocess
import threading
from unittest.mock import Mock

import httpx
import pytest

from jev_browser import ultrafast
from jev_browser.inspector import Store, make_server


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setattr("jev_browser.run_status.activities", lambda root: [])
    root = tmp_path / "source"
    package = root / "jev_ultrafast"
    package.mkdir(parents=True)
    for name in ("agent.py", "browser.py", "model.py", "questions.py", "snapshot.js"):
        (package / name).write_text("# original source")
    monkeypatch.setenv("JEV_ULTRAFAST_ROOT", str(root))
    monkeypatch.setenv("TYPESAFE_API_KEY", "private-test-key")
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "private-test-key")
    monkeypatch.setattr(ultrafast.threading, "Thread", Mock())
    child = Mock()
    child.poll.return_value = None
    spawn = Mock(return_value=child)
    monkeypatch.setattr(ultrafast.subprocess, "Popen", spawn)
    return Store(tmp_path / "runs"), spawn


def test_launch_native_worker_and_shared_lock(configured):
    store, spawn = configured
    prompt = '打开 https://example.com，查看标题。 $(echo unsafe)'
    result = ultrafast.launch(store, {"prompt": prompt})
    command = spawn.call_args.args[0]
    assert command[1:3] == ["-m", "jev_browser.ultrafast"]
    assert "dynamic" not in command and "private-test-key" not in str(command)
    path = ultrafast.run_path(store, result["id"])
    meta = json.loads((path / "meta.json").read_text())
    assert meta["prompt"] == prompt
    assert meta["url"] == "https://example.com/"
    assert len(meta["source_sha256"]) == 64
    assert ultrafast.listing(store) == [meta]
    assert store.listing() == []  # Native artifacts never masquerade as LongSeq traces.
    assert ultrafast.data(store, result["id"])["active"]
    with pytest.raises(RuntimeError):
        store.launch_demo()
    with pytest.raises(RuntimeError):
        ultrafast.launch(store, {"prompt": "another"})
    spawn.return_value.poll.return_value = 1
    assert ultrafast.data(store, result["id"])["state"]["status"] == "interrupted"


@pytest.mark.parametrize("body", [None, [], {}, {"prompt": " "}, {"prompt": 42},
    {"prompt": "x", "url": "file:///etc/passwd"}, {"prompt": "x", "url": 12},
    {"prompt": "x", "url": "http://a:b@example.com"}])
def test_invalid_launch(configured, body):
    store, spawn = configured
    with pytest.raises(ValueError):
        ultrafast.launch(store, body)
    spawn.assert_not_called()


def test_source_and_credentials_required(configured, monkeypatch):
    store, spawn = configured
    monkeypatch.delenv("TEXT_MODEL_API_KEY")
    with pytest.raises(ValueError, match="TEXT_MODEL_API_KEY"):
        ultrafast.launch(store, {"prompt": "read"})
    assert "private-test-key" not in json.dumps(ultrafast.configuration())
    spawn.assert_not_called()


def test_native_execution_records_and_closes(tmp_path):
    ultrafast.write_json(tmp_path / "meta.json", {"prompt": "Read title", "url": "https://example.com/"})
    closed = []

    class Agent:
        def __init__(self, url, goal, **kwargs):
            assert goal == "Read title" and url == "https://example.com/"
            assert kwargs == {"screenshots": True}

        def snapshot(self):
            return {"status": "ready", "page": {"title": "Example", "screenshot":
                    base64.b64encode(b"jpeg").decode()}, "history": []}

        def run(self):
            yield {**self.snapshot(), "status": "done", "elapsed_ms": 200,
                   "decisions": [{"operation": "DONE"}]}

        def close(self):
            closed.append(True)

    assert ultrafast.execute(tmp_path, Agent) == 0
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["status"] == "done"
    assert "screenshot" not in state["page"]
    assert "strict_success" not in state
    assert (tmp_path / "latest.jpg").read_bytes() == b"jpeg"
    assert len((tmp_path / "snapshots.jsonl").read_text().splitlines()) == 2
    assert closed == [True]


def test_worker_failure_is_saved_without_secret(tmp_path):
    ultrafast.write_json(tmp_path / "meta.json", {"prompt": "Read", "url": "https://example.com/"})
    class BrokenAgent:
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError("private-provider-key")

    assert ultrafast.execute(tmp_path, BrokenAgent) == 1
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["status"] == "error"
    assert "private-provider-key" not in json.dumps(state)


def test_timeout_preserves_last_native_state(tmp_path):
    ultrafast.write_json(tmp_path / "state.json", {"status": "ready", "history": [{"step": 1}]})
    child = Mock()
    child.wait.side_effect = [subprocess.TimeoutExpired("native", 300), 0]
    ultrafast.supervise(child, tmp_path)
    child.terminate.assert_called_once()
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["status"] == "timeout" and len(state["history"]) == 1


def test_paths_cannot_escape_runs(tmp_path):
    store = Store(tmp_path / "runs")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "meta.json").write_text("{}")
    (store.root / "ultrafast").mkdir(parents=True)
    (store.root / "ultrafast" / "escape").symlink_to(outside, target_is_directory=True)
    for run_id in ("../outside", "escape", str(outside)):
        with pytest.raises(ValueError):
            ultrafast.run_path(store, run_id)


def test_http_view_and_launch_guards(tmp_path):
    server = make_server(tmp_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}") as client:
            page = client.get("/ultrafast")
            assert page.status_code == 200 and "原始 JEV Ultrafast" in page.text
            token = re.search(r'name="demo-token" content="([^"]+)"', page.text)[1]
            assert client.get("/original.js").status_code == 200
            assert client.get("/api/ultrafast/runs").json() == []
            assert client.get("/api/ultrafast/run?id=../../outside").status_code == 404
            assert client.post("/api/ultrafast/launch", json={"prompt": "x"}).status_code == 403
            assert client.post("/api/ultrafast/launch", json={"prompt": "x"}, headers={
                "X-Demo-Token": token, "Origin": "https://evil.test"}).status_code == 403
            assert client.post("/api/ultrafast/launch", json={"prompt": ""}, headers={
                "X-Demo-Token": token}).status_code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_waiting_for_chrome_is_visible_and_only_current_run_can_stop(configured):
    store, spawn = configured
    result = ultrafast.launch(store, {"prompt": "read"})
    path = ultrafast.run_path(store, result["id"])
    (path / "console.log").write_text('Chrome is asking "Allow remote debugging?" private-test-key')
    data = ultrafast.data(store, result["id"])
    assert "等待 Chrome 授权" in data["connection_message"]
    assert "private-test-key" not in json.dumps(data)
    with pytest.raises(ValueError):
        ultrafast.stop(store, {"id": "other"})
    spawn.return_value.terminate.assert_not_called()
    assert ultrafast.stop(store, result)["stopping"]
    spawn.return_value.terminate.assert_called_once()


def native_snapshot(fingerprint, screenshot, *, decision=None):
    return {"status": "predicted" if decision else "ready", "decision": decision,
            "decisions": [decision] if decision else [], "history": [],
            "page": {"url": "https://example.test", "title": fingerprint, "w": 1000, "h": 500,
                     "fingerprint": fingerprint, "screenshot": base64.b64encode(screenshot).decode(),
                     "actions": [{"id": "fill-2", "label": "Search before navigation"}]},
            "elements": [{"index": "2", "label": "Search before navigation", "role": "combobox",
                          "operations": ["SELECT"], "options": [{"index": "2:1"}]}],
            "overlays": [{"index": "2", "editable": True, "label": "Search before navigation",
                          "rect": {"x": 100, "y": 50, "w": 200, "h": 50}}]}


def native_decision():
    return {"choice": "fill-2", "operation": "SELECT", "target": "2:1", "fingerprint": "before",
            "latency_ms": 100, "target_confidence": .9, "operation_probabilities": {"SELECT": .8},
            "target_probabilities": {"2:1": .9}, "elapsed_ms": 100}


def test_decision_capture_happens_before_action_and_frame_is_immutable(tmp_path):
    ultrafast.write_json(tmp_path / "meta.json", {"prompt": "test", "url": "https://example.test"})

    class Agent:
        def __init__(self, *_args, **_kwargs):
            self.state = native_snapshot("before", b"before-image")

        def snapshot(self):
            return self.state

        def command(self, name, body=None):
            if name == "tick":
                self.command("predict")
                return self.command("act")
            if name == "predict":
                self.state = native_snapshot("before", b"before-image", decision=native_decision())
            else:
                # The decision observation must be durable BEFORE page-changing execution.
                context = json.loads((tmp_path / "decisions/0.json").read_text())
                assert context["page"]["fingerprint"] == "before"
                self.state = native_snapshot("after", b"after-image")
                self.state.update(status="done", decisions=[native_decision()])
            return self.state

        def run(self):
            yield self.command("tick")

        def close(self):
            pass

    assert ultrafast.execute(tmp_path, Agent) == 0
    context = json.loads((tmp_path / "decisions/0.json").read_text())
    assert (tmp_path / context["frame"]).read_bytes() == b"before-image"
    assert (tmp_path / "latest.jpg").read_bytes() == b"after-image"
    assert "screenshot" not in context["page"]


def test_legacy_decision_never_uses_unrelated_final_screenshot(tmp_path):
    path = tmp_path / "ultrafast" / "old"
    path.mkdir(parents=True)
    ultrafast.write_json(path / "meta.json", {"id": "old"})
    before = native_snapshot("before", b"old")
    after = native_snapshot("after", b"new")
    after["decisions"] = [native_decision()]
    ultrafast.write_json(path / "state.json", after)
    (path / "snapshots.jsonl").write_text(json.dumps(before)+"\n"+json.dumps(after)+"\n")
    store = Store(tmp_path)
    context = ultrafast.decision_data(store, "old", "0")
    assert context["page"]["fingerprint"] == "before" and context["frame"] is None
    for value in ("-1", "../../secret", "9"):
        with pytest.raises(ValueError):
            ultrafast.decision_data(store, "old", value)
    with pytest.raises(ValueError):
        ultrafast.image_path(store, "old", "../../secret")


async def test_native_preview_maps_select_target_and_replays_dom_under_csp(tmp_path):
    import time

    from playwright.async_api import async_playwright

    path = tmp_path / "ultrafast" / "preview"
    path.mkdir(parents=True)
    ultrafast.write_json(path / "meta.json", {"id": "preview", "prompt": "test", "url": "https://example.test"})
    server = make_server(tmp_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                page = await browser.new_page(viewport={"width": 1500, "height": 1200})
                await page.set_content('<h1>Decision page</h1>')
                screenshot = await page.screenshot(type="jpeg")
                snapshot = native_snapshot("before", screenshot, decision=native_decision())
                snapshot["elements"][0]["label"] = '<img onerror="alert(1)">'
                ultrafast.record(path, snapshot, time.monotonic())
                later = native_snapshot("after", screenshot)
                later.update(status="done", decisions=[native_decision()])
                later["history"] = [{"step": 1, "choice": "fill-2", "action": "Search",
                                     "text": '<img src=x onerror="alert(1)">', "page_changed": True,
                                     "executed_ms": 200, "latency_ms": 100}]
                later["overlays"][0]["rect"]["x"] = 600
                later["elements"][0]["label"] = "Unrelated reused DOM 2"
                ultrafast.record(path, later, time.monotonic())
                await page.goto(f'http://127.0.0.1:{server.server_port}/ultrafast?run=preview')
                target = page.locator('#targets .target.selected')
                await target.wait_for()
                assert await page.locator('#snapshot-strip .snapshot-card').count() == 2
                assert '输入：' in await page.locator('#execution-detail').inner_text()
                assert '页面已变化' in await page.locator('#execution-detail').inner_text()
                assert await page.locator('#execution-detail img').count() == 0
                await page.locator('#next-snapshot').click()
                await page.wait_for_function("document.querySelector('.studio-view[data-view=ultrafast]').shadowRoot.querySelector('#preview-mode').textContent === 'LATEST PAGE'")
                assert await page.locator('#next-snapshot').is_disabled()
                await page.locator('#previous-snapshot').click()
                await target.wait_for()
                assert await page.locator('#previous-snapshot').is_disabled()
                bounds = await target.bounding_box()
                viewport = await page.locator('.viewport').bounding_box()
                assert (bounds['x'] - viewport['x']) / viewport['width'] == pytest.approx(.1, abs=.002)
                assert bounds['width'] / viewport['width'] == pytest.approx(.2, abs=.002)
                assert await page.locator('#choices .choice.best .probability').inner_text() == '90%'
                assert await page.locator('#choices img').count() == 0
                await page.locator('#choices .choice.best').hover()
                assert 'inspected' in await target.get_attribute('class')
                await page.locator('#overlays').uncheck()
                assert await page.locator('#targets').is_hidden()
                await page.locator('#overlays').check()
                await page.locator('#decision-picker').select_option('page')
                await page.wait_for_function("document.querySelector('.studio-view[data-view=ultrafast]').shadowRoot.querySelector('#preview-mode').textContent === 'LATEST PAGE'")
                assert await page.locator('#targets .selected').count() == 0
                assert 'Unrelated reused DOM 2' in await page.locator('#choices').inner_text()
                await page.locator('#history [data-decision="0"]').click()
                await target.wait_for()
                assert await page.locator('#follow-decision').is_checked() is False
                await page.wait_for_timeout(1400)  # Polling must not override historical selection.
                assert await page.locator('#decision-picker').input_value() == '0'
                await page.locator('#snapshot-strip [data-decision="page"]').click()
                await page.locator('#return-live').click()
                await target.wait_for()
                assert await page.locator('#follow-decision').is_checked()
                await page.set_viewport_size({"width": 480, "height": 900})
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            finally:
                await browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_replay_index_does_not_shift_actions_after_unexecuted_decision(tmp_path):
    path = tmp_path / "ultrafast" / "replay"
    path.mkdir(parents=True)
    decisions = [{**native_decision(), "elapsed_ms": t} for t in (100, 200, 400)]
    decisions.append({"operation": "DONE", "choice": "DONE", "elapsed_ms": 600})
    action = {"step": 1, "choice": "fill-2", "executed_ms": 300, "text": "search"}
    (path / "decisions").mkdir()
    ultrafast.write_json(path / "decisions/1.json", {
        "frame": "frames/123.jpg", "page": {"title": "Before execution"}})
    entries = ultrafast.timeline_data(Store(tmp_path), path, {"decisions": decisions, "history": [action]})
    assert [e["action"] for e in entries] == [None, action, None, None]
    assert entries[1]["frame"] == "frames/123.jpg"
    assert entries[0]["frame"] is None  # Never substitute a later screenshot.


def test_legacy_request_keeps_chosen_dom_without_inventing_geometry(tmp_path):
    path = tmp_path / "ultrafast" / "old"
    path.mkdir(parents=True)
    ultrafast.write_json(path / "meta.json", {"id": "old"})
    d = native_decision()
    d["request"] = {"state": {"page": {"title": "Before"}, "elements": [
        {"index": "2", "label": "Original selected element", "operations": ["SELECT"]}]}}
    ultrafast.write_json(path / "state.json", {"decisions": [d]})
    context = ultrafast.decision_data(Store(tmp_path), "old", "0")
    assert context["elements"][0]["label"] == "Original selected element"
    assert context["overlays"] == [] and context["frame"] is None
