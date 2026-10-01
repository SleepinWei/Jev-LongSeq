import json
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from jev_browser.browser import PlaywrightBackend
from jev_browser.dynamic import (
    DynamicController,
    Feedback,
    JsonFeedback,
    UngroundedFeedback,
    generate_dynamic,
    validated_feedback,
)
from jev_browser.evaluation import efficiency_profile
from jev_browser.memory import Memory
from jev_browser.models import ModelTransport
from jev_browser.protocol import Budget, Decision, Element, Observation, Operation, Receipt, Task


def task():
    return Task(id="select", control_mode="dynamic", sandbox=True,
                objective="Assign the second activity to Rajesh, then save after verification.")


def observation():
    return Observation(observation_id="o1", document_version="v1", tab_id="tab0",
                       url="about:blank", title="Draft", text="Not Saved", elements=[
                           Element(id="field", role="combobox", name="User", editable=True,
                                   value="rajesh@example.test", grid_ref="grid", row_ref="2",
                                   popup_open=True),
                           Element(id="choice", role="option", name="rajesh@example.test Rajesh",
                                   option_owner="field", grid_ref="grid", row_ref="2")])


@pytest.mark.parametrize("binding", ["inline", "aria", "ambiguous"])
async def test_browser_binds_options_only_to_unique_visible_input(binding):
    html = '''<div class="awesomplete"><label>User<input id="user" role="combobox"
      aria-controls="list" value="rajesh@example.test"></label>
      <ul id="list" role="listbox"><li role="option"
        onclick="document.getElementById('list').style.display='none'">rajesh@example.test Rajesh</li></ul></div>'''
    if binding == "inline":
        html = html.replace('aria-controls="list"', '')
    elif binding == "aria":
        html = html.replace('<ul id="list"', '</div><ul id="list"')
    else:
        html = html.replace('<ul id="list"', '<input role="combobox" aria-controls="list"><ul id="list"')
    async with PlaywrightBackend(task()) as browser:
        await browser.load_html(html)
        before = await browser.observe()
        option = next(e for e in before.elements if e.role == "option")
        field = next(e for e in before.elements if e.name == "User")
        if binding == "ambiguous":
            assert option.option_owner is None
            return
        assert option.option_owner == field.id and field.popup_open is True
        agent = DynamicController(task(), browser, None, feedback=None)
        selected = next(a for a in generate_dynamic(before, task()) if a.element_ref == option.id)
        await agent.perform(selected, before)
        fresh = await browser.observe()
        assert agent.confirm_visible_option(fresh)
        assert agent.last_transition["confirmation_scope"] == "option_selected_ui"
        assert agent.last_transition["business_commit_confirmed"] is False
        assert agent.last_transition["link_resolution_confirmed"] is False
        assert not agent.pending and not agent.memory.feedback.get("complete")
        assert "Local UI only" in agent.memory.feedback["working_memory"]


@pytest.mark.parametrize("case", ["same_frame", "wrong_row", "wrong_grid", "wrong_value",
                                 "replacement", "popup_open", "popup_unknown", "unknown_receipt",
                                 "dialog", "navigation", "runtime_error", "unowned_option"])
async def test_option_readback_rejects_ambiguous_or_unfresh_evidence(case):
    before = observation()
    if case == "unowned_option":
        before.elements[1].option_owner = None
    backend = AsyncMock()
    backend.execute.return_value = Receipt(action_id="a", status="unknown" if case == "unknown_receipt" else "ok")
    agent = DynamicController(task(), backend, None, feedback=None)
    action = next(a for a in generate_dynamic(before, task()) if a.element_ref == "choice")
    await agent.perform(action, before)
    fresh = before.model_copy(deep=True)
    fresh.observation_id = "o2"
    fresh.elements = [fresh.elements[0]]
    field = fresh.elements[0]
    field.popup_open = False
    if case == "same_frame":
        fresh.observation_id = before.observation_id
    if case == "wrong_row":
        field.row_ref = "1"
    if case == "wrong_grid":
        field.grid_ref = "other"
    if case == "wrong_value":
        field.value = "pooja@example.test"
    if case == "replacement":
        field.id = "new"
    if case == "popup_open":
        field.popup_open = True
    if case == "popup_unknown":
        field.popup_open = None
    if case == "dialog":
        fresh.dialogs = ["Save this draft?"]
    if case == "navigation":
        fresh.url = "https://elsewhere.test"
    if case == "runtime_error":
        fresh.errors = ["page_error:failed to select"]
    assert not agent.confirm_visible_option(fresh)
    assert agent.pending and not agent.memory.confirmed_writes
    assert backend.execute.await_count == 1


async def test_light_readback_retains_goal_memory_and_resolves_current_references():
    definition, obs, memory = task(), observation(), Memory()
    memory.dynamic_mode = True
    memory.feedback = {"next_goal": "Continue the draft", "working_memory": "Keep unfinished work"}

    def respond(request):
        payload = json.loads(request.content)
        context = json.loads(payload["messages"][-1]["content"])
        assert context["trusted_goal"] == definition.objective
        assert context["untrusted_memory"]["working_memory"] == "Keep unfinished work"
        assert payload["max_tokens"] == 4096
        assert set(context["schema"]["properties"]) == {"last_outcome", "evidence_ids"}
        ref = next(k for k, v in context["readback_evidence"].items() if v == "Not Saved")
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "last_outcome": "confirmed", "evidence_ids": [ref]})}, "finish_reason": "stop"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = ModelTransport("https://test.example", "test", "test", client=client)
        transport.required_goal = definition.objective
        result = await JsonFeedback(transport).review(definition, obs, memory,
                                                     phase="action_readback", transition={"resolved": False})
    assert result.readback_quote == "Not Saved"
    assert result.working_memory == "Keep unfinished work" and result.next_goal == "Continue the draft"
    assert not result.complete and result.answer == ""
    assert transport.ledger[0]["kind"] == "dynamic_readback"
    profile = efficiency_profile(transport.ledger, actions=1, elapsed_s=1)
    assert profile["by_component"]["brain"]["attempts"] == 1


@pytest.mark.parametrize("response,finish,error", [
    ({"last_outcome": "confirmed", "evidence_ids": ["old-frame"]}, "stop", UngroundedFeedback),
    ({"last_outcome": "confirmed", "evidence_ids": []}, "stop", UngroundedFeedback),
    ({"last_outcome": "confirmed", "evidence_ids": ["v0"], "complete": True}, "stop", ValidationError),
    ({"last_outcome": "confirmed", "evidence_ids": ["v0"]}, "length", ValueError),
])
async def test_light_readback_rejects_missing_proof_and_completion_smuggling(response, finish, error):
    transport = AsyncMock()
    transport.model = "test"
    transport.post.return_value = {"choices": [{"message": {"content": json.dumps(response)},
                                                "finish_reason": finish}]}
    with pytest.raises(error):
        await JsonFeedback(transport).review(task(), observation(), Memory(),
                                            phase="no_progress", transition={"resolved": False})


def test_invalid_advisory_note_does_not_invalidate_control_fields():
    feedback = validated_feedback(json.dumps({"next_goal": "Continue", "last_outcome": "confirmed",
        "readback_quote": "Not Saved", "notes": [
            {"quote": "Not Saved", "interpretation": "Draft", "critical": True},
            {"quote": "User", "interpreation": "typo", "critical": False}]}))
    assert feedback.last_outcome == "confirmed" and feedback.readback_quote == "Not Saved"
    assert len(feedback.notes) == 1 and len(feedback._note_diagnostics) == 1


@pytest.mark.parametrize("override", [{"complete": True}, {"last_outcome": "success"},
                                      {"action": "submit"}, {"complete": "true"}])
def test_advisory_note_isolation_never_relaxes_completion_or_control_schema(override):
    with pytest.raises(ValidationError):
        validated_feedback(json.dumps({"next_goal": "Continue", "notes": [
            {"quote": "User", "interpreation": "typo"}], **override}))


def test_invalid_critical_note_is_not_discarded():
    with pytest.raises(ValidationError):
        validated_feedback(json.dumps({"next_goal": "Continue", "notes": [
            {"quote": "User", "interpreation": "typo", "critical": True}]}))


async def test_pending_readback_uses_its_own_budget_before_generic_no_progress():
    obs = observation()
    obs.elements[1].option_owner = None  # Unknown ownership cannot use the local shortcut.
    backend = AsyncMock()
    backend.observe.return_value = obs
    backend.execute.return_value = Receipt(action_id="a", status="ok")
    phases = []

    class Brain:
        async def review(self, *args, phase, **kwargs):
            phases.append(phase)
            return Feedback(next_goal="Select the observed user", last_outcome="pending")

    class Policy:
        async def choose(self, task, obs, memory, contract, candidates):
            operation = Operation.WAIT if memory.pending_writes else Operation.CLICK
            return Decision(choice=next(a.id for a in candidates if a.operation == operation),
                            outcome="pending")

    agent = DynamicController(task(), backend, Policy(), feedback=Brain(),
                              budget=Budget(no_progress_limit=1, readback_waits=2))
    result = await agent.run()
    assert result.status == "needs_attention" and "readback unresolved" in result.reason
    assert phases == ["initial", "action_readback"]
    assert agent.pending and sum(e["operation"] == Operation.CLICK for e in agent.memory.events) == 1
