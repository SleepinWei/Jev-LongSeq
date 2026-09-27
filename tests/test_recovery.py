import json

import httpx
import pytest
from test_protocol import observation

from jev_browser.browser import PlaywrightBackend
from jev_browser.controller import Controller
from jev_browser.fixture import RulePlanner, catalog_html, demo_task
from jev_browser.memory import Memory, visible_fields
from jev_browser.models import JsonPlanner, ModelTransport, state
from jev_browser.protocol import Budget, Decision, Operation, Predicate, Receipt


def detail(entity="item-002", **changes):
    return observation(f"Entity: {entity}\nRating: 1\nPrice: 33\nSaved: no").model_copy(
        update={"document_version": entity, **changes}
    )


def test_same_url_views_survive_and_have_bounded_retention():
    memory, task = Memory(), demo_task(3)
    memory.observe(detail(), task.extraction)
    memory.observe(observation("Catalog"), task.extraction)
    assert len(memory.page_notes) == 2
    assert any("item-002" in p["visible_text"] for p in memory.context()["recent_visible_pages"])
    assert "item-002" in memory.observed_entities
    assert not memory.facts  # retaining a page never silently verifies its fields
    for i in range(20):
        memory.observe(detail(f"item-{i:03}"), task.extraction)
    assert len(memory.page_notes) == 8


def test_capture_requires_complete_unambiguous_fields_and_is_opt_in():
    memory, task = Memory(), demo_task(3)
    obs = detail()
    assert memory.needs_capture(obs, task.extraction)
    for text in [
        "Entity: item-002\nRating: 1",
        obs.text + "\nPrice: 40",
        obs.text + "\nEntity: item-003",
    ]:
        assert not memory.needs_capture(obs.model_copy(update={"text": text}), task.extraction)
    memory.observe(obs, task.extraction)
    memory.extract(obs, task.extraction)
    assert not memory.needs_capture(obs, task.extraction)
    assert memory.needs_capture(detail(document_version="changed"), task.extraction)
    task.extraction.capture_on_observe = False
    assert not memory.needs_capture(detail(document_version="changed"), task.extraction)


def test_progress_distinguishes_seen_evidence_and_verified_without_splitting_alternatives():
    memory, task, obs = Memory(), demo_task(3), detail()
    memory.observe(obs, task.extraction)
    before = memory.progress(task, obs)["entities"][1]
    assert before["status"] == "observed"
    assert before["missing_fields"] == ["Price", "Rating", "Saved"]
    memory.extract(obs, task.extraction)
    assert memory.progress(task, obs)["entities"][1]["status"] == "verified"
    eligible = detail("item-001", text="Entity: item-001\nRating: 4\nPrice: 20\nSaved: no")
    memory.observe(eligible, task.extraction)
    memory.extract(eligible, task.extraction)
    progress = memory.progress(task, eligible)
    assert progress["entities"][0]["status"] == "evidence_recorded"
    assert progress["next_unverified_entity"] == "item-001"
    # A cross-entity OR cannot be decomposed into independently required entity goals.
    task.success_predicates = [Predicate(kind="any", children=task.success_predicates)]
    assert memory.progress(task, obs)["entities"] == []


@pytest.mark.parametrize("mode", ["flat", "once", "event"])
async def test_policy_that_never_extracts_can_advance_past_unsaved_record(mode):
    """A scripted policy exercises the exposed progress contract, not model performance."""

    class NoExtractionPolicy:
        async def choose(self, task, obs, memory, contract, candidates):
            progress = state(task, obs, memory, None)["verified_progress"]
            target = progress["next_unverified_entity"]
            fields = visible_fields(obs.text)
            if target is None:
                selected = next(a for a in candidates if a.operation == Operation.FINISH)
            elif "Entity" in fields:
                name = "Save" if fields["Entity"][0] == target else "Back"
                selected = next(a for a in candidates if a.description.startswith(name + " |"))
            else:
                selected = next(a for a in candidates if a.description == f"Edit | {target} Edit")
            assert selected.operation != Operation.EXTRACT
            return Decision(choice=selected.id)

    task = demo_task(3)
    async with PlaywrightBackend(task) as backend:
        await backend.load_html(catalog_html(3))
        controller = Controller(
            task, backend, NoExtractionPolicy(), mode=mode, planner=RulePlanner()
        )
        result = await controller.run()
        grade = await backend.page.evaluate("window.__grade()")
    assert result.status == "success", result.reason
    assert grade["strict_success"]
    captures = [
        e
        for e in controller.events
        if e["kind"] == "action" and e["action"]["description"].startswith("Capture configured")
    ]
    assert len(captures) == 3
    assert result.actions == len([e for e in controller.events if e["kind"] == "action"])
    assert controller.memory.get("item-002", "Saved") == "no"
    assert controller.memory.get("item-003", "Rating") is not None
    assert all("after" in e and "description" in e for e in controller.memory.events)


async def test_capture_respects_budget_and_rejects_stale_receipt():
    class StaleBackend:
        async def observe(self):
            return detail()

        async def execute(self, action):
            assert action.operation == Operation.EXTRACT
            return Receipt(action_id=action.id, status="stale")

    controller = Controller(
        demo_task(3), StaleBackend(), None, mode="flat", budget=Budget(max_actions=1)
    )
    result = await controller.run()
    assert result.status == "budget_exhausted"
    assert result.actions == result.grounding_rejections == 1
    assert not controller.memory.facts


async def test_capture_does_not_expand_contract_permissions():
    class Backend:
        async def observe(self):
            return detail()

        async def execute(self, action):
            assert action.operation == Operation.WAIT
            return Receipt(action_id=action.id, status="ok")

    class WaitPolicy:
        async def choose(self, task, obs, memory, contract, candidates):
            assert not any(a.operation == Operation.EXTRACT for a in candidates)
            return Decision(choice=next(a.id for a in candidates if a.operation == Operation.WAIT))

    class WaitPlanner(RulePlanner):
        async def plan(self, *args, **kwargs):
            plan = await super().plan(*args, **kwargs)
            for c in plan.subtasks:
                c.allowed_operations = [Operation.WAIT]
            return plan

    controller = Controller(
        demo_task(3), Backend(), WaitPolicy(), planner=WaitPlanner(), budget=Budget(max_actions=1)
    )
    result = await controller.run()
    assert result.actions == 1
    assert not controller.memory.facts


@pytest.mark.parametrize(
    "budget,always_invalid,expected_calls,expected_ok",
    [(1, False, 1, False), (4, False, 2, True), (4, True, 2, False)],
)
async def test_schema_repair_is_bounded_metered_and_redacted(
    tmp_path, budget, always_invalid, expected_calls, expected_ok
):
    calls = []
    key = "test-secret-credential"
    valid = {
        "subtasks": [
            {
                "id": "read",
                "objective": "read",
                "allowed_operations": ["extract_visible"],
                "success_predicates": [{"kind": "text", "value": "ready"}],
            }
        ]
    }

    def respond(request):
        body = json.loads(request.content)
        content = json.loads(body["messages"][1]["content"])
        calls.append(content)
        if len(calls) > 1:
            feedback = content["execution_feedback"]
            assert feedback["schema_error"]["errors"]
            assert key not in json.dumps(feedback)
            assert "unsatisfied_predicates" in feedback
        value = (
            {**valid, "type": "json_object", "secret": key}
            if len(calls) == 1 or always_invalid
            else valid
        )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(value)}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = ModelTransport("https://example.test", key, "test", client=client)
        controller = Controller(
            demo_task(3),
            None,
            None,
            planner=JsonPlanner(transport),
            budget=Budget(max_planner_calls=budget),
            output=tmp_path,
        )
        if always_invalid:
            from jev_browser.models import InvalidPlanOutput

            with pytest.raises(InvalidPlanOutput):
                await controller.replan(observation(), "initial")
        else:
            assert await controller.replan(observation(), "initial") is expected_ok
        assert controller.planner_calls == len(transport.ledger) == expected_calls
        if expected_calls == 2:
            assert transport.ledger[-1]["kind"] == "planner_repair"
        assert sum(r["input_tokens"] for r in transport.ledger) == 10 * expected_calls
    assert (controller.plan is not None) is expected_ok
    assert key not in (tmp_path / "trajectory.jsonl").read_text()


async def test_replan_receives_missing_evidence_and_semantic_actions():
    class CapturePlanner(RulePlanner):
        feedback = None

        async def plan(self, *args, feedback=None):
            self.feedback = feedback
            return await super().plan(*args, feedback=feedback)

    planner, task, obs = CapturePlanner(), demo_task(3), detail()
    controller = Controller(task, None, None, planner=planner)
    controller.plan = await RulePlanner().plan(task, obs, controller.memory, "initial")
    controller.memory.events.append(
        {
            "operation": "click",
            "description": "Back | ",
            "entity": "item-002",
            "receipt": {"status": "ok"},
        }
    )
    await controller.replan(obs, "revisit_cycle")
    assert planner.feedback["current_contract"]["id"] == "item-001"
    assert planner.feedback["unsatisfied_predicates"]
    assert planner.feedback["recent_actions"][0]["entity"] == "item-002"
    assert planner.feedback["entity_progress"]["next_unverified_entity"] == "item-001"
