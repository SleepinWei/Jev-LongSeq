"""Reject impossible stage operations before policy dispatch and repair with facts."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from jev_browser.dynamic import (
    DynamicController,
    Feedback,
    InvalidFeedbackOutput,
    JsonFeedback,
    StageControl,
    control_capabilities,
    stage_plan_diagnostics,
)
from jev_browser.models import ModelTransport
from jev_browser.protocol import Element, Observation, Operation, Task


def page():
    return Observation(observation_id="current", document_version="v1", tab_id="tab",
        url="about:blank", title="Vendor", text="New Vendor", elements=[
            Element(id="display", role="button", name="Select display name as"),
            Element(id="first", role="textbox", name="First Name", editable=True),
            Element(id="option", role="menuitem", name="Requested display name")])


def task():
    return Task(id="capabilities", sandbox=True, control_mode="dynamic",
                objective="Create the requested vendor")


async def test_impossible_button_fill_is_repaired_before_fast_policy_with_current_capabilities():
    requests = []

    def respond(request):
        context = json.loads(json.loads(request.content)["messages"][-1]["content"])
        requests.append(context)
        assert context["current_control_capabilities"]["display"] == ["click"]
        if len(requests) == 1:
            ref, operation = "display", "fill"
        else:
            diagnostic = context["schema_error"][0]
            assert diagnostic["element_ref"] == "display"
            assert diagnostic["unavailable_operations"] == ["fill"]
            assert diagnostic["available_operations"] == ["click"]
            ref, operation = "first", "fill"
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "next_goal": "Fill the observed first name", "working_memory": "Other work pending",
            "stage_controls": [{"element_ref": ref, "operations": [operation]}]})}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        brain = JsonFeedback(ModelTransport("https://model.test", "test-key", "test", client=client))
        controller = DynamicController(task(), AsyncMock(), AsyncMock(), feedback=brain)
        await controller.review(page(), phase="step")
        assert len(requests) == 2
        assert set(controller.memory.feedback["execution_scope"]["bindings"]) == {"first"}
        assert any(e["kind"] == "stage_plan_rejected" for e in controller.events)
        assert any(a.element_ref == "first" for a in
                   controller.generate_stage_candidates(page(), limit=250, offset=0))
        controller.backend.execute.assert_not_awaited()
        controller.policy.choose.assert_not_awaited()


async def test_two_impossible_plans_preserve_previous_scope_and_memory_without_dispatch():
    brain = AsyncMock()
    brain.review.return_value = Feedback(next_goal="Fill a button", working_memory="wrong replacement",
        stage_controls=[StageControl(element_ref="display", operations=["fill"])])
    controller = DynamicController(task(), AsyncMock(), AsyncMock(), feedback=brain)
    original = {"next_goal": "Original work", "working_memory": "Keep this checkpoint",
                "execution_scope": {"bindings": {"prior": {"operations": ["click"]}}}}
    controller.memory.feedback = original.copy()
    with pytest.raises(InvalidFeedbackOutput):
        await controller.review(page(), phase="ui_checkpoint")
    assert controller.memory.feedback == original
    assert controller.task.objective == task().objective
    assert brain.review.await_count == 2
    controller.backend.execute.assert_not_awaited()


@pytest.mark.parametrize("case", ["disabled", "readonly", "task_disallowed", "native_select"])
def test_capabilities_follow_actual_generator_and_task_permissions(case):
    obs, definition = page(), task()
    operation = "fill"
    if case == "disabled":
        obs.elements[1].enabled = False
    elif case == "readonly":
        obs.elements[1].read_only = True
    elif case == "task_disallowed":
        definition.allowed_operations = [Operation.CLICK, Operation.WAIT]
    else:
        operation = "select"  # A text field is not a native selection control.
    feedback = Feedback(next_goal="Set field",
        stage_controls=[StageControl(element_ref="first", operations=[operation])])
    assert stage_plan_diagnostics(feedback, obs, definition)


def test_menu_select_normalization_and_readonly_navigation_scope_stay_supported():
    feedback = Feedback(next_goal="Select the visible option",
        stage_controls=[StageControl(element_ref="option", operations=["select"])])
    assert not stage_plan_diagnostics(feedback, page(), task())
    assert not stage_plan_diagnostics(Feedback(next_goal="Locate next page", stage_controls=[]), page(), task())
    assert control_capabilities(page(), task())["option"] == ["click"]


async def test_local_readback_does_not_revalidate_inherited_stage_against_changed_controls():
    brain = AsyncMock()
    feedback = Feedback(next_goal="Prior stage", last_outcome="pending",
        stage_controls=[StageControl(element_ref="display", operations=["fill"])])
    feedback._local_readback = True
    brain.review.return_value = feedback
    controller = DynamicController(task(), AsyncMock(), AsyncMock(), feedback=brain)
    await controller.review(page(), phase="action_readback")
    assert brain.review.await_count == 1
    assert not any(e["kind"] == "stage_plan_rejected" for e in controller.events)
