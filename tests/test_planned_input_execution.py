"""Exact stage inputs eliminate redundant inference without confirming a write."""
from unittest.mock import AsyncMock

import pytest

from jev_browser.dynamic import DynamicController, Feedback, InvalidInputValue, generate_dynamic
from jev_browser.protocol import Element, Observation, Operation, Task


async def setup(value="", *, select=False, row=None):
    task = Task(id="planned-input", control_mode="dynamic", sandbox=True,
                objective="Clear Employee and re-resolve it from fresh options.")
    field = Element(id="employee", role="combobox", name="Employee", editable=not select,
                    selectable=select, value="EMP-007", options=["EMP-007", "EMP-008"] if select else [],
                    grid_ref="g" if row else None, row_ref=row)
    obs = Observation(observation_id="now", document_version="v", tab_id="tab", url="about:blank",
                      title="Draft", text="Not Saved", elements=[field])
    op = "select" if select else "fill"
    brain = AsyncMock()
    brain.review.return_value = Feedback(next_goal="Clear the current Employee first",
        inputs=[{"name": "Employee", "value": value, "grid_ref": field.grid_ref, "row_ref": row}],
        stage_controls=[{"element_ref": field.id, "operations": [op]}],
        stage_entry={"intent": "act", "element_ref": field.id, "operation": op})
    brain.value.return_value = value
    agent = DynamicController(task, AsyncMock(), AsyncMock(), feedback=brain)
    await agent.review(obs, phase="initial")
    action = next(a for a in generate_dynamic(obs, task)
                  if a.operation == op and a.bound_value is None)
    return agent, obs, action


@pytest.mark.parametrize("value,row", [("", None), ("Ananya", None), ("Pooja", "3")])
async def test_exact_planned_value_skips_helper_without_confirming_or_dispatching(value, row):
    agent, obs, action = await setup(value, row=row)
    before_calls = agent.feedback_calls
    await agent.bind_input(action, obs)
    assert action.bound_value == value and agent.feedback_calls == before_calls
    agent.feedback_model.value.assert_not_awaited()
    agent.backend.execute.assert_not_awaited()
    assert not agent.pending and not agent.memory.pending_writes and not agent.memory.write_checkpoints
    assert agent.events[-1]["input_model_call_skipped"] is True


@pytest.mark.parametrize("change", ["environment", "location", "generation", "scope", "operation",
                                   "readonly", "disabled", "identity", "ambiguous", "row", "pending",
                                   "unknown_write", "stale_observation", "stale_document", "tab", "frame",
                                   "loading", "fresh_scope", "permission"])
async def test_plan_binding_does_not_apply_outside_current_unique_authorized_field(change):
    agent, obs, action = await setup("Ananya")
    scope = agent.memory.feedback["execution_scope"]
    if change == "environment":
        agent.memory.environment_id = "another"
    elif change == "location":
        obs.url = "about:other"
    elif change == "generation":
        scope["generation"] += 1
    elif change == "scope":
        scope["bindings"] = {}
    elif change == "operation":
        scope["bindings"]["employee"]["operations"] = ["click"]
    elif change == "readonly":
        obs.elements[0].read_only = True
    elif change == "disabled":
        obs.elements[0].enabled = False
    elif change == "identity":
        obs.elements[0].name = "Company"
    elif change == "ambiguous":
        obs.elements.append(obs.elements[0].model_copy(update={"id": "duplicate"}))
    elif change == "row":
        obs.elements[0].row_ref = "other"
    elif change == "pending":
        agent.pending = {"key": "unconfirmed"}
    elif change == "unknown_write":
        agent.memory.pending_writes["unknown"] = {"dispatch_status": "unknown"}
    elif change == "stale_observation":
        action.observation_id = "old"
    elif change == "stale_document":
        action.document_version = "old"
    elif change == "tab":
        action.tab_id = "other"
    elif change == "frame":
        action.frame_id = "other"
    elif change == "loading":
        obs.loading = True
    elif change == "fresh_scope":
        agent.fresh_scope_required = True
    elif change == "permission":
        agent.task.allowed_operations = [Operation.WAIT]
    assert agent.scoped_planned_input(action, obs, obs.elements[0]) is None
    agent.backend.execute.assert_not_awaited()


async def test_select_requires_current_observed_option_even_with_exact_stage_value():
    agent, obs, action = await setup("EMP-008", select=True)
    obs.elements[0].options = ["EMP-007"]
    with pytest.raises(InvalidInputValue) as caught:
        await agent.bind_input(action, obs)
    assert caught.value.diagnostic == "unobserved_select_option"
    assert action.bound_value is None
    agent.feedback_model.value.assert_not_awaited()
    agent.backend.execute.assert_not_awaited()


async def test_missing_or_duplicate_field_value_keeps_helper_path():
    agent, obs, action = await setup("Ananya")
    agent.memory.feedback["inputs"] *= 2
    assert agent.scoped_planned_input(action, obs, obs.elements[0]) is None
    await agent.bind_input(action, obs)
    agent.feedback_model.value.assert_awaited_once()
    assert agent.events[-1].get("input_model_call_skipped") is not True
