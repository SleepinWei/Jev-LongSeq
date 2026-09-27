import asyncio
import json

import pytest
from playwright.async_api import async_playwright

from jev_browser.chrome import ChromeBackend
from jev_browser.inspector import Store
from jev_browser.preview import LivePreview
from jev_browser.protocol import Task


async def test_preview_recovers_after_transient_capture_error(tmp_path):
    calls = 0

    async def capture():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("navigation")
        return b"recovered frame", "https://session.example/"

    preview = LivePreview(tmp_path, capture, archive=True)
    preview.start()
    try:
        for _ in range(30):
            if (tmp_path / "frames.jsonl").exists():
                break
            await asyncio.sleep(0.1)
        assert (tmp_path / "live.jpg").read_bytes() == b"recovered frame"
        assert json.loads((tmp_path / "live.json").read_text())["active"]
    finally:
        await preview.close()


async def test_chrome_reuses_session_and_preserves_user_tabs(tmp_path, monkeypatch):
    daemon = pytest.importorskip("browser_harness.daemon")
    profile = tmp_path / "profile"
    output = tmp_path / "runs" / "task"
    output.mkdir(parents=True)
    (output / "manifest.json").write_text('{}')
    task = Task(id="chrome", objective="read", control_mode="dynamic",
                start_url="https://session.example/", allowed_origins=["https://session.example"])

    class TestChrome(ChromeBackend):
        async def _route(self, route):
            await route.fulfill(body='<h1>Shared session</h1><button>Continue</button>',
                                content_type="text/html")

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            str(profile), headless=True, args=["--remote-debugging-port=0"])
        try:
            port = (profile / "DevToolsActivePort").read_text().splitlines()[0]
            monkeypatch.setattr(daemon, "get_ws_url", lambda: f"http://127.0.0.1:{port}")
            original = context.pages[0]
            await original.set_content('<h1>User tab</h1>')
            await context.add_cookies([{"name": "test_session", "value": "shared",
                                       "url": "https://session.example/"}])
            async with TestChrome(task, output=output, live_preview=True) as backend:
                obs = await backend.observe()
                assert obs.http_status == 200
                assert "Shared session" in obs.text
                assert await backend.page.evaluate("document.cookie") == "test_session=shared"
                assert len(backend.pages) == 1
                assert len(context.pages) == 2
                for _ in range(50):
                    if (output / "frames.jsonl").exists():
                        break
                    await asyncio.sleep(0.1)
                store = Store(output.parent)
                assert store.frames(output)
                data, mime = store.image("task", "0")
                assert mime == "image/jpeg" and data.startswith(b'\xff\xd8')
            assert await original.locator("h1").inner_text() == "User tab"
            assert len(context.pages) == 2
            assert await context.pages[1].locator("h1").inner_text() == "Shared session"
            assert not (output / "trace.zip").exists()
            assert not json.loads((output / "live.json").read_text())["active"]
        finally:
            await context.close()


@pytest.mark.parametrize("resource", ["preview/../../secret.jpg", "preview/link.jpg"])
def test_chrome_replay_rejects_external_paths(tmp_path, resource):
    run = tmp_path / "runs" / "task"
    (run / "preview").mkdir(parents=True)
    (run / "manifest.json").write_text('{}')
    secret = tmp_path / "secret.jpg"
    secret.write_bytes(b"secret")
    (run / "preview" / "link.jpg").symlink_to(secret)
    (run / "frames.jsonl").write_text(json.dumps({"resource": resource}) + "\n")
    with pytest.raises(ValueError):
        Store(run.parent).image("task", "0")
