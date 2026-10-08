"""A hidden-reasoning length failure changes the single repair's token allowance."""
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from jev_browser.dynamic import DynamicController, InvalidFeedbackOutput, JsonFeedback
from jev_browser.models import ModelTransport
from jev_browser.protocol import Observation, Task


@pytest.mark.parametrize("failure,twice,expected", [("length", False, [4096, 8192]),
                                                 ("length", True, [4096, 8192]),
                                                 ("schema", False, [4096, 4096])])
async def test_only_length_escalates_one_repair_without_releasing_pending(failure, twice, expected):
    payloads = []
    task = Task(id="repair", sandbox=True, control_mode="dynamic", objective="Save the requested record")
    obs = Observation(observation_id="current", document_version="v2", url="about:blank",
                      tab_id="tab", title="Records", text="Records; current rows do not prove the save")

    def respond(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        context = json.loads(payload["messages"][1]["content"])
        assert context["trusted_goal"] == task.objective
        assert context["current_page"]["observation_id"] == obs.observation_id
        assert context["last_transition"]["dispatch_status"] == "ok"
        if len(payloads) == 1 or twice:
            if failure == "length":
                return httpx.Response(200, json={"choices": [{"finish_reason": "length",
                    "message": {"content": "", "reasoning_content": "Unfinished analysis"}}]})
            content = '{"last_outcome":"unknown","value":"forbidden extra field"}'
        else:
            content = '{"last_outcome":"unknown","evidence_ids":[]}'
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = ModelTransport("https://test.example", "test", "test", client=client)
        transport.required_goal = task.objective
        agent = DynamicController(task, AsyncMock(), None, feedback=JsonFeedback(transport))
        pending = {"key": "original-save", "dispatch_status": "ok", "resolved": False,
                   "action": {"operation": "click"}, "before_semantics": "old-frame"}
        agent.pending = pending
        agent.memory.pending_writes[pending["key"]] = pending
        agent.consumed.add(pending["key"])
        if twice:
            with pytest.raises(InvalidFeedbackOutput):
                await agent.review(obs, phase="action_readback")
        else:
            result = await agent.review(obs, phase="action_readback")
            assert result.last_outcome == "unknown"
        assert [p["max_tokens"] for p in payloads] == expected
        first, second = [json.loads(p["messages"][1]["content"]) for p in payloads]
        assert {k: v for k, v in first.items() if k != "schema_error"} == {
            k: v for k, v in second.items() if k != "schema_error"}
        assert agent.feedback_calls == 2 and agent.pending is pending
        assert agent.memory.pending_writes[pending["key"]] is pending
        assert agent.consumed == {pending["key"]} and not agent.memory.confirmed_writes
        agent.backend.execute.assert_not_awaited()


@pytest.mark.parametrize("cursor,accepted", [(None, True), (0, False), (1, False), ("null", False)])
async def test_non_list_null_cursor_needs_no_repair_and_cannot_authorize_pagination(cursor, accepted):
    from pydantic import ValidationError
    from jev_browser.memory import Memory

    task = Task(id="cursor", sandbox=True, control_mode="dynamic", objective="Inspect the visible menu")
    obs = Observation(observation_id="current", document_version="v2", url="about:blank", tab_id="tab",
                      title="Menu", text="Employee Separation")
    calls = []

    def respond(request):
        context = json.loads(json.loads(request.content)["messages"][1]["content"])
        calls.append(context)
        assert set(context["schema"]["properties"]) == {"last_outcome", "evidence_ids"}
        ref = next(iter(context["readback_evidence"]))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content":
            json.dumps({"last_outcome": "confirmed", "evidence_ids": [ref], "next_cursor": cursor})}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        brain = JsonFeedback(ModelTransport("https://test.example", "test", "test", client=client))
        transition = {"dispatch_status": "ok", "action": {"operation": "click"}}
        if accepted:
            result = await brain.readback(task, obs, Memory(), transition)
            assert result.last_outcome == "confirmed" and result.readback_quote == obs.text
            assert result._readback_next_cursor is None and result._local_readback
        else:
            with pytest.raises(ValidationError):
                await brain.readback(task, obs, Memory(), transition)
        assert len(calls) == 1
