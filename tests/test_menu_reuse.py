"""Menu expansion can recur across stages; business submissions remain consumed."""

from unittest.mock import AsyncMock

import pytest

from jev_browser.dynamic import (
    DynamicController,
    Feedback,
    StageControl,
    action_key,
    generate_dynamic,
)
from jev_browser.protocol import Element, Observation, Receipt, Task


def page(**updates):
    obs = Observation(observation_id="before", document_version="v1", tab_id="tab-1",
        url="about:blank", title="Vendors", text="Vendors Quick new", elements=[
            Element(id="opener", role="button", name="Quick new")])
    return obs.model_copy(update=updates)


async def opened_controller(*, name="Quick new", status="ok"):
    task = Task(id="menu-reuse", sandbox=True, control_mode="dynamic",
                objective="Create a vendor, then create a journal using the creation menu.")
    backend, brain = AsyncMock(), AsyncMock()
    backend.execute.return_value = Receipt(action_id="opener", status=status)
    brain.review.return_value = Feedback(next_goal="Open creation menu",
        stage_controls=[StageControl(element_ref="opener", operations=["click"])])
    controller = DynamicController(task, backend, AsyncMock(), feedback=brain)
    before = page(elements=[Element(id="opener", role="button", name=name)])
    await controller.review(before, phase="step")
    action = next(a for a in generate_dynamic(before, task) if a.element_ref == "opener")
    await controller.perform(action, before)
    after = before.model_copy(update={"observation_id":"after", "document_version":"v2",
        "text":"Vendors Quick new Vendor Manual Journal", "elements":[*before.elements,
            Element(id="vendor", role="menuitem", name="Vendor"),
            Element(id="journal", role="menuitem", name="Manual Journal")]})
    if status == "ok":
        assert controller.confirm_transition("confirmed", after, "action_readback_review")
    closed = before.model_copy(update={"observation_id":"closed"})
    return controller, before, after, closed, action


async def test_confirmed_closed_menu_needs_new_explicit_authorization_to_reappear_and_dispatch():
    controller, before, _, closed, action = await opened_controller()
    key = action_key(action, before)
    assert key in controller.consumed and key in controller.reusable_menu_actions
    assert not controller.reusable_menu_keys(closed)
    assert await controller.perform(action, closed) == "identical mutation was already dispatched; no resubmission"
    # A local readback cannot silently create a new planning authorization.
    local = Feedback(next_goal="Continue", last_outcome="none")
    local._local_readback = True
    controller.feedback_model.review.return_value = local
    await controller.review(closed, phase="action_readback")
    assert not controller.reusable_menu_keys(closed)
    controller.feedback_model.review.return_value = Feedback(next_goal="Open menu for the journal",
        stage_controls=[StageControl(element_ref="opener", operations=["click"])])
    await controller.review(closed, phase="step")
    candidates = controller.generate_stage_candidates(closed, limit=250, offset=0)
    second = next(a for a in candidates if a.element_ref == "opener")
    assert key in controller.consumed  # Never clear the transaction dedup ledger.
    assert await controller.perform(second, closed) is None
    assert controller.backend.execute.await_count == 2
    assert controller.pending and not controller.reusable_menu_keys(closed)
    assert any(e["kind"] == "menu_opener_reauthorized" for e in controller.events)


@pytest.mark.parametrize("change", ["open", "no_scope", "wrong_environment", "wrong_route", "pending"])
async def test_reuse_cannot_escape_scope_or_pending_guards(change):
    controller, _, after, closed, _ = await opened_controller()
    await controller.review(closed, phase="step")
    obs = closed
    if change == "open":
        obs = after
    elif change == "no_scope":
        controller.memory.feedback["execution_scope"]["bindings"] = {}
    elif change == "wrong_environment":
        controller.memory.environment_id = "another-env"
    elif change == "wrong_route":
        obs = closed.model_copy(update={"url":"about:other"})
    else:
        controller.memory.pending_writes["unknown"] = {"dispatch_status":"unknown"}
    assert not controller.reusable_menu_keys(obs)


@pytest.mark.parametrize("name", ["Save", "Submit", "Publish", "Approve"])
async def test_business_commit_button_is_never_released_even_if_menu_items_appear(name):
    controller, before, _, closed, action = await opened_controller(name=name)
    assert not controller.reusable_menu_actions
    await controller.review(closed, phase="step")
    assert not controller.reusable_menu_keys(closed)
    assert action_key(action, before) in controller.consumed
    assert not any(a.element_ref == "opener" for a in
                   controller.generate_stage_candidates(closed, limit=250, offset=0))


async def test_unknown_menu_dispatch_never_acquires_reuse_permission():
    controller, _, after, closed, _ = await opened_controller(status="unknown")
    assert not controller.confirm_transition("pending", after, "not confirmed")
    assert not controller.reusable_menu_actions
    await controller.review(closed, phase="step")
    assert not controller.reusable_menu_keys(closed)


async def test_second_unknown_dispatch_revokes_previous_reuse_permission():
    controller, _, _, closed, _ = await opened_controller()
    await controller.review(closed, phase="step")
    action = next(a for a in controller.generate_stage_candidates(closed, limit=250, offset=0)
                  if a.element_ref == "opener")
    controller.backend.execute.return_value = Receipt(action_id=action.id, status="unknown")
    assert await controller.perform(action, closed) == "unknown action outcome; no resubmission"
    assert not controller.reusable_menu_actions and not controller.reusable_menu_keys(closed)
