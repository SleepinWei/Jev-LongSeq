"""Identical-content browser reloads are UI receipts, never saved-business proof."""
from unittest.mock import AsyncMock

import pytest

from jev_browser.dynamic import DynamicController, Feedback, generate_dynamic, semantic_key
from jev_browser.protocol import Element, Observation, Operation, Receipt, Task


def page():
    return Observation(observation_id="before", document_version="v1", document_id="1000",
        tab_id="tab", url="https://example.test/report", title="Exits", text="No rows Refresh",
        http_status=200, elements=[Element(id="refresh", role="button", name="reload (icon control)")])


def controller():
    agent = DynamicController(Task(id="reload", control_mode="dynamic",
        objective="Verify exits, then complete independent remaining journal work.",
        allowed_origins=["https://example.test"]), AsyncMock(), AsyncMock(), feedback=AsyncMock())
    agent.memory.feedback = Feedback(next_goal="Refresh and read the report",
        working_memory="Report unresolved; journal still pending",
        stage_entry={"intent": "verify", "operation": "click", "element_ref": "refresh"},
        verification={"goal": "Read report", "fallback_goal": "Continue journal"}).model_dump()
    agent.backend.execute.return_value = Receipt(action_id="refresh", status="ok")
    return agent


async def pending_reload(agent, obs):
    agent.arm_verification(obs)
    action = next(a for a in generate_dynamic(obs, agent.task)
                  if a.element_ref == "refresh" and a.operation == Operation.CLICK)
    assert await agent.perform(action, obs) is None
    return obs.model_copy(update={"observation_id": "fresh", "document_version": "v2", "document_id": "2000"})


async def test_new_document_confirms_only_refresh_keeps_verification_and_blocks_replay():
    agent = controller()
    before = page()
    fresh = await pending_reload(agent, before)
    assert semantic_key(before) == semantic_key(fresh)
    key = agent.pending["key"]
    assert agent.confirm_visible_document_reload(fresh)
    assert agent.pending is None and not agent.memory.pending_writes
    assert not agent.memory.write_checkpoints
    assert agent.memory.feedback["verification"]["goal"] == "Read report"
    assert agent.memory.feedback["working_memory"] == "Report unresolved; journal still pending"
    assert key in agent.consumed
    record = agent.memory.confirmed_actions[-1]
    assert record["confirmation_scope"] == "document_reloaded_ui"
    assert not record["business_commit_confirmed"]
    assert not agent.confirm_visible_document_reload(fresh)
    agent.policy.choose.assert_not_awaited()
    agent.backend.execute.assert_awaited_once()


@pytest.mark.parametrize("change", ["same_document", "missing_document", "old_observation", "url", "tab",
    "frame", "loading", "dialog", "challenge", "http_error", "missing_status", "runtime_error", "unknown"])
async def test_reload_proof_requires_actual_fresh_same_page_browser_document(change):
    agent = controller()
    fresh = await pending_reload(agent, page())
    if change == "same_document":
        fresh.document_id = "1000"
    elif change == "missing_document":
        fresh.document_id = None
    elif change == "old_observation":
        fresh.observation_id = "before"
    elif change == "url":
        fresh.url += "/new"
    elif change == "tab":
        fresh.tab_id = "other"
    elif change == "frame":
        fresh.frame_id = "other"
    elif change == "loading":
        fresh.loading = True
    elif change == "dialog":
        fresh.dialogs = ["Confirm submission?"]
    elif change == "challenge":
        fresh.challenge = True
    elif change == "http_error":
        fresh.http_status = 500
    elif change == "missing_status":
        fresh.http_status = None
    elif change == "runtime_error":
        fresh.errors = ["page_error: refresh failed"]
    else:
        agent.pending["dispatch_status"] = "unknown"
    assert not agent.confirm_visible_document_reload(fresh)
    assert agent.pending and agent.memory.pending_writes
    assert not agent.memory.confirmed_actions and not agent.memory.write_checkpoints


@pytest.mark.parametrize("change", ["save", "submit", "row", "dialog", "href", "non_verification", "legacy", "business_form"])
async def test_marker_is_not_minted_for_business_writes_or_ambiguous_controls(change):
    agent, obs = controller(), page()
    if change in {"save", "submit"}:
        obs.elements[0].name = change.title()
    elif change == "row":
        obs.elements[0].grid_ref, obs.elements[0].row_ref = "g", "row"
    elif change == "dialog":
        obs.dialogs = ["Edit employee"]
    elif change == "href":
        obs.elements[0].href = "https://example.test/pay"
    elif change == "non_verification":
        agent.memory.feedback["verification"] = None
    elif change == "legacy":
        obs.document_id = None
    else:
        obs.elements.append(Element(id="save", role="button", name="Save"))
    fresh = await pending_reload(agent, obs)
    assert "ui_reload_document" not in agent.pending
    assert not agent.confirm_visible_document_reload(fresh)
    # Neither a new document nor a model-assigned scope can confirm Save/Submit.
    agent.pending["confirmation_scope"] = "document_reloaded_ui"
    assert not agent.confirm_transition("confirmed", fresh, "model claim")
    assert agent.pending and not agent.memory.write_checkpoints


async def test_real_identical_report_reload_gets_new_document_without_policy_readback():
    from jev_browser.browser import PlaywrightBackend

    agent = controller()
    html = '<title>Exits</title><p>No rows</p><button onclick="location.reload()">Refresh</button>'
    async with PlaywrightBackend(agent.task) as browser:
        await browser.page.route("https://example.test/report", lambda route: route.fulfill(
            status=200, content_type="text/html", body=html))
        await browser.page.goto("https://example.test/report")
        before = await browser.observe()
        agent.backend = browser
        agent.memory.feedback["stage_entry"]["element_ref"] = next(e.id for e in before.elements if e.name == "Refresh")
        action = next(a for a in generate_dynamic(before, agent.task) if a.operation == Operation.CLICK)
        agent.arm_verification(before)
        assert await agent.perform(action, before) is None
        fresh = await browser.observe()
        assert before.document_id != fresh.document_id
        assert semantic_key(before) == semantic_key(fresh)
        assert agent.confirm_visible_document_reload(fresh)
        assert not agent.memory.write_checkpoints
        assert agent.memory.feedback["verification"]
        agent.policy.choose.assert_not_awaited()
