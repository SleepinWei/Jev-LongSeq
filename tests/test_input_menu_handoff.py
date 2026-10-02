"""A confirmed input changing menu choices must retire the pre-input guidance."""

from unittest.mock import AsyncMock

import pytest

from jev_browser.dynamic import DynamicController, Feedback, StageControl, generate_dynamic
from jev_browser.protocol import Element, Observation, Operation, Receipt, Task


def page():
    return Observation(observation_id="before", document_version="v1", tab_id="tab",
        url="about:blank", title="Vendor", text="Vendor form", elements=[
            Element(id="last", role="textbox", name="Last Name", editable=True),
            Element(id="open", role="button", name="Select display name as"),
            Element(id="choice", role="menuitem", name="Ananya Ananya")])


async def filled_controller(status="ok"):
    task = Task(id="menu-input", sandbox=True, control_mode="dynamic", objective="Create the requested vendor")
    backend, brain = AsyncMock(), AsyncMock()
    backend.execute.return_value = Receipt(action_id="fill", status=status)
    brain.review.return_value = Feedback(next_goal="Fill the name then open its display menu",
        stage_controls=[StageControl(element_ref="last", operations=["fill"]),
                        StageControl(element_ref="open", operations=["click"])])
    controller = DynamicController(task, backend, AsyncMock(), feedback=brain)
    before = page()
    await controller.review(before, phase="step")
    action = next(a for a in generate_dynamic(before, task) if a.element_ref == "last")
    action.bound_value = "Reddy"
    await controller.perform(action, before)
    after = before.model_copy(deep=True)
    after.observation_id, after.document_version = "after", "v2"
    after.elements[0].value = "Reddy"
    after.elements[2].name = "Ananya Reddy Ananya Reddy"
    return controller, after


async def test_input_option_change_requests_fresh_scope_without_authorizing_choice_or_business_save():
    controller, after = await filled_controller()
    assert controller.confirm_visible_input(after)
    assert controller.ui_review_due and not controller.pending
    assert not controller.memory.write_checkpoints
    choice = next(a for a in generate_dynamic(after, controller.task) if a.element_ref == "choice")
    assert not controller.stage_action_allowed(choice, after)
    controller.feedback_model.review.return_value = Feedback(next_goal="Click the observed matching name",
        stage_controls=[StageControl(element_ref="choice", operations=["click"])])
    await controller.review(after, phase="ui_checkpoint")
    candidates = controller.generate_stage_candidates(after, limit=250, offset=0)
    assert any(a.element_ref == "choice" for a in candidates)
    assert not any(a.element_ref == "open" for a in candidates)
    assert controller.memory.confirmed_actions[-1]["business_commit_confirmed"] is False
    assert any(e["kind"] == "input_menu_handoff" for e in controller.events)
    controller.backend.execute.assert_awaited_once()  # Original fill only.


@pytest.mark.parametrize("case", ["unchanged_menu", "id_only", "unknown", "new_page",
                                  "loading", "error", "disabled_option", "icon_only"])
async def test_unchanged_or_unconfirmed_or_unsafe_menu_does_not_trigger_input_handoff(case):
    controller, after = await filled_controller("unknown" if case == "unknown" else "ok")
    if case in {"unchanged_menu", "id_only"}:
        after.elements[2].name = "Ananya Ananya"
        if case == "id_only":
            after.elements[2].id = "new-handle"
    elif case == "new_page":
        after.url = "about:another"
    elif case == "loading":
        after.loading = True
    elif case == "error":
        after.errors = ["page_error:new runtime exception"]
    elif case == "disabled_option":
        after.elements[2].enabled = False
    elif case == "icon_only":
        after.elements[2].name = "menu (icon control)"
    controller.confirm_visible_input(after)
    assert not controller.ui_review_due
    assert not any(e["kind"] == "input_menu_handoff" for e in controller.events)
    if case in {"unknown", "new_page"}:
        assert controller.pending and controller.memory.pending_writes
    controller.backend.execute.assert_awaited_once()
    assert Operation.CLICK not in {e.get("operation") for e in controller.memory.confirmed_actions}
