"""Rapid seeks with slow screenshots must settle on matching historical evidence."""
import asyncio
import json
import re
import threading
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import Error, async_playwright, expect

from jev_browser.inspector import make_server
from jev_browser.observability import write_json


async def test_fast_scrubbing_limits_image_requests_and_preserves_final_frame(tmp_path):
    run = tmp_path / "scrubbing"
    run.mkdir()
    (run / "preview").mkdir()
    write_json(run / "manifest.json", {"task_id": "scrubbing", "policy": "jev"})
    write_json(run / "task.json", {"objective": "Original goal"})
    write_json(run / "report.json", {"result": {"status": "failed", "actions": 30}, "grade": {}})
    base = datetime(2026, 10, 9, tzinfo=UTC).timestamp()
    rows, frames = [], []
    for i in range(30):
        obs = {"observation_id": f"obs-{i}", "title": f"Page {i}", "url": "https://example.test/",
               "tab_id": "tab-0", "tabs": {}, "elements": []}
        action = {"id": f"a{i}", "operation": "click", "description": f"Action {i}",
                  "observation_id": f"obs-{i}", "element_ref": f"e{i}", "effect": "read"}
        events = [{"kind": "observation", "observation": obs},
                  {"kind": "feedback", "feedback": {"next_goal": f"Goal {i}", "inputs": []}},
                  {"kind": "action", "action": action, "receipt": {"status": "ok"}}]
        for offset, event in enumerate(events):
            rows.append({"time": datetime.fromtimestamp(base + 3 * i + offset, UTC).isoformat(), **event})
        frames.append({"time": base + 3 * i, "resource": "preview/frame.png", "width": 100, "height": 100,
                       "observation_id": f"obs-{i}", "overlays": [
                           {"id": f"e{i}", "label": f"Control {i}", "editable": False,
                            "rect": {"x": 10, "y": 10, "w": 20, "h": 20}}]})
    (run / "trajectory.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    (run / "frames.jsonl").write_text("".join(json.dumps(frame) + "\n" for frame in frames))
    server = make_server(tmp_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content("<h1>Frame</h1>")
                image = await page.screenshot(path=str(run / "preview/frame.png"))
                requested, errors = [], []
                page.on("pageerror", lambda error: errors.append(str(error)))

                async def slow_image(route):
                    frame = parse_qs(urlsplit(route.request.url).query)["frame"][0]
                    requested.append(frame)
                    await asyncio.sleep(0.3)
                    try:
                        await route.fulfill(status=200, content_type="image/png", body=image)
                    except Error:
                        pass  # Aborted obsolete requests must not affect the final frame.

                await page.route(re.compile(r"/api/image\?"), slow_image)
                await page.goto(f"http://127.0.0.1:{server.server_port}/?run=scrubbing&replay=1")
                await expect(page.locator("#position")).to_have_text("1 / 90")
                await expect(page.locator("#targets .target")).to_have_count(0)
                requested.clear()
                await page.locator("#scrubber").evaluate("""async slider => {
                  for(let i=1;i<24;i++) {
                    slider.value=3*i+2; slider.dispatchEvent(new Event('input',{bubbles:true}));
                    await new Promise(resolve=>setTimeout(resolve,3));
                  }
                  slider.dispatchEvent(new Event('change',{bubbles:true}));
                }""")
                await expect(page.locator("#position")).to_have_text("72 / 90")
                await expect(page.locator("#step-instruction")).to_have_text("Goal 23")
                await expect(page.locator("#replay-action")).to_contain_text("Action 23")
                await expect(page.locator("#targets .target span")).to_have_text("e23")
                assert len(requested) <= 3, f"Each intermediate seek requested an image: {requested}"
                assert requested[-1] == "23"
                before = len(requested)
                # Request a slow old frame, then return to a cached newer frame.
                await page.locator("#scrubber").evaluate("el => {el.value=2; el.dispatchEvent(new Event('input')); el.dispatchEvent(new Event('change'));}")
                await expect(page.locator("#position")).to_have_text("3 / 90")
                await page.wait_for_timeout(130)
                await page.locator("#scrubber").evaluate("el => {el.value=71; el.dispatchEvent(new Event('input')); el.dispatchEvent(new Event('change'));}")
                await expect(page.locator("#targets .target span")).to_have_text("e23")
                await page.wait_for_timeout(400)
                await expect(page.locator("#step-instruction")).to_have_text("Goal 23")
                await expect(page.locator("#targets .target span")).to_have_text("e23")
                assert len(requested) == before + 1, "Cached frame should not be fetched again"
                assert not errors
            finally:
                await browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
