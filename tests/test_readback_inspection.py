import copy
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from jev_browser.context_budget import ContextBudgetExceeded
from jev_browser.dynamic import DynamicController, Feedback, JsonFeedback, generate_dynamic
from jev_browser.models import ModelTransport
from jev_browser.protocol import (
    Element,
    GridCell,
    GridRow,
    Observation,
    Receipt,
    Task,
    VisibleGrid,
)


def setup():
    task = Task(id="save-record", objective="Create Ada's vendor then complete other requested work.",
                control_mode="dynamic", allowed_origins=["http://localhost:31005"])
    before = Observation(observation_id="before", document_version="before", tab_id="tab-1",
        url="http://localhost:31005/vendors/new", title="New record", text="Ada Save",
        elements=[Element(id="name", role="textbox", name="Name", value="Ada", editable=True),
                  Element(id="save", role="button", name="Save")])
    after = before.model_copy(update={"observation_id": "after", "document_version": "after",
        "url": "http://localhost:31005/vendors", "text": "Vendors List Other vendor",
        "elements": [Element(id="column", role="button", name="Name", grid_ref="g"),
                     Element(id="new", role="button", name="New Vendor"),
                     Element(id="delete", role="button", name="Delete"),
                     Element(id="filter", role="button", name="Filter")],
        "grids": [VisibleGrid(id="g", rows=[GridRow(key="1", cells=[GridCell(column="Name", value="Other")])])]})
    backend = AsyncMock()
    backend.execute.return_value = Receipt(action_id="test", status="ok")
    backend.observe.return_value = after
    return task, before, after, backend


class Inspector:
    async def inspect_readback(self, task, obs, transition, candidates):
        return next(a.id for a in candidates if a.element_ref == "column")

    async def review(self, task, obs, memory, **kwargs):
        outcome = "confirmed" if "Ada" in obs.text else "unknown"
        result = Feedback(next_goal="Preserved stage", last_outcome=outcome,
                          readback_quote="Ada" if outcome == "confirmed" else "Vendors List")
        result._local_readback = True
        return result


async def saved_controller():
    task, before, after, backend = setup()
    agent = DynamicController(task, backend, None, feedback=Inspector())
    await agent.perform(next(a for a in generate_dynamic(before, task) if a.element_ref == "save"), before)
    return agent, after, backend


async def test_overflow_inspection_finds_record_without_replaying_or_confirming_from_sort():
    agent, after, backend = await saved_controller()
    original = agent.pending
    overflow = ContextBudgetExceeded("protected context too large")
    assert await agent.readback_after_policy_overflow(after, overflow) is None
    assert agent.pending is original and agent.memory.pending_writes[original["key"]] is original
    assert not agent.memory.confirmed_writes and not agent.memory.write_checkpoints
    assert [call.args[0].element_ref for call in backend.execute.await_args_list] == ["save", "column"]
    fresh = after.model_copy(update={"observation_id": "sorted", "text": "Vendors List Ada"})
    assert await agent.readback_after_policy_overflow(fresh, overflow) is None
    assert agent.pending is None and len(agent.memory.write_checkpoints) == 1
    assert agent.memory.write_checkpoints[0]["source"]["observation_id"] == "sorted"
    assert backend.execute.await_count == 2


@pytest.mark.parametrize("change", ["unknown", "same_form", "other_origin", "other_tab", "editable",
                                   "dialog", "loading", "new_error", "second_pending", "no_fields"])
async def test_inspection_requires_successful_dispatch_and_readonly_result_list(change):
    agent, after, backend = await saved_controller()
    if change == "unknown":
        agent.pending["dispatch_status"] = "unknown"
    elif change == "same_form":
        after.url = agent.pending["before"]["url"]
    elif change == "other_origin":
        after.url = "https://elsewhere.example/vendors"
    elif change == "other_tab":
        after.tab_id = "tab-2"
    elif change == "editable":
        after.elements.append(Element(id="field", role="textbox", name="Edit", editable=True))
    elif change == "dialog":
        after.dialogs = ["Confirm delete?"]
    elif change == "loading":
        after.loading = True
    elif change == "new_error":
        after.errors = ["page_error: save failed"]
    elif change == "second_pending":
        agent.memory.pending_writes["another"] = {}
    else:
        agent.pending["field_snapshot"] = []
    assert not agent.pending_write_inspections(after)
    assert not await agent.inspect_pending_write(after)
    assert backend.execute.await_count == 1


async def test_inspections_are_finite_exclude_writes_and_retain_unknown_receipts():
    agent, after, backend = await saved_controller()
    candidates = agent.pending_write_inspections(after)
    assert {a.element_ref for a in candidates} == {None, "column"}
    original = agent.pending
    backend.execute.return_value = Receipt(action_id="sort", status="unknown")
    assert not await agent.inspect_pending_write(after)
    assert agent.pending is original and not agent.memory.confirmed_writes
    assert not any(a.element_ref == "column" for a in agent.pending_write_inspections(after))
    original["readback_inspections"] *= 4
    assert not agent.pending_write_inspections(after)
    assert backend.execute.await_count == 2


async def test_inspector_only_receives_exact_current_evidence_and_original_goal():
    agent, after, _ = await saved_controller()
    pending = copy.deepcopy(agent.pending)
    candidates = agent.pending_write_inspections(after)

    async def respond(request):
        payload = json.loads(request.content)
        content = json.loads(payload["messages"][-1]["content"])
        assert content["trusted_goal"] == agent.task.objective
        assert "untrusted_memory" not in content
        assert "Other vendor" in content["readback_evidence"]
        assert content["last_transition"]["field_snapshot"] == pending["field_snapshot"]
        assert content["schema"]["properties"]["choice"]["enum"] == ["stop", *[a.id for a in candidates]]
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"choice":"stop"}'}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        brain = JsonFeedback(ModelTransport("https://test.example", "test", "test", client=client))
        assert await brain.inspect_readback(agent.task, after, agent.pending, candidates) == "stop"
    assert agent.pending == pending


async def test_missing_record_stops_after_four_fresh_inspections_without_claiming_success():
    class ScrollInspector(Inspector):
        async def inspect_readback(self, task, obs, transition, candidates):
            return next(a.id for a in candidates if a.bound_value == "down")

    agent, after, backend = await saved_controller()
    agent.feedback_model = ScrollInspector()
    original = agent.pending
    overflow = ContextBudgetExceeded("protected context")
    for i in range(4):
        fresh = after.model_copy(update={"observation_id": str(i), "text": f"Vendors List Other {i}"})
        backend.observe.return_value = fresh
        assert await agent.readback_after_policy_overflow(fresh, overflow) is None
    last = after.model_copy(update={"observation_id": "final", "text": "Vendors List Still missing"})
    result = await agent.readback_after_policy_overflow(last, overflow)
    assert result.status == "needs_attention"
    assert agent.pending is original and agent.memory.pending_writes[original["key"]] is original
    assert not agent.memory.write_checkpoints and not agent.memory.confirmed_writes
    assert backend.execute.await_count == 5  # Original Save plus four inspections, never another Save.


async def test_unchanged_refresh_rebinds_inspection_to_current_backend_observation():
    agent, after, backend = await saved_controller()
    current = after
    observations = 0

    async def observe():
        nonlocal current, observations
        observations += 1
        current = after.model_copy(update={"observation_id": f"fresh-{observations}"})
        return current

    async def execute(action):
        assert action.observation_id == current.observation_id  # Actual browser backend rule.
        return Receipt(action_id=action.id, status="ok")

    backend.observe.side_effect = observe
    backend.execute.side_effect = execute
    assert await agent.readback_after_policy_overflow(after, ContextBudgetExceeded("large")) is None
    assert observations == 2  # Unknown refresh plus the inspection's own current binding.
    assert backend.execute.await_args.args[0].observation_id == "fresh-2"
    assert agent.pending and not agent.memory.confirmed_writes


async def test_new_record_during_inspection_refresh_gets_readback_before_any_sort():
    agent, after, backend = await saved_controller()
    backend.observe.return_value = after.model_copy(update={"observation_id": "fresh", "text": "Vendors List Ada"})
    assert await agent.inspect_pending_write(after)
    assert backend.execute.await_count == 1
    assert agent.pending and not agent.memory.confirmed_writes
    fresh = backend.observe.return_value
    assert await agent.readback_after_policy_overflow(fresh, ContextBudgetExceeded("large")) is None
    assert agent.pending is None
    assert backend.execute.await_count == 1  # The original Save only.
