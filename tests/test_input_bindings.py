import json
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from jev_browser.context_budget import ContextBudgetExceeded
from jev_browser.dynamic import (
    DynamicController,
    JsonFeedback,
    generate_dynamic,
)
from jev_browser.input_bindings import quoted_inputs
from jev_browser.memory import Memory
from jev_browser.models import ModelTransport
from jev_browser.protocol import Budget, Element, Observation, Operation, Receipt, Task


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


def input_trial(responses, *, budget=None, select=False):
    task, obs = setup()
    if select:
        obs.elements = [Element(id="e0", role="combobox", name="Plan", selectable=True,
                                options=["gold", "basic"])]
    backend = AsyncMock()
    backend.execute.return_value = Receipt(action_id="a", status="ok")
    transport = AsyncMock()
    transport.model = "test"
    transport.post.side_effect = responses
    agent = DynamicController(task, backend, None, feedback=JsonFeedback(transport), budget=budget)
    agent.memory.feedback["working_memory"] = "Keep the unresolved work"
    operation = Operation.SELECT if select else Operation.FILL
    action = next(a for a in generate_dynamic(obs, task)
                  if a.operation == operation and a.bound_value is None)

    async def loop():
        await agent.bind_input(action, obs)
        await agent.perform(action, obs)
        return agent.result("budget_exhausted", "test complete")

    agent.dynamic_loop = loop
    return agent, backend, transport, action, task


def input_response(raw, finish="stop"):
    return {"choices": [{"message": {"content": raw}, "finish_reason": finish}]}


async def test_truncated_input_repairs_once_before_exactly_one_dispatch():
    agent, backend, transport, action, task = input_trial([
        input_response('{"value":"private unfinished text'),
        input_response('{"value":"Ada"}'),
    ])
    result = await agent.run()
    assert result.feedback_calls == 2
    assert backend.execute.await_count == 1 and action.bound_value == "Ada"
    requests = [call.args[0] for call in transport.post.await_args_list]
    contexts = [json.loads(r["messages"][-1]["content"]) for r in requests]
    assert all(r["max_tokens"] == 8192 for r in requests)
    assert all(c["trusted_goal"] == task.objective for c in contexts)
    assert all(c["untrusted_memory"]["working_memory"] == "Keep the unresolved work"
               for c in contexts)
    diagnostic = contexts[1].pop("repair_diagnostic")
    assert diagnostic == ["json_invalid"] and contexts[0] == contexts[1]
    assert "private unfinished text" not in json.dumps(agent.events)


@pytest.mark.parametrize("raw,finish", [
    ('{"value":"unfinished', "stop"),
    ('{"value":null}', "stop"),
    ('{"value":42}', "stop"),
    ('{"value":[]}', "stop"),
    ('{"value":"Ada","extra":"secret"}', "stop"),
    ('{}', "stop"),
    ('```json\n{"value":"Ada"}\n```', "stop"),
    ('{"value":"Ada"}', "length"),
    (None, "stop"),
])
async def test_persistently_invalid_input_stops_without_dispatch(raw, finish):
    response = input_response(raw, finish)
    agent, backend, transport, action, _ = input_trial([response, response])
    result = await agent.run()
    assert result.status == "needs_attention" and "one repair" in result.reason
    assert transport.post.await_count == result.feedback_calls == 2
    backend.execute.assert_not_awaited()
    assert action.bound_value is None and agent.pending is None
    assert not agent.memory.pending_writes


async def test_input_repair_respects_remaining_call_budget():
    agent, backend, transport, action, _ = input_trial([
        input_response('{"value":null}'), input_response('{"value":"Ada"}'),
    ], budget=Budget(max_feedback_calls=1))
    result = await agent.run()
    assert result.status == "budget_exhausted" and result.feedback_calls == 1
    assert transport.post.await_count == 1 and action.bound_value is None
    backend.execute.assert_not_awaited()


async def test_unobserved_select_option_is_repaired_before_dispatch():
    agent, backend, transport, action, _ = input_trial([
        input_response('{"value":"Gold"}'), input_response('{"value":"gold"}'),
    ], select=True)
    await agent.run()
    assert backend.execute.await_count == 1 and action.bound_value == "gold"
    context = json.loads(transport.post.await_args_list[1].args[0]["messages"][-1]["content"])
    assert context["repair_diagnostic"] == "unobserved_select_option"


async def test_context_overflow_is_never_retried_as_input_format_error():
    _, obs = setup()
    agent, backend, transport, action, _ = input_trial([])
    transport.post.side_effect = ContextBudgetExceeded({"protected_bytes": 1000})
    with pytest.raises(ContextBudgetExceeded):
        await agent.bind_input(action, obs)
    assert transport.post.await_count == 1 and agent.feedback_calls == 1
    backend.execute.assert_not_awaited()
    assert not any(e["kind"] == "invalid_input_value" for e in agent.events)


async def test_transport_records_only_safe_output_metadata_and_preserves_goal_guard():
    task, obs = setup()
    raw = '{"value":"Ada"}'

    def respond(request):
        return httpx.Response(200, json=input_response(raw))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = ModelTransport("https://model.test", "test", "test", client=client)
        transport.required_goal = task.objective
        feedback = JsonFeedback(transport)
        action = next(a for a in generate_dynamic(obs, task)
                      if a.operation == Operation.FILL and a.bound_value is None)
        assert await feedback.value(task, obs, Memory(), action) == "Ada"
        assert transport.ledger[0]["finish_reason"] == "stop"
        assert transport.ledger[0]["response_content_chars"] == len(raw)
        assert raw not in json.dumps(transport.ledger)
        changed = task.model_copy(update={"objective": "Changed goal"})
        with pytest.raises(ValueError, match="changed the original task prompt"):
            await feedback.value(changed, obs, Memory(), action, diagnostic=["json_invalid"])
        assert len(transport.ledger) == 1
