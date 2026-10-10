"""Uncertain Jev choices escalate before any browser or input-helper side effect."""
from unittest.mock import AsyncMock

import pytest

from jev_browser.dynamic import DynamicController, Feedback
from jev_browser.models import JevPolicy
from jev_browser.protocol import Budget, Element, Observation, Receipt, Task


def page():
    return Observation(observation_id="o1", document_version="v1", tab_id="tab-0",
        url="about:blank", title="Draft", text="Draft", elements=[
            Element(id="bad", role="button", name="Open Link"),
            Element(id="good", role="button", name="Inspect draft"),
            Element(id="field", role="textbox", name="Company", editable=True)])


async def run_choices(choices, *, threshold=None, dispatch_status="unknown"):
    browser = AsyncMock()
    browser.observe.return_value = page()
    browser.execute.side_effect = lambda action: Receipt(action_id=action.id, status=dispatch_status)
    phases, rejected = [], []

    async def review(task, obs, memory, *, phase, **kwargs):
        phases.append(phase)
        rejected.append(memory.context()["execution_feedback"].get("policy_abstention"))
        return Feedback(next_goal="Inspect the draft before navigating.")

    brain = AsyncMock()
    brain.review.side_effect = review
    transport = AsyncMock()
    transport.model, transport.observer = "jev-latest", None
    remaining = iter(choices)

    async def respond(payload, kind):
        operation, target, confidence = next(remaining)
        options = payload["questions"]["action"]["criteria"]
        assert any(v["operation"] == "request_replan" for v in options.values())
        choice = next(k for k, v in options.items()
                      if v["operation"] == operation and (not target or v.get("target") == target))
        answers = {"action": {"type": "choice", "choice": choice, "confidence": confidence}}
        if "outcome" in payload["questions"]:
            answers["outcome"] = {"type": "choice", "choice": "pending"}
        return {"answers": answers}

    transport.post.side_effect = respond
    policy = JevPolicy(transport, context_max_bytes=200000)
    controller = DynamicController(Task(id="draft", control_mode="dynamic", sandbox=True,
        objective="Inspect draft"), browser, policy, feedback=brain,
        budget=Budget(max_cycles=len(choices), confidence_threshold=threshold))
    await controller.run()
    return controller, browser, brain, phases, rejected


@pytest.mark.parametrize("operation,target", [("click", "bad"), ("fill", "field")])
@pytest.mark.parametrize("confidence", [0.29, None])
async def test_uncertain_action_consults_brain_before_dispatch_or_input(operation, target, confidence):
    agent, browser, brain, phases, rejected = await run_choices([
        (operation, target, confidence), ("click", "good", 0.9)])
    assert phases == ["initial", "low_confidence"]
    assert browser.execute.await_count == 1
    assert browser.execute.await_args.args[0].element_ref == "good"
    brain.value.assert_not_awaited()
    assert rejected[1]["element_ref"] == target
    assert rejected[1]["confidence"] == confidence
    assert rejected[1]["browser_action_dispatched"] is False
    assert rejected[1]["threshold"] == 0.5
    assert agent.actions == 1


async def test_explicit_no_action_asks_brain_even_at_low_confidence():
    agent, browser, _, phases, rejected = await run_choices([
        ("request_replan", None, 0.1), ("click", "good", 0.9)])
    assert phases == ["initial", "jev_requested"]
    assert rejected[1]["operation"] == "request_replan"
    assert browser.execute.await_count == agent.actions == 1


async def test_confident_action_at_threshold_can_execute():
    _, browser, _, phases, _ = await run_choices([("click", "good", 0.5)])
    assert phases == ["initial"]
    assert browser.execute.await_count == 1


async def test_explicit_threshold_is_respected():
    _, browser, _, phases, _ = await run_choices([
        ("click", "bad", 0.6), ("click", "good", 0.9)], threshold=0.7)
    assert phases == ["initial", "low_confidence"]
    assert browser.execute.await_count == 1


async def test_repeated_abstention_is_bounded_and_never_dispatches():
    agent, browser, _, phases, _ = await run_choices([("click", "bad", 0.29)] * 3)
    assert phases == ["initial", "low_confidence", "low_confidence"]
    browser.execute.assert_not_awaited()
    assert agent.actions == 0
    assert not agent.pending and not agent.consumed


async def test_abstention_preserves_prior_unconfirmed_write():
    agent, browser, _, _, _ = await run_choices([
        ("click", "bad", 0.9), ("click", "good", 0.29)], dispatch_status="ok")
    assert browser.execute.await_count == 1
    assert agent.pending and agent.memory.pending_writes
    event = next(e for e in agent.events if e["kind"] == "policy_abstained")
    assert event["pending_preserved"] is True
    assert event["browser_action_dispatched"] is False
