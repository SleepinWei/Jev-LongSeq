"""A DS stage entry replaces selection once, never readback or input binding."""
from unittest.mock import AsyncMock

import pytest

from jev_browser.dynamic import (
    DynamicController,
    Feedback,
    StageControl,
    StageEntry,
)
from jev_browser.models import JsonPolicy
from jev_browser.protocol import Budget, Decision, Element, Observation, Operation, Receipt, Task


def page():
    return Observation(observation_id="o", document_version="v", tab_id="tab", url="about:blank",
        title="Draft", text="New record", elements=[
            Element(id="name", role="textbox", name="Name", editable=True),
            Element(id="save", role="button", name="Save")])


def task():
    return Task(id="entry-delivery", sandbox=True, control_mode="dynamic",
                objective='Create a record named "requested name".')


async def controller(operation="fill", ref="name", **kwargs):
    brain = AsyncMock()
    brain.review.return_value = Feedback(next_goal="Fill name then Save", working_memory="Other work pending",
        inputs=[{"name": "Name", "value": "requested name"}],
        stage_controls=[StageControl(element_ref="name", operations=["fill"]),
                        StageControl(element_ref="save", operations=["click"])],
        stage_entry=StageEntry(intent="act", operation=operation, element_ref=ref))
    brain.value.return_value = "requested name"
    agent = DynamicController(task(), AsyncMock(), JsonPolicy(AsyncMock()), feedback=brain, **kwargs)
    await agent.review(page(), phase="initial")
    return agent


async def test_fill_entry_binds_validated_plan_without_another_value_request():
    agent = await controller()
    obs = page()
    candidates = agent.generate_stage_candidates(obs, limit=250, offset=0)
    decision, candidates, _ = await agent.choose_with_context_pages(obs, candidates, limit=250, offset=0)
    selected = next(a for a in candidates if a.id == decision.choice)
    assert selected.operation == Operation.FILL and selected.bound_value is None
    agent.policy.transport.post.assert_not_awaited()
    await agent.bind_input(selected, obs)
    assert selected.bound_value == "requested name"
    agent.feedback_model.value.assert_not_awaited()
    assert agent.events[-1]["source"]["kind"] == "validated_stage_input"
    assert agent.stage_entry_decision(obs, candidates) is None
    assert not agent.memory.confirmed_writes and not agent.memory.write_checkpoints


async def test_dynamic_loop_dispatches_entry_without_policy_and_retains_pending_save():
    agent = await controller("click", "save", budget=Budget(max_cycles=1))
    agent.backend.observe.return_value = page()
    agent.backend.execute.return_value = Receipt(action_id="save", status="ok")
    result = await agent.dynamic_loop()
    assert result.status == "budget_exhausted"
    agent.policy.transport.post.assert_not_awaited()
    agent.backend.execute.assert_awaited_once()
    assert agent.pending["click_target"]["name"] == "Save"
    assert agent.memory.pending_writes and not agent.memory.write_checkpoints


@pytest.mark.parametrize("change", ["observation", "version", "semantic", "environment", "generation",
                                   "pending", "orphan", "handoff", "loading", "scope", "consumed", "policy"])
async def test_ticket_cannot_override_safety_boundaries_or_changed_frame(change):
    agent = await controller("click", "save")
    obs = page()
    candidates = agent.generate_stage_candidates(obs, limit=250, offset=0)
    if change == "observation":
        obs.observation_id = "new"
    elif change == "version":
        obs.document_version = "new"
    elif change == "semantic":
        obs.text = "Unexpected error"
    elif change == "environment":
        agent.memory.environment_id = "other"
    elif change == "generation":
        agent.memory.feedback["execution_scope"]["generation"] += 1
    elif change == "pending":
        agent.pending = {"key": "unknown"}
    elif change == "orphan":
        agent.memory.pending_writes["unknown"] = {}
    elif change == "handoff":
        agent.memory.feedback["planning_handoff"] = {"environment_id": agent.memory.environment_id}
    elif change == "loading":
        obs.loading = True
    elif change == "scope":
        agent.memory.feedback["execution_scope"]["bindings"] = {}
    elif change == "consumed":
        from jev_browser.dynamic import action_key
        agent.consumed.add(action_key(next(a for a in candidates if a.element_ref == "save"), obs))
    elif change == "policy":
        agent.policy = AsyncMock()  # Jev does not opt in.
    assert agent.stage_entry_decision(obs, candidates) is None
    agent.backend.execute.assert_not_awaited()


async def test_spent_or_ambiguous_entry_falls_back_to_policy():
    agent = await controller()
    obs = page()
    candidates = agent.generate_stage_candidates(obs, limit=250, offset=0)
    unbound = next(a for a in candidates if a.element_ref == "name" and a.bound_value is None)
    assert agent.stage_entry_decision(obs, [*candidates, unbound.model_copy(update={"id": "duplicate"})]) is None
    assert agent.stage_entry_decision(obs, candidates)
    agent.policy.choose = AsyncMock(return_value=Decision(choice=unbound.id))
    await agent.choose_with_context_pages(obs, candidates, limit=250, offset=0)
    agent.policy.choose.assert_awaited_once()


async def test_local_readback_does_not_issue_another_entry_ticket():
    agent = await controller()
    obs = page()
    candidates = agent.generate_stage_candidates(obs, limit=250, offset=0)
    assert agent.stage_entry_decision(obs, candidates)
    feedback = Feedback(next_goal="Population confirmed", last_outcome="confirmed")
    feedback._local_readback = True
    agent.feedback_model.review.return_value = feedback
    await agent.review(obs, phase="action_readback")
    assert agent.stage_entry_decision(obs, candidates) is None


async def test_delivered_entry_uses_exact_plan_even_if_helper_would_return_a_different_value():
    agent = await controller()
    obs = page()
    candidates = agent.generate_stage_candidates(obs, limit=250, offset=0)
    decision = agent.stage_entry_decision(obs, candidates)
    selected = next(a for a in candidates if a.id == decision.choice)
    agent.feedback_model.value.return_value = "wrong record"
    agent.feedback_model.repair_value.return_value = "wrong record"
    await agent.bind_input(selected, obs)
    assert selected.bound_value == "requested name"
    agent.feedback_model.value.assert_not_awaited()
    agent.feedback_model.repair_value.assert_not_awaited()
    agent.backend.execute.assert_not_awaited()
