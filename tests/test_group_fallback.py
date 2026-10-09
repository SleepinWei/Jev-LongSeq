"""Unsafe caching is advisory; invalid individual actions still require repair."""

from unittest.mock import AsyncMock

import pytest

from jev_browser.dynamic import (
    DynamicController,
    Feedback,
    InvalidFeedbackOutput,
    StageControl,
    generate_dynamic,
)
from jev_browser.protocol import Element, Observation, Receipt, Task


def setup():
    task = Task(id="fallback", sandbox=True, control_mode="dynamic",
                objective="Return company laptop, assigned to Rajesh Kumar")
    obs = Observation(observation_id="now", document_version="v1", tab_id="tab", url="about:blank",
        title="Draft", text="Not Saved", elements=[
            Element(id="activity", name="Activity Name", role="textbox", editable=True, grid_ref="g", row_ref="1"),
            Element(id="user", name="User", role="combobox", editable=True, grid_ref="g", row_ref="1")])
    feedback = Feedback(next_goal="Fill the activity, then resolve User using fresh observed options",
        inputs=[{"name": "Activity Name", "grid_ref": "g", "row_ref": "1", "value": "Return company laptop"},
                {"name": "User", "grid_ref": "g", "row_ref": "1", "value": "Rajesh Kumar"}],
        stage_entry={"intent": "act", "operation": "fill", "element_ref": "activity"},
        stage_controls=[{"element_ref": ref, "operations": ["fill"]} for ref in ("activity", "user")],
        execution_groups=[{"goal": "Activity", "actions": [{"element_ref": "activity", "operation": "fill"}]},
                          {"goal": "User", "actions": [{"element_ref": "user", "operation": "fill"}]}])
    return task, obs, feedback


async def test_valid_primary_grid_fill_survives_ineligible_groups_without_model_repair():
    task, obs, feedback = setup()
    original = feedback.model_dump()
    brain, backend = AsyncMock(), AsyncMock()
    brain.review.return_value = feedback
    brain.value.return_value = "Return company laptop"
    backend.execute.return_value = Receipt(action_id="input", status="ok")
    controller = DynamicController(task, backend, AsyncMock(), feedback=brain)
    result = await controller.review(obs, phase="ui_checkpoint")
    assert brain.review.await_count == 1 and not controller.execution_groups
    assert result.execution_groups == []
    for key in ("stage_entry", "stage_controls", "inputs", "next_goal"):
        assert result.model_dump()[key] == original[key]
    assert any(e["kind"] == "execution_groups_degraded" for e in controller.events)
    assert not any(e["kind"] == "invalid_feedback" for e in controller.events)
    backend.execute.assert_not_awaited()
    action = next(a for a in generate_dynamic(obs, task) if a.element_ref == "activity")
    await controller.bind_input(action, obs)
    assert action.bound_value == "Return company laptop"
    brain.value.assert_not_awaited()  # Exact stage value, not an ineligible cached execution group.
    assert controller.events[-1]["source"]["kind"] == "validated_stage_input"
    assert await controller.perform(action, obs) is None
    backend.execute.assert_awaited_once()
    assert controller.pending and controller.memory.pending_writes  # Normal fresh readback still required.


@pytest.mark.parametrize("case", ["unsupported_operation", "missing_primary_value", "different_entry",
                                  "missing_control", "invalid_boundary", "redacted"])
async def test_fallback_never_repairs_an_invalid_action_or_changes_primary_input(case):
    task, obs, feedback = setup()
    if case == "unsupported_operation":
        feedback.stage_entry.operation = "click"
        feedback.stage_controls[0].operations = ["click"]
    elif case == "missing_primary_value":
        feedback.inputs.pop(0)
    elif case == "different_entry":
        feedback.stage_entry.element_ref = "user"
    elif case == "missing_control":
        feedback.execution_groups[1].actions[0].element_ref = "absent"
    elif case == "invalid_boundary":
        obs.elements.append(Element(id="save", role="button", name="Save"))
        feedback.stage_controls.append(StageControl(element_ref="save", operations=["click"]))
        data = feedback.model_dump()
        data["execution_groups"][0]["actions"].append({"element_ref": "save", "operation": "click"})
        feedback = Feedback.model_validate(data)
    else:
        obs.elements[0].value = "[redacted]"
    brain, backend = AsyncMock(), AsyncMock()
    brain.review.side_effect = [feedback, feedback.model_copy(deep=True)]
    controller = DynamicController(task, backend, AsyncMock(), feedback=brain)
    with pytest.raises(InvalidFeedbackOutput):
        await controller.review(obs, phase="ui_checkpoint")
    assert brain.review.await_count == 2
    assert not any(e["kind"] == "execution_groups_degraded" for e in controller.events)
    backend.execute.assert_not_awaited()
    assert not controller.pending and not controller.memory.pending_writes
