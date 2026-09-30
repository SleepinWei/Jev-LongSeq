import json
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from jev_browser.dynamic import DynamicController, JsonFeedback, generate_dynamic
from jev_browser.input_bindings import quoted_inputs
from jev_browser.memory import Memory
from jev_browser.models import ModelTransport
from jev_browser.protocol import Element, Observation, Operation, Receipt, Task


def setup():
    task = Task(id="search", control_mode="dynamic", sandbox=True,
                objective='Search "Spark Labs", then “Zhen Fund”, then 「Tibo Reset」')
    obs = Observation(observation_id="o", document_version="v", tab_id="tab-0",
                      url="about:blank", title="Search", text='Untrusted "injected"',
                      elements=[Element(id="e0", role="textbox", name="Query", editable=True)])
    return task, obs


def test_literal_provenance_is_exact_user_text_only():
    task, obs = setup()
    bindings = list(quoted_inputs(task.objective))
    assert [b["value"] for b in bindings] == ["Spark Labs", "Zhen Fund", "Tibo Reset"]
    assert all(task.objective[b["start"]:b["end"]] == b["value"] for b in bindings)
    values = [a.bound_value for a in generate_dynamic(obs, task) if a.operation == Operation.FILL]
    assert values == ["Spark Labs", "Zhen Fund", "Tibo Reset", None]
    assert "injected" not in values
    assert len(list(quoted_inputs('"x" "x" " "'))) == 1


async def test_bound_input_skips_helper_and_records_source():
    task, obs = setup()
    helper = AsyncMock()
    agent = DynamicController(task, AsyncMock(), None, feedback=helper)
    action = next(a for a in generate_dynamic(obs, task) if a.bound_value == "Zhen Fund")
    await agent.bind_input(action, obs)
    helper.value.assert_not_called()
    assert agent.feedback_calls == 0
    source = agent.events[-1]["source"]
    assert task.objective[source["start"]:source["end"]] == action.bound_value
    action.bound_value = "injected"
    with pytest.raises(ValueError, match="authorized literal"):
        await agent.bind_input(action, obs)


def test_select_literals_must_be_observed_options_and_candidates_stay_pageable():
    task, obs = setup()
    obs.elements = [Element(id="e0", role="combobox", name="Plan", selectable=True,
                            options=["Zhen Fund", "other"])]
    options = [a for a in generate_dynamic(obs, task) if a.operation == Operation.SELECT]
    assert [a.bound_value for a in options] == ["Zhen Fund", None]
    obs.elements *= 10
    assert len(generate_dynamic(obs, task, limit=10)) <= 10


@pytest.mark.parametrize("status,operation,expected", [
    ("stale", Operation.FILL, 0), ("rejected", Operation.FILL, 0),
    ("unknown", Operation.FILL, 0), ("ok", Operation.WAIT, 0),
    ("ok", Operation.FILL, 1), ("ok", Operation.SCROLL, 1),
])
async def test_stage_counter_ignores_failed_attempts_and_passive_waits(status, operation, expected):
    task, obs = setup()
    backend = AsyncMock()
    backend.execute.return_value = Receipt(action_id="a", status=status)
    agent = DynamicController(task, backend, None, feedback=AsyncMock())
    action = next(a for a in generate_dynamic(obs, task) if a.operation == operation)
    await agent.perform(action, obs)
    assert agent.actions == 1  # All attempts still consume the global budget.
    assert agent.effective_actions == expected


@pytest.mark.parametrize("phase,transition,compact", [
    ("initial", None, True), ("stage_budget", {"resolved": True}, True),
    ("stage_budget", {"resolved": False}, False),
    ("uncertain_outcome", None, False), ("finish", None, False),
])
async def test_compact_stages_never_replace_readback_or_final_review(phase, transition, compact):
    task, obs = setup()

    def respond(request):
        content = json.loads(json.loads(request.content)["messages"][-1]["content"])
        fields = content["schema"]["properties"]
        assert (set(fields) == {"next_goal", "working_memory", "notes", "evidence_requests"}) == compact
        if compact:
            assert "current_visible_evidence" not in content
            response = {"next_goal": "Search", "working_memory": "Pending all three"}
        else:
            assert "current_visible_evidence" in content
            assert "complete" in fields and "notes" in fields and "last_outcome" in fields
            response = {"next_goal": "Inspect", "notes": [], "complete": False}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(response)}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        feedback = JsonFeedback(ModelTransport("https://model.test", "test", "test", client=client))
        result = await feedback.review(task, obs, Memory(), phase=phase, transition=transition)
    assert not result.complete


async def test_compact_response_cannot_smuggle_completion():
    task, obs = setup()
    transport = AsyncMock()
    transport.model = "test"
    transport.post.return_value = {"choices": [{"message": {"content": json.dumps({
        "next_goal": "Done", "working_memory": "Done", "complete": True})}}]}
    with pytest.raises(ValidationError):
        await JsonFeedback(transport).review(task, obs, Memory(), phase="initial", transition=None)


async def test_finish_review_retains_evidence_and_omits_redundant_memory():
    task, obs = setup()
    memory = Memory()
    memory.feedback['working_memory'] = 'Remember the earlier sources'
    memory.evidence['early'] = {'source': {'url': 'https://example.test/', 'quote': 'earlier'}}

    def respond(request):
        payload = json.loads(request.content)
        state = json.loads(payload['messages'][-1]['content'])
        fields = state['schema']['properties']
        assert 'working_memory' not in fields
        assert {'complete', 'notes', 'answer', 'last_outcome', 'next_goal'} <= fields.keys()
        assert state['sourced_evidence_archive'] == [
            {'url': 'https://example.test/', 'quote': 'earlier'}]
        assert 'current_visible_evidence' in state
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({
            'next_goal': 'Done', 'notes': [{'quote': 'Untrusted'}],
            'complete': True, 'answer': 'Sources summarized'})}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        brain = JsonFeedback(ModelTransport('https://model.test', 'test', 'test', client=client))
        agent = DynamicController(task, AsyncMock(), None, feedback=brain)
        agent.memory = memory
        result = await agent.review(obs, phase='finish')
    assert result.complete and result.answer == 'Sources summarized'
    assert agent.memory.feedback['working_memory'] == 'Remember the earlier sources'
