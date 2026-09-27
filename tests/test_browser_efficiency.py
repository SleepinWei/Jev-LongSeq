from unittest.mock import AsyncMock

import pytest

from jev_browser.browser import PlaywrightBackend
from jev_browser.dynamic import DynamicController, generate_dynamic
from jev_browser.observability import Observer
from jev_browser.protocol import Operation, Receipt, Task


def task():
    return Task(id="search", objective="Search Spark Labs", control_mode="dynamic", sandbox=True)


async def test_search_survives_unrelated_feed_update_with_bounded_handles():
    definition = task()
    async with PlaywrightBackend(definition) as browser:
        browser.observer = Observer()
        await browser.load_html('<form role="search"><input aria-label="Query"></form>'
                                '<p id="feed">News</p>' + '<button hidden>hidden</button>' * 500)
        obs = await browser.observe()
        assert len(obs.elements) == 1
        assert browser.handles == {}  # No per-node remote handles during observation.
        action = next(a for a in generate_dynamic(obs, definition) if a.operation == Operation.FILL)
        action.bound_value = "Spark Labs"
        await browser.page.locator('#feed').evaluate("el => el.textContent = 'Updated news'")
        assert (await browser.execute(action)).status == "ok"
        assert await browser.page.locator('input').input_value() == "Spark Labs"
        assert len(browser.handles) == 1
        await browser.observe()
        assert browser.handles == {}
        assert {'browser.snapshot', 'browser.snapshot_read', 'browser.release_handles'} <= {
            s['name'] for s in browser.observer.spans}


@pytest.mark.parametrize("change", [
    "document.querySelector('input').replaceWith(document.querySelector('input').cloneNode(true))",
    "document.querySelector('input').setAttribute('aria-label', 'New destination')",
    "document.querySelector('input').disabled = true",
    "document.querySelector('input').value = 'user edit'",
    "document.querySelector('span').textContent = 'Different search context'",
    "document.body.insertAdjacentHTML('beforeend', '<div role=dialog>Confirm</div>')",
    "document.body.insertAdjacentHTML('beforeend', '<p>Verify you are human</p>')",
    "location.hash = 'new-page'",
])
async def test_search_rejects_changed_target_or_context(change):
    definition = task()
    async with PlaywrightBackend(definition) as browser:
        await browser.load_html('<form role="search"><span>Search records</span>'
                                '<input aria-label="Query"></form>')
        obs = await browser.observe()
        action = next(a for a in generate_dynamic(obs, definition) if a.operation == Operation.FILL)
        action.bound_value = "Spark Labs"
        await browser.page.evaluate(change)
        assert (await browser.execute(action)).status == "stale"
        assert await browser.page.locator('input').input_value() != "Spark Labs"


async def test_non_search_write_retains_full_page_check():
    definition = task()
    async with PlaywrightBackend(definition) as browser:
        await browser.load_html('<p>Transfer $10</p><button>Confirm</button>')
        obs = await browser.observe()
        action = next(a for a in generate_dynamic(obs, definition) if a.operation == Operation.CLICK)
        await browser.page.locator('p').evaluate("el => el.textContent = 'Transfer $100'")
        assert (await browser.execute(action)).status == "stale"


async def test_hidden_iframe_does_not_block_main_page_but_visible_frame_does():
    definition = task()
    async with PlaywrightBackend(definition) as browser:
        await browser.load_html('<form role="search"><input aria-label="Query"></form>'
                                '<iframe hidden srcdoc="Background"></iframe>')
        obs = await browser.observe()
        assert "unsupported_iframe" not in obs.errors
        action = next(a for a in generate_dynamic(obs, definition) if a.operation == Operation.FILL)
        action.bound_value = "Spark Labs"
        await browser.page.locator('iframe').evaluate("el => el.hidden = false")
        assert (await browser.execute(action)).status == "stale"
        assert "unsupported_iframe" in (await browser.observe()).errors


@pytest.mark.parametrize("status", ["stale", "ok", "unknown"])
async def test_literal_input_cache_only_survives_undispatched_retry(status):
    from jev_browser.protocol import Element, Observation

    definition = task()
    obs = Observation(observation_id="one", document_version="v1", tab_id="tab-0",
                      url="about:blank", title="Search", text="News",
                      elements=[Element(id="e0", role="textbox", name="Query", editable=True)])
    backend = AsyncMock()
    backend.execute.return_value = Receipt(action_id="a", status=status)
    feedback = AsyncMock()
    feedback.value.return_value = "Spark Labs"
    controller = DynamicController(definition, backend, None, feedback=feedback)
    action = next(a for a in generate_dynamic(obs, definition) if a.operation == Operation.FILL)
    await controller.bind_input(action, obs)
    await controller.perform(action, obs)
    obs.text = "Updated unrelated news"
    retry = next(a for a in generate_dynamic(obs, definition) if a.operation == Operation.FILL)
    await controller.bind_input(retry, obs)
    assert feedback.value.await_count == (1 if status == "stale" else 2)
    assert retry.bound_value == "Spark Labs"
    # A new instruction or changed target value invalidates a cached retry.
    controller.memory.feedback["next_goal"] = "Search another term"
    retry = next(a for a in generate_dynamic(obs, definition) if a.operation == Operation.FILL)
    await controller.bind_input(retry, obs)
    assert feedback.value.await_count == (2 if status == "stale" else 3)


async def test_evidence_derived_input_is_not_cached():
    from jev_browser.protocol import Element, Observation

    obs = Observation(observation_id="one", document_version="v1", tab_id="tab-0",
                      url="about:blank", title="Search", text="Suggested: Something else",
                      elements=[Element(id="e0", role="textbox", name="Query", editable=True)])
    definition = task()
    feedback = AsyncMock()
    feedback.value.return_value = "Something else"
    controller = DynamicController(definition, AsyncMock(), None, feedback=feedback)
    action = next(a for a in generate_dynamic(obs, definition) if a.operation == Operation.FILL)
    await controller.bind_input(action, obs)
    action = next(a for a in generate_dynamic(obs, definition) if a.operation == Operation.FILL)
    await controller.bind_input(action, obs)
    assert feedback.value.await_count == 2
