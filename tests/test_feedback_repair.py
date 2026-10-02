"""Readback reference repair must retain unknown writes and never crash or replay."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from jev_browser.dynamic import (
    DynamicController,
    InvalidFeedbackOutput,
    JsonFeedback,
    UngroundedFeedback,
    generate_dynamic,
)
from jev_browser.models import ModelTransport
from jev_browser.protocol import Element, Observation, Receipt, Task


async def pending_save(brain):
    task = Task(id="repair", sandbox=True, control_mode="dynamic", objective="Create the vendor")
    before = Observation(observation_id="before", document_version="v1", tab_id="tab",
        url="about:blank", title="Vendor", text="Not Saved", elements=[
            Element(id="save", role="button", name="Save")])
    backend = AsyncMock()
    backend.execute.return_value = Receipt(action_id="save", status="ok")
    controller = DynamicController(task, backend, AsyncMock(), feedback=brain)
    controller.memory.feedback = {"next_goal":"Save the vendor", "working_memory":"Remaining journal work"}
    save = next(a for a in generate_dynamic(before, task) if a.element_ref == "save")
    await controller.perform(save, before)
    after = before.model_copy(update={"observation_id":"after", "document_version":"v2",
        "text":"The vendor has been successfully created.", "elements":[]})
    return controller, after


async def test_reference_enum_and_targeted_repair_resolve_a_real_current_quote():
    contexts = []

    def respond(request):
        context = json.loads(json.loads(request.content)["messages"][-1]["content"])
        contexts.append(context)
        assert context["schema"]["properties"]["evidence_ids"]["items"]["enum"] == list(context["readback_evidence"])
        if len(contexts) == 1:
            refs = ["invented-old-reference"]
        else:
            assert context["schema_error"][0]["invalid_refs"] == ["invented-old-reference"]
            refs = [next(k for k, v in context["readback_evidence"].items()
                         if v == "The vendor has been successfully created.")]
        return httpx.Response(200, json={"choices":[{"message":{"content":json.dumps({
            "last_outcome":"confirmed", "evidence_ids":refs})}, "finish_reason":"stop"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        brain = JsonFeedback(ModelTransport("https://model.test", "test-key", "test", client=client))
        controller, after = await pending_save(brain)
        feedback = await controller.review(after, phase="action_readback")
        assert controller.confirm_transition(feedback.last_outcome, after, "fresh_model_readback")
        assert not controller.pending and len(contexts) == 2
        controller.backend.execute.assert_awaited_once()
    finally:
        await client.aclose()


async def test_two_invalid_reference_responses_stop_with_pending_instead_of_none_attribute_crash():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"choices":[{"message":{"content":json.dumps({
            "last_outcome":"confirmed", "evidence_ids":["invented-ref"]})}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        brain = JsonFeedback(ModelTransport("https://model.test", "test-key", "test", client=client))
        controller, after = await pending_save(brain)

        async def loop():
            return await controller.review(after, phase="action_readback")

        controller.dynamic_loop = loop
        result = await controller.run()
        assert result.status == "needs_attention" and "no resubmission" in result.reason
        assert controller.pending and controller.memory.pending_writes
        assert not controller.memory.confirmed_writes and len(calls) == 2
        assert controller.memory.feedback["last_outcome"] == "unknown"
        controller.backend.execute.assert_awaited_once()
        assert any(e["kind"] == "feedback_readback_unresolved" for e in controller.events)
    finally:
        await client.aclose()


async def test_invalid_finish_without_feedback_is_not_downgraded_to_local_readback():
    brain = AsyncMock()
    brain.review.side_effect = UngroundedFeedback([{"loc":["evidence_ids"], "type":"unknown_current_evidence_ref"}])
    controller = DynamicController(Task(id="finish", sandbox=True, control_mode="dynamic",
        objective="Read current page"), AsyncMock(), AsyncMock(), feedback=brain)
    obs = Observation(observation_id="fresh", document_version="v", tab_id="tab",
                      url="about:blank", title="Page", text="Page")
    with pytest.raises(InvalidFeedbackOutput):
        await controller.review(obs, phase="finish")
    assert not controller.memory.feedback.get("complete") and brain.review.await_count == 2
