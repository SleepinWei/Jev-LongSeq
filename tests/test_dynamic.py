import json

import httpx
import pytest
from pydantic import ValidationError

from jev_browser.browser import PlaywrightBackend
from jev_browser.controller import Controller
from jev_browser.dynamic import (
    WORKING_MEMORY_RECENT,
    WORKING_MEMORY_TARGET,
    WORKING_MEMORY_TRIGGER,
    DynamicController,
    EvidenceNote,
    Feedback,
    JsonFeedback,
    action_key,
    evidence_text,
    feedback_json,
    generate_dynamic,
)
from jev_browser.evaluation import efficiency_profile
from jev_browser.memory import Memory
from jev_browser.models import JevPolicy, ModelTransport
from jev_browser.protocol import (
    Budget,
    Decision,
    Element,
    Observation,
    Operation,
    Receipt,
    Task,
)


def task():
    return Task(
        id="arbitrary-form",
        control_mode="dynamic",
        sandbox=True,
        objective="Enter Ada, choose Gold, check consent and submit the form.",
    )


def observation(**kw):
    return Observation(
        observation_id="o1",
        document_version="v1",
        tab_id="tab-0",
        url="about:blank",
        title="Form",
        text=kw.pop("text", "A visible form"),
        **kw,
    )


async def test_unidentifiable_clicks_are_neither_offered_nor_dispatched():
    elements = [Element(id='blank', role='button', name=''),
                Element(id='form-blank', role='button', name='', context='Employee Save'),
                Element(id='navigation-blank', role='button', name='', context='Navigation: Search'),
                Element(id='row-editor', role='button', name='', context='Row 1 Employee',
                        grid_ref='g1', row_ref='1'),
                Element(id='link', role='link', name='', href='about:blank'),
                Element(id='refresh', role='button', name='refresh (icon control)')]
    obs = observation(elements=elements)
    clicks = [a for a in generate_dynamic(obs, task()) if a.operation == Operation.CLICK]
    assert {a.element_ref for a in clicks} == {'row-editor', 'link', 'refresh'}
    agent = DynamicController(task(), None, None, feedback=None)
    forged = clicks[-1].model_copy(update={'element_ref': 'blank'})
    assert await agent.perform(forged, obs) == 'click target has no observed name, destination or row context'
    assert agent.pending is None and not agent.memory.pending_writes
    assert not agent.consumed and agent.actions == 0


class FormPolicy:
    async def choose(self, task, obs, memory, contract, candidates):
        assert task.rules == task.success_predicates == task.extraction.fields == []
        assert contract is None
        assert memory.feedback["next_goal"]
        for element in obs.elements:
            operation = None
            if element.editable and element.value != "Ada":
                operation = Operation.FILL
            elif element.selectable and element.value != "gold":
                operation = Operation.SELECT
            elif element.role == "checkbox" and not element.checked:
                operation = Operation.CLICK
            elif element.role == "button":
                operation = Operation.CLICK
            if operation:
                action = next(
                    a
                    for a in candidates
                    if a.operation == operation and a.element_ref == element.id
                )
                return Decision(
                    choice=action.id, outcome="confirmed" if memory.pending_writes else "none"
                )
        return Decision(
            choice=next(a.id for a in candidates if a.operation == Operation.FINISH),
            outcome="confirmed" if memory.pending_writes else "none",
        )


class FormFeedback:
    async def review(self, task, obs, memory, *, phase, transition, diagnostic=None):
        complete = "Completed for Ada" in obs.text
        return Feedback(
            next_goal="Finish the user's form, checking the latest visible result.",
            notes=[EvidenceNote(quote=evidence_text(obs)[:1200])],
            last_outcome="confirmed" if transition and not transition.get("resolved") else "none",
            complete=complete,
            answer="Completed for Ada with Gold and consent." if complete else "",
        )

    async def value(self, task, obs, memory, action):
        return "gold" if action.operation == Operation.SELECT else "Ada"


FORM = """<h1>Registration</h1><form onsubmit="event.preventDefault();
document.body.textContent='Completed for '+this.elements.person.value+' with '+this.elements.tier.value+' and consent '+this.elements.consent.checked">
<label>Attendee<input name="person"></label>
<label>Membership<select name="tier"><option value="basic">Basic</option>
<option value="gold">Gold</option></select></label>
<label>Consent<input type="checkbox" name="consent"></label>
<button>Register</button></form>"""


async def test_rule_free_form_fills_selects_checks_and_freshly_verifies(tmp_path):
    definition = task()
    async with PlaywrightBackend(definition) as browser:
        await browser.load_html(FORM)
        initial = await browser.observe()
        checkbox = next(e for e in initial.elements if e.role == "checkbox")
        assert checkbox.checked is False and not checkbox.editable
        agent = DynamicController(
            definition,
            browser,
            FormPolicy(),
            feedback=FormFeedback(),
            output=tmp_path,
        )
        result = await agent.run()
    assert result.status == "success", result.reason
    assert result.strict_success is None  # semantic review is not a hidden grader
    assert result.actions == 4 and result.planner_calls == 0
    assert result.feedback_calls == 4  # initial guidance, fresh finish, two input helpers
    assert len(agent.memory.confirmed_writes) == 4
    assert not agent.memory.pending_writes
    assert agent.memory.facts == {} and agent.memory.observed_entities == set()
    assert len(agent.memory.evidence) >= 4
    assert "Completed for Ada" in agent.final_answer
    assert sum(e["kind"] == "finish_observation" for e in agent.events) == 1
    assert json.loads((tmp_path / "result.json").read_text())["feedback_calls"] == 4


def test_structured_empty_predicates_cannot_silently_accept_completion():
    with pytest.raises(ValidationError, match="success predicates"):
        Task(id="empty", objective="Do work")
    with pytest.raises(ValueError, match="DynamicController"):
        Controller(task(), None, None, mode="flat")
    with pytest.raises(ValidationError, match="predefined rules"):
        Task(
            id="bad",
            objective="Do work",
            control_mode="dynamic",
            success_predicates=[{"kind": "text", "value": "anything"}],
        )


def test_generic_candidates_cover_unknown_names_and_pages_and_filter_origins():
    obs = observation(
        elements=[Element(id=f"e{i}", role="button", name=f"Unseen control {i}") for i in range(40)]
        + [Element(id="external", role="link", name="Outside", href="https://outside.test/")]
    )
    seen = set()
    for offset in range(20):
        actions = generate_dynamic(obs, task(), limit=8, offset=offset)
        assert len(actions) <= 8
        seen.update(a.element_ref for a in actions if a.element_ref)
    assert seen == {f"e{i}" for i in range(40)}
    action = next(a for a in generate_dynamic(obs, task()) if a.element_ref == "e0")
    consumed = {action_key(action, obs)}
    assert not any(a.element_ref == "e0" for a in generate_dynamic(obs, task(), consumed=consumed))


class FirstClick:
    async def choose(self, task, obs, memory, contract, candidates):
        action = next(
            (a for a in candidates if a.operation == Operation.CLICK),
            next(a for a in candidates if a.operation == Operation.WAIT),
        )
        return Decision(choice=action.id, outcome="confirmed" if memory.pending_writes else "none")


async def test_dispatch_ok_without_visible_readback_never_repeats_submission():
    definition = task()
    async with PlaywrightBackend(definition) as browser:
        await browser.load_html(
            '<p>Pending form</p><button onclick="window.count=(window.count||0)+1">Send</button>'
        )
        agent = DynamicController(
            definition,
            browser,
            FirstClick(),
            feedback=FormFeedback(),
            budget=Budget(readback_waits=2),
        )
        result = await agent.run()
        assert await browser.page.evaluate("window.count") == 1
    assert result.status == "needs_attention"
    assert "no resubmission" in result.reason
    assert len(agent.memory.pending_writes) == 1
    assert len(agent.memory.confirmed_writes) == 0


class UnknownBackend:
    def __init__(self):
        self.dispatched = 0

    async def observe(self):
        return observation(elements=[Element(id="send", role="button", name="Send")])

    async def execute(self, action):
        self.dispatched += 1
        return Receipt(action_id=action.id, status="unknown")


async def test_unknown_receipt_stops_with_pending_record():
    backend = UnknownBackend()
    agent = DynamicController(task(), backend, FirstClick(), feedback=FormFeedback())
    result = await agent.run()
    assert result.status == "needs_attention" and backend.dispatched == 1
    assert len(agent.memory.pending_writes) == 1


async def test_ungrounded_feedback_repairs_once_and_never_executes():
    class Hallucinating(FormFeedback):
        async def review(self, *args, **kwargs):
            return Feedback(next_goal="Done", complete=True, answer="Done",
                            notes=[EvidenceNote(quote="Invented success")])

    backend = UnknownBackend()
    agent = DynamicController(task(), backend, FirstClick(), feedback=Hallucinating())
    result = await agent.run()
    assert result.status == "needs_attention" and result.feedback_calls == 2
    assert backend.dispatched == 0
    assert all(
        "Invented success" not in e["source"]["quote"] for e in agent.memory.evidence.values()
    )


@pytest.mark.parametrize("outcome", ["none", "pending", "unknown"])
async def test_nonconfirming_feedback_discards_bad_notes_without_repair(outcome):
    class Brain:
        calls = 0

        async def review(self, *args, **kwargs):
            self.calls += 1
            return Feedback(next_goal="Inspect remaining work", last_outcome=outcome,
                            notes=[EvidenceNote(quote="A visible form"),
                                   EvidenceNote(quote="Invented successful write")])

    brain = Brain()
    agent = DynamicController(task(), None, None, feedback=brain)
    feedback = await agent.review(observation(), phase="uncertain_outcome")
    assert brain.calls == agent.feedback_calls == 1
    assert feedback.last_outcome == outcome and not feedback.complete
    assert [note.quote for note in feedback.notes] == ["A visible form"]
    assert [e["source"]["quote"] for e in agent.memory.evidence.values()] == ["A visible form"]
    event = next(e for e in agent.events if e["kind"] == "feedback_notes_discarded")
    assert event["repair_call_skipped"]
    assert event["diagnostic"][0]["loc"] == ["notes", 1, "quote"]
    assert "Invented successful write" not in json.dumps(event)


async def test_unknown_outcome_with_bad_note_stops_without_retrying_or_replaying():
    class Backend(UnknownBackend):
        async def execute(self, action):
            self.dispatched += 1
            return Receipt(action_id=action.id, status="ok")

    class Policy(FirstClick):
        async def choose(self, task, obs, memory, contract, candidates):
            decision = await super().choose(task, obs, memory, contract, candidates)
            decision.outcome = "unknown" if memory.pending_writes else "none"
            return decision

    class Brain:
        async def review(self, *args, phase, **kwargs):
            return Feedback(next_goal="Inspect before acting",
                            last_outcome="unknown" if phase == "uncertain_outcome" else "none",
                            notes=[EvidenceNote(quote="Invented success")])

    backend = Backend()
    agent = DynamicController(task(), backend, Policy(), feedback=Brain())
    result = await agent.run()
    assert result.status == "needs_attention" and "uncertain mutation" in result.reason
    assert backend.dispatched == 1 and result.feedback_calls == 2  # Initial + outcome, no repair.
    assert agent.memory.pending_writes
    assert not agent.memory.confirmed_writes


async def test_whitespace_quote_repair_archives_exact_observed_text_without_http_retry():
    class Brain:
        async def review(self, *args, **kwargs):
            return Feedback(next_goal="Done", complete=True, answer="Observed form",
                            notes=[EvidenceNote(quote="A visible form")])

    obs = observation()
    obs.text = "A\n visible\tform"
    agent = DynamicController(task(), None, None, feedback=Brain())
    feedback = await agent.review(obs, phase="finish")
    assert agent.feedback_calls == 1 and feedback.complete
    assert feedback.notes[0].quote == obs.text
    assert next(iter(agent.memory.evidence.values()))["source"]["quote"] == obs.text
    assert any(e["kind"] == "feedback_quote_normalized" for e in agent.events)


async def test_confirmed_feedback_requires_grounding_and_gets_targeted_repair():
    class Brain:
        calls = 0

        async def review(self, *args, diagnostic=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                assert diagnostic is None
                return Feedback(next_goal="Continue", last_outcome="confirmed",
                                notes=[EvidenceNote(quote="Old page evidence")])
            assert diagnostic[0]["loc"] == ["notes", 0, "quote"]
            assert diagnostic[0]["type"] == "quote_not_in_current_observation"
            return Feedback(next_goal="Continue", last_outcome="confirmed",
                            notes=[EvidenceNote(quote="A visible form")])

    agent = DynamicController(task(), None, None, feedback=Brain())
    feedback = await agent.review(observation())
    assert agent.feedback_calls == 2
    assert feedback.last_outcome == "confirmed"
    assert feedback.notes[0].quote == "A visible form"


async def test_fresh_finish_review_can_reject_prior_complete_claim():
    class Premature(FormFeedback):
        async def review(self, task, obs, memory, *, phase, **kwargs):
            return Feedback(
                next_goal="More work remains",
                complete=phase != "finish",
                answer="Finished",
                notes=[EvidenceNote(quote="A visible form")],
            )

    backend = UnknownBackend()

    class FinishPolicy:
        async def choose(self, task, obs, memory, contract, candidates):
            return Decision(
                choice=next(a.id for a in candidates if a.operation == Operation.FINISH)
            )

    agent = DynamicController(
        task(),
        backend,
        FinishPolicy(),
        feedback=Premature(),
        budget=Budget(max_cycles=2),
    )
    result = await agent.run()
    assert result.status != "success" and result.false_completions == 2
    assert backend.dispatched == 0 and result.strict_success is None


async def test_input_helper_budget_is_enforced_before_browser_mutation():
    definition = task()
    async with PlaywrightBackend(definition) as browser:
        await browser.load_html(FORM)
        agent = DynamicController(
            definition,
            browser,
            FormPolicy(),
            feedback=FormFeedback(),
            budget=Budget(max_feedback_calls=1),
        )
        result = await agent.run()
        assert await browser.page.locator('[name="person"]').input_value() == ""
    assert result.status == "budget_exhausted" and result.actions == 0


async def test_generated_select_value_must_be_observed():
    class BadValue(FormFeedback):
        async def value(self, *args):
            return "invisible-option"

    definition = task()
    async with PlaywrightBackend(definition) as browser:
        await browser.load_html('<select><option value="basic">Basic</option></select>')
        agent = DynamicController(definition, browser, FormPolicy(), feedback=BadValue())
        result = await agent.run()
        assert await browser.page.locator("select").input_value() == "basic"
    assert result.status == "needs_attention" and result.actions == 0


async def test_feedback_wire_contract_and_call_ledger():
    def respond(request):
        payload = json.loads(request.content)
        # DeepSeek JSON mode requires an explicit JSON instruction in messages.
        assert "json" in " ".join(m["content"] for m in payload["messages"]).lower()
        content = json.loads(payload["messages"][1]["content"])
        assert content["trusted_goal"] == task().objective
        assert "verified_progress" not in content and "trusted_task" not in content
        if "selected_action" in content:
            response = {"value": "Ada"}
        elif content["phase"] == "step":
            assert "current_visible_evidence" not in content
            assert set(content["schema"]["properties"]) == {"next_goal", "working_memory", "notes", "evidence_requests"}
            response = {"next_goal": "Inspect", "working_memory": "Nothing completed yet"}
        else:
            assert "current_visible_evidence" in content
            response = Feedback(
                next_goal="Inspect", notes=[EvidenceNote(quote="A visible form")]
            ).model_dump()
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(response)}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = ModelTransport("https://model.test", "test", "test", client=client)
        feedback = JsonFeedback(transport)
        obs, memory = observation(), Memory()
        await feedback.review(task(), obs, memory, phase="step", transition=None)
        await feedback.review(task(), obs, memory, phase="finish", transition=None)
        await feedback.value(task(), obs, memory, generate_dynamic(obs, task())[0])
    assert [r["kind"] for r in transport.ledger] == [
        "dynamic_feedback",
        "dynamic_finish",
        "dynamic_input",
    ]


@pytest.mark.parametrize("phase", ["initial", "step", "finish"])
async def test_feedback_accepts_long_memory_and_harmless_json_metadata(phase):
    long_memory = "observed state\n" * 600
    response = {"next_goal": "Inspect", "working_memory": long_memory, "type": "object"}

    def respond(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "```json\n" + json.dumps(response) + "\n```"}}],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        brain = JsonFeedback(ModelTransport("https://model.test", "test", "test", client=client))
        feedback = await brain.review(task(), observation(), Memory(), phase=phase, transition=None)
    assert feedback.working_memory == long_memory
    with pytest.raises(ValidationError):
        Feedback.model_validate(feedback_json(json.dumps({**response, "action": "click"})))
    with pytest.raises(ValidationError):
        Feedback.model_validate(feedback_json(json.dumps({**response, "type": "execute"})))


async def test_memory_compression_preserves_recent_tail_pending_and_evidence():
    old = "old settled record\n" * 3000
    recent = "recent unresolved item\n" * 300
    text = old + recent

    def respond(request):
        payload = json.loads(request.content)
        content = json.loads(payload["messages"][1]["content"])
        assert content["trusted_goal"] == task().objective
        assert "working_memory" not in content["untrusted_memory"]
        assert content["recent_tail_retained"] == text[-WORKING_MEMORY_RECENT:]
        assert content["untrusted_memory"]["pending_writes"][0]["expected_goal"] == "readback required"
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({
                "working_memory": "Earlier entities processed; current form still requires readback."
            })}}], "usage": {"prompt_tokens": 20, "completion_tokens": 10},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = ModelTransport("https://model.test", "test", "test", client=client)
        agent = DynamicController(task(), None, None, feedback=JsonFeedback(transport))
        agent.memory.feedback["working_memory"] = text
        agent.memory.evidence["old"] = {"source": {"url": "about:blank", "quote": "old evidence"}}
        agent.memory.pending_writes["p"] = {
            "action": {"operation": "click"}, "expected_goal": "readback required", "waits": 1,
        }
        result = await agent.compact_memory(observation(), text)
        assert len(result) <= WORKING_MEMORY_TARGET
        assert result.endswith(text[-WORKING_MEMORY_RECENT:])
        assert "Earlier entities processed" in result
        assert agent.memory.pending_writes["p"]["waits"] == 1
        assert agent.memory.evidence["old"]["source"]["quote"] == "old evidence"
        assert agent.feedback_calls == 1
        assert text in agent.memory.working_memory_archive.values()
        assert [r["kind"] for r in transport.ledger] == ["dynamic_memory_compression"]
        profile = efficiency_profile(transport.ledger, actions=0, elapsed_s=1)
        assert profile["by_component"]["brain"]["known_input_tokens"] == 20
        assert profile["total"]["attempts"] == 1
        assert await agent.compact_memory(observation(), result) == result
        assert agent.feedback_calls == 1


@pytest.mark.parametrize("failure", ["provider", "empty", "budget"])
async def test_memory_compression_failure_keeps_newest_data_without_stopping(failure):
    class Brain:
        async def compress(self, *args):
            if failure == "provider":
                raise RuntimeError("provider unavailable")
            return ""

    agent = DynamicController(task(), None, None, feedback=Brain(), budget=Budget(max_feedback_calls=1))
    if failure == "budget":
        agent.feedback_calls = 1
    text = "old\n" * WORKING_MEMORY_TRIGGER + "newest pending readback"
    compacted = await agent.compact_memory(observation(), text)
    assert compacted == text[-WORKING_MEMORY_TARGET:]
    assert compacted.endswith("newest pending readback")
    assert agent.feedback_calls == 1
    assert agent.events[-1]["method"] == "recent_tail"


async def test_oversized_feedback_is_compacted_before_policy_context():
    text = "settled\n" * WORKING_MEMORY_TRIGGER + "current pending work"

    class Brain:
        async def review(self, *args, **kwargs):
            return Feedback(next_goal="Continue", working_memory=text)

    agent = DynamicController(task(), None, None, feedback=Brain())
    feedback = await agent.review(observation())
    assert feedback.working_memory == text[-WORKING_MEMORY_TARGET:]
    assert agent.memory.context()["working_memory"] == feedback.working_memory
    assert agent.events[-1]["feedback"]["working_memory"] == feedback.working_memory


async def test_sparse_brain_matches_per_step_baseline_with_fewer_llm_calls():
    results = []
    for interval in (1, 12):
        definition = task()
        async with PlaywrightBackend(definition) as browser:
            await browser.load_html(FORM)
            agent = DynamicController(
                definition,
                browser,
                FormPolicy(),
                feedback=FormFeedback(),
                budget=Budget(brain_interval=interval),
            )
            result = await agent.run()
            results.append(result)
    baseline, sparse = results
    assert baseline.status == sparse.status == "success"
    assert baseline.actions == sparse.actions == 4
    assert baseline.feedback_calls == 8 and sparse.feedback_calls == 4
    # Test doubles establish scheduler behavior only, not real model speed or token savings.


async def test_uncertain_outcome_calls_brain_and_discards_old_next_action():
    class UncertainOnce(FormPolicy):
        escalated = False

        async def choose(self, task, obs, memory, contract, candidates):
            if memory.pending_writes and not self.escalated:
                self.escalated = True
                submit = next(
                    a
                    for a in candidates
                    if a.operation == Operation.CLICK and "Register" in a.description
                )
                # This proposal was made before updated guidance and must not be consumed.
                return Decision(choice=submit.id, outcome="unknown")
            return await super().choose(task, obs, memory, contract, candidates)

    definition = task()
    async with PlaywrightBackend(definition) as browser:
        await browser.load_html(FORM)
        agent = DynamicController(
            definition,
            browser,
            UncertainOnce(),
            feedback=FormFeedback(),
        )
        result = await agent.run()
        assert "with gold and consent true" in await browser.page.locator("body").inner_text()
    assert result.status == "success" and result.actions == 4
    assert result.feedback_calls == 5
    assert (
        sum(
            e["kind"] == "brain_requested" and e["reason"] == "uncertain_outcome"
            for e in agent.events
        )
        == 1
    )


async def test_stage_brain_confirmation_resolves_pending_before_next_policy_choice():
    class PendingPolicy(FormPolicy):
        async def choose(self, task, obs, memory, contract, candidates):
            decision = await super().choose(task, obs, memory, contract, candidates)
            if memory.pending_writes:
                decision.outcome = "pending"
            return decision

    definition = task()
    async with PlaywrightBackend(definition) as browser:
        await browser.load_html(FORM)
        agent = DynamicController(definition, browser, PendingPolicy(), feedback=FormFeedback(),
                                  budget=Budget(brain_interval=1))
        result = await agent.run()
        assert "with gold and consent true" in await browser.page.locator("body").inner_text()
    assert result.status == "success"
    assert result.actions == 4
    assert any(e["kind"] == "transition_confirmed" and e["basis"] == "stage_brain_review"
               for e in agent.events)


@pytest.mark.parametrize("changed", [True, False])
async def test_readback_deadline_reviews_local_click_without_replaying(changed):
    class Backend:
        clicks = 0

        async def observe(self):
            closed = self.clicks and changed
            return Observation(
                observation_id="closed" if closed else "open", document_version="v1",
                tab_id="tab-0", url="about:blank", title="Dialog",
                text="Dialog closed" if closed else "Dialog open",
                elements=[] if closed else [Element(id="close", role="button", name="Close")],
            )

        async def execute(self, action):
            if action.operation == Operation.CLICK:
                self.clicks += 1
            return Receipt(action_id=action.id, status="ok")

    class Policy:
        async def choose(self, task, obs, memory, contract, candidates):
            operation = Operation.FINISH if backend.clicks else Operation.CLICK
            return Decision(choice=next(a.id for a in candidates if a.operation == operation),
                            outcome="pending" if memory.pending_writes else "none")

    class Brain:
        async def review(self, task, obs, memory, *, phase, transition, diagnostic=None):
            if transition and not transition.get("resolved"):
                assert transition["stage_goal"] == "Read all remaining records after closing dialog"
                assert transition["expected_goal"] != transition["stage_goal"]
                assert "Close" in transition["expected_goal"]
            return Feedback(
                next_goal="Read all remaining records after closing dialog",
                notes=[EvidenceNote(quote=obs.text)],
                last_outcome="confirmed" if transition else "none",
                complete=phase == "finish" and changed,
                answer="Dialog closed" if phase == "finish" and changed else "",
            )

    backend = Backend()
    agent = DynamicController(task(), backend, Policy(), feedback=Brain(),
                              budget=Budget(readback_waits=2, no_progress_limit=100))
    result = await agent.run()
    assert backend.clicks == 1, result.reason
    assert result.status == ("success" if changed else "needs_attention")
    assert any(e["kind"] == "brain_requested" and e["reason"] == "action_readback"
               for e in agent.events)
    if not changed:
        assert not any(e["kind"] == "transition_confirmed" for e in agent.events)
    else:
        assert any(e["kind"] == "transition_confirmed" and e["basis"] == "action_readback_review"
                   for e in agent.events)


@pytest.mark.parametrize("operation", [Operation.FILL, Operation.SELECT])
async def test_same_value_input_can_be_confirmed_without_page_change(operation):
    class Backend:
        async def execute(self, action):
            return Receipt(action_id=action.id, status="ok")

    el = Element(id="user", role="combobox", name="User", value="Rajesh Kumar",
                 editable=operation == Operation.FILL, selectable=operation == Operation.SELECT,
                 options=["Rajesh Kumar"])
    obs = observation(elements=[el])
    agent = DynamicController(task(), Backend(), None, feedback=None)
    action = next(a for a in generate_dynamic(obs, task()) if a.operation == operation)
    action.bound_value = "Rajesh Kumar"
    await agent.perform(action, obs)
    assert agent.pending is None  # Same visible value is satisfied without another dispatch.
    assert any(e["kind"] == "input_already_satisfied" for e in agent.events)
    assert not agent.memory.pending_writes
    assert not agent.memory.feedback.get("complete")  # Input readback is not task completion.


async def test_same_state_confirmation_rejects_hidden_or_wrong_input_target():
    class Backend:
        async def execute(self, action):
            return Receipt(action_id=action.id, status="ok")

    obs = observation(elements=[Element(id="field", role="textbox", name="User",
                                        value="[redacted]", editable=True)])
    agent = DynamicController(task(), Backend(), None, feedback=None)
    action = next(a for a in generate_dynamic(obs, task()) if a.operation == Operation.FILL)
    action.bound_value = "[redacted]"
    await agent.perform(action, obs)
    assert not agent.confirm_transition("confirmed", obs, "test")
    agent.pending["action"]["bound_value"] = "Rajesh Kumar"
    assert not agent.confirm_transition("confirmed", obs, "test")


@pytest.mark.parametrize("receipt_status", ["stale", "unknown"])
async def test_stale_option_is_reselected_from_fresh_page_but_unknown_is_not_replayed(receipt_status):
    class Backend:
        attempts = 0
        selected = False

        async def observe(self):
            return Observation(observation_id=f"o{self.attempts}", document_version="v1",
                               tab_id="tab-0", url="about:blank", title="Users",
                               text="User selected" if self.selected else "Pick Rajesh",
                               elements=[] if self.selected else [Element(
                                   id=f"user{self.attempts}", role="option", name="Rajesh Kumar")])

        async def execute(self, action):
            self.attempts += 1
            if self.attempts == 1:
                return Receipt(action_id=action.id, status=receipt_status)
            assert action.element_ref == "user1"
            self.selected = True
            return Receipt(action_id=action.id, status="ok")

    class Policy:
        calls = 0

        async def choose(self, task, obs, memory, contract, candidates):
            self.calls += 1
            if not backend.selected:
                assert self.calls == 1  # Fresh retry preserves the undispatched choice.
            op = Operation.FINISH if backend.selected else Operation.CLICK
            return Decision(choice=next(a.id for a in candidates if a.operation == op),
                            outcome="confirmed" if memory.pending_writes else "none")

    class Brain:
        async def review(self, task, obs, memory, **kw):
            return Feedback(next_goal="Select Rajesh", complete=backend.selected,
                            answer="User selected" if backend.selected else "",
                            notes=[EvidenceNote(quote=obs.text)])

    backend, policy = Backend(), Policy()
    agent = DynamicController(task(), backend, policy, feedback=Brain())
    result = await agent.run()
    assert backend.attempts == (2 if receipt_status == "stale" else 1)
    assert result.status == ("success" if receipt_status == "stale" else "needs_attention")
    assert any(e["kind"] == "stale_click_reselected" for e in agent.events) == (receipt_status == "stale")


@pytest.mark.parametrize("change", ["ambiguous", "tab", "url", "context", "dialog", "exhausted"])
def test_stale_reselection_refuses_ambiguous_or_changed_page(change):
    obs = observation(elements=[Element(id="new", role="option", name="Rajesh Kumar")])
    agent = DynamicController(task(), None, None, feedback=None)
    agent.stale_click = {"element": obs.elements[0].model_dump(), "attempts": 1,
                         "url": obs.url, "tab_id": obs.tab_id,
                         "title": obs.title, "dialogs": list(obs.dialogs)}
    if change == "ambiguous":
        obs.elements.append(obs.elements[0].model_copy(update={"id": "other"}))
    elif change == "tab":
        obs.tab_id = "tab-1"
    elif change == "url":
        obs.url = "https://another.example"
    elif change == "context":
        obs.elements[0].context = "Different record"
    elif change == "dialog":
        obs.dialogs = ["New editor"]
    else:
        agent.stale_click["attempts"] = 3
    assert agent.refreshed_stale_click(obs, generate_dynamic(obs, task())) is None


async def test_jev_outcome_and_next_action_share_one_http_request():
    def respond(request):
        payload = json.loads(request.content)
        assert set(payload["questions"]) == {"action", "outcome"}
        options = payload["questions"]["action"]["criteria"]
        assert "document_version" not in next(iter(options.values()))
        assert "document_version" not in payload["state"]["untrusted_observation"]
        return httpx.Response(
            200,
            json={
                "answers": {
                    "action": {"type": "choice", "choice": next(iter(options)), "confidence": 0.9},
                    "outcome": {"type": "choice", "choice": "confirmed"},
                },
                "usage": {"input_tokens": 80, "output_tokens": 6},
            },
        )

    memory = Memory()
    memory.pending_writes["last"] = {"action": "prior dispatched click"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = ModelTransport("https://model.test", "test", "test", client=client)
        decision = await JevPolicy(transport).choose(
            task(), observation(), memory, None, generate_dynamic(observation(), task())
        )
    assert decision.outcome == "confirmed" and len(transport.ledger) == 1


def test_efficiency_counts_failures_and_separates_brain_from_jev():
    ledger = [
        {
            "kind": "dynamic_feedback",
            "status": 200,
            "input_tokens": 90,
            "output_tokens": 10,
            "latency_s": 1,
            "cost_usd": 0.01,
        },
        {"kind": "jev", "error": "ConnectError", "latency_s": 0.5},
        {
            "kind": "jev",
            "status": 200,
            "input_tokens": 20,
            "output_tokens": 2,
            "latency_s": 0.2,
            "cost_usd": 0.001,
        },
    ]
    profile = efficiency_profile(ledger, actions=1, elapsed_s=2)
    assert profile["by_component"]["brain"]["http_attempts"] == 1
    assert profile["by_component"]["jev"]["http_attempts"] == 2
    assert profile["total"]["unknown_usage_attempts"] == 1
    assert profile["total"]["known_input_tokens"] == 110
    assert profile["total"]["cost_usd"] is None


@pytest.mark.parametrize('receipt_status', ['ok', 'unknown'])
async def test_link_destination_readback_does_not_wait_for_entire_reading_goal(receipt_status):
    class Navigation:
        def __init__(self):
            self.clicks = 0
        async def observe(self):
            return Observation(
                observation_id=f'o{self.clicks}', document_version=f'v{self.clicks}', tab_id='tab-0',
                url='https://example.test/next' if self.clicks else 'https://example.test/',
                http_status=200, title='Catalogue', text='More entries still need reading',
                elements=[] if self.clicks else [Element(id='link', role='link', name='Continue',
                                                        href='https://example.test/next')])
        async def execute(self, action):
            self.clicks += 1
            return Receipt(action_id=action.id, status=receipt_status)
    class ReadingPolicy:
        async def choose(self, task, obs, memory, contract, candidates):
            if backend.clicks:
                assert not memory.pending_writes
                # Still not complete: the normal fresh finish review must reject this.
                return Decision(choice=next(a.id for a in candidates if a.operation == Operation.FINISH))
            return Decision(choice=next(a.id for a in candidates if a.operation == Operation.CLICK))
    class ReadingFeedback:
        async def review(self, *args, **kwargs):
            return Feedback(next_goal='Read all entries, then aggregate', complete=False)
    backend = Navigation()
    definition = Task(id='navigation', control_mode='dynamic', objective='Read every entry',
                      start_url='https://example.test/', allowed_origins=['https://example.test'])
    agent = DynamicController(definition, backend, ReadingPolicy(), feedback=ReadingFeedback(),
                              budget=Budget(max_cycles=3))
    result = await agent.run()
    assert backend.clicks == 1
    assert result.status != 'success'
    assert not agent.pending
    assert any(e['kind']=='transition_confirmed' and e['basis']=='observed_navigation_destination'
               for e in agent.events)


async def test_compact_stage_keeps_unsummarized_cross_page_evidence():
    class Transport:
        model = 'test'
        async def post(self, payload, kind):
            content = json.loads(payload['messages'][-1]['content'])
            assert content['new_evidence_since_last_brain_call'] == [
                {'url': 'https://example.test/earlier', 'quote': 'Earlier visible record: 12.34'}]
            return {'choices': [{'message': {'content': json.dumps({
                'next_goal': 'Continue collecting records', 'working_memory': 'Earlier total: 12.34'})}}]}
    memory = Memory()
    memory.dynamic_mode = True
    memory.evidence['earlier'] = {'source': {
        'url':'https://example.test/earlier', 'quote':'Earlier visible record: 12.34'}}
    await JsonFeedback(Transport()).review(task(), observation(), memory,
                                           phase='stage_budget', transition=None)


async def test_finish_review_receives_evidence_before_last_summary_cursor():
    class Transport:
        model = 'test'
        async def post(self, payload, kind):
            content = json.loads(payload['messages'][-1]['content'])
            assert content['new_evidence_since_last_brain_call'] == []
            assert content['sourced_evidence_archive'] == [
                {'url': 'https://example.test/first', 'quote': 'First-page record: 12.34'}]
            return {'choices': [{'message': {'content': json.dumps({
                'next_goal':'Check remaining coverage', 'notes':[], 'complete':False})}}]}
    memory = Memory()
    memory.dynamic_mode = True
    memory.evidence['old'] = {'source': {'url':'https://example.test/first',
                                       'quote':'First-page record: 12.34'}}
    memory.feedback['evidence_cursor'] = 1
    await JsonFeedback(Transport()).review(task(), observation(), memory,
                                           phase='finish', transition=None)


@pytest.mark.parametrize('link', [True, False])
async def test_pending_popup_can_be_inspected_before_click_confirmation(link):
    source, destination = 'https://example.test/', 'https://example.test/next'

    class Popup:
        def __init__(self):
            self.clicked = False
            self.active = 'tab-0'
            self.operations = []

        async def observe(self):
            tabs = {'tab-0': source, **({'tab-1': destination} if self.clicked else {})}
            return Observation(observation_id=f'o{len(self.operations)}', document_version='v',
                tab_id=self.active, url=tabs[self.active], tabs=tabs, http_status=200,
                title='Source' if self.active == 'tab-0' else 'Article',
                text='Unchanged source' if self.active == 'tab-0' else 'Article content',
                elements=[Element(id='link', role='link' if link else 'button', name='Read article',
                                  href=destination if link else None)])

        async def execute(self, action):
            self.operations.append(action.operation)
            if action.operation == Operation.CLICK:
                assert not self.clicked
                self.clicked = True
            elif action.operation == Operation.SWITCH_TAB:
                assert agent.pending  # Switching observes; it does not confirm the click.
                self.active = action.bound_value
            return Receipt(action_id=action.id, status='ok')

    class Policy:
        async def choose(self, task, obs, memory, contract, candidates):
            if backend.active == 'tab-1':
                return Decision(choice=next(a.id for a in candidates if a.operation == Operation.FINISH),
                                outcome='confirmed')
            op = Operation.SWITCH_TAB if backend.clicked else Operation.CLICK
            return Decision(choice=next(a.id for a in candidates if a.operation == op),
                            outcome='pending' if backend.clicked else 'none')

    class Brain:
        async def review(self, *args, **kwargs):
            return Feedback(next_goal='Read the opened article', complete=False)

    backend = Popup()
    definition = Task(id='popup', control_mode='dynamic', objective='Read the article',
                      start_url=source, allowed_origins=['https://example.test'])
    agent = DynamicController(definition, backend, Policy(), feedback=Brain(),
                              budget=Budget(max_cycles=3))
    result = await agent.run()
    assert backend.operations == [Operation.CLICK, Operation.SWITCH_TAB]
    assert backend.active == 'tab-1' and agent.pending is None
    assert result.status != 'success'  # Navigating is not task completion.
    registry = agent.memory.context()['opened_pages']
    assert any(p['url'] == destination and p['observed'] for p in registry)


def test_readback_switch_does_not_include_old_or_unauthorized_tabs():
    definition = Task(id='popup', control_mode='dynamic', objective='Read',
                      start_url='https://example.test/', allowed_origins=['https://example.test'])
    agent = DynamicController(definition, None, None, feedback=None)
    agent.pending = {'action': {'operation': Operation.CLICK},
                     'before_tabs': {'tab-old': 'https://example.test/old'}}
    obs = observation(tabs={'tab-old': 'https://example.test/old',
                            'tab-other': 'https://outside.test/',
                            'tab-new': 'https://example.test/new'})
    assert agent.readback_tabs(obs) == {'tab-new': 'https://example.test/new'}
    agent.pending['action']['operation'] = Operation.FILL
    assert agent.readback_tabs(obs) == {}


def test_page_registry_retains_sources_after_rolling_notes_expire():
    memory = Memory()
    memory.dynamic_mode = True
    obs = observation(tabs={'tab-0': 'about:blank', 'tab-1': 'https://example.test/article'})
    memory.observe(obs)
    assert memory.page_registry['https://example.test/article']['observed'] is False
    for i in range(30):
        page = obs.model_copy(update={'url': f'https://example.test/{i}', 'title': f'Page {i}',
                                      'text': f'Source {i}', 'tabs': {'tab-0': f'https://example.test/{i}'}})
        memory.observe(page)
    assert len(memory.page_notes) == 8
    assert memory.page_registry['https://example.test/0']['excerpt'] == 'Source 0'
    assert memory.page_registry['https://example.test/article']['open_tab_ids'] == []
    context = memory.context()
    assert context['opened_pages_truncated'] and len(context['opened_pages']) == 24
    assert memory.export()['page_registry']['https://example.test/0']['observed']


@pytest.mark.parametrize('operation', [Operation.FILL, Operation.SELECT])
async def test_fresh_exact_input_readback_resolves_without_model_pending_waits(operation):
    class Backend:
        calls = 0

        async def execute(self, action):
            self.calls += 1
            return Receipt(action_id=action.id, status='ok')

    field = Element(id='field', role='combobox', name='Employee', value='',
                    editable=operation == Operation.FILL, selectable=operation == Operation.SELECT,
                    options=['Ada'], context='Row 1')
    obs = observation(elements=[field])
    backend = Backend()
    agent = DynamicController(task(), backend, None, feedback=None)
    action = next(a for a in generate_dynamic(obs, task()) if a.operation == operation)
    action.bound_value = 'Ada'
    await agent.perform(action, obs)
    fresh = obs.model_copy(deep=True)
    fresh.observation_id = 'o2'
    fresh.elements[0].value = 'Ada'
    assert agent.confirm_visible_input(fresh)
    assert not agent.pending and not agent.memory.pending_writes
    assert backend.calls == 1
    assert not agent.memory.feedback.get('complete')
    # A same-value proposal is satisfied; it does not dispatch a second mutation.
    action.observation_id = fresh.observation_id
    await agent.perform(action, fresh)
    assert backend.calls == 1


@pytest.mark.parametrize('case', ['unknown', 'same_observation', 'wrong_id', 'wrong_row', 'password'])
async def test_local_readback_does_not_confirm_ambiguous_hidden_or_unknown_input(case):
    class Backend:
        async def execute(self, action):
            return Receipt(action_id=action.id, status='unknown' if case == 'unknown' else 'ok')

    obs = observation(elements=[Element(id='field', role='textbox', name='Employee', editable=True,
                                        context='Row 1')])
    agent = DynamicController(task(), Backend(), None, feedback=None)
    action = next(a for a in generate_dynamic(obs, task()) if a.operation == Operation.FILL)
    action.bound_value = '[redacted]' if case == 'password' else 'Ada'
    await agent.perform(action, obs)
    fresh = obs.model_copy(deep=True)
    fresh.observation_id = 'o2' if case != 'same_observation' else 'o1'
    fresh.elements[0].value = action.bound_value
    if case == 'wrong_id':
        fresh.elements[0].id = 'replacement'
    if case == 'wrong_row':
        fresh.elements[0].context = 'Row 2'
    assert not agent.confirm_visible_input(fresh)
    assert agent.pending


async def test_repeated_save_remains_blocked_and_runtime_failure_stops_before_models():
    class Backend:
        calls = 0

        async def execute(self, action):
            self.calls += 1
            return Receipt(action_id=action.id, status='ok')

    obs = observation(elements=[Element(id='save', role='button', name='Save')])
    backend = Backend()
    agent = DynamicController(task(), backend, None, feedback=None)
    action = next(a for a in generate_dynamic(obs, task()) if a.operation == Operation.CLICK)
    await agent.perform(action, obs)
    assert 'no resubmission' in await agent.perform(action, obs)
    assert backend.calls == 1
    obs.elements.append(Element(id='company', role='status', name='Company', required=True,
                               read_only=True, enabled=False))
    obs.errors = ["page_error:Cannot read properties of undefined (reading 'fields_dict')"]
    assert 'Company' in agent.blocked(obs)


def archived_quote(agent, quote, *, key='old'):
    agent.memory.evidence[key] = {
        'verification': 'quote_grounded_only',
        'source': {'url': 'about:blank', 'tab_id': 'tab-0', 'observation_id': 'old-page',
                   'document_version': 'old-version', 'captured_at': 'old-time',
                   'pointer': 'visible_observation', 'quote': quote}}


async def test_confirmed_local_readback_keeps_current_proof_and_references_history_without_retry():
    old = 'textbox Separation Begins On = 2026-06-30'
    unknown = 'Invented settlement payment success'
    question = 'Permanently Submit HR-EMP-SEP-2026-00001?'

    class Brain:
        async def review(self, *args, **kwargs):
            return Feedback(next_goal='Click the affirmative button once, then inspect submission',
                            last_outcome='confirmed', readback_quote=question,
                            notes=[EvidenceNote(quote=old, critical=True),
                                   EvidenceNote(quote=unknown, critical=True)])

    agent = DynamicController(task(), None, None, feedback=Brain())
    archived_quote(agent, old)
    before = agent.memory.evidence['old'].copy()
    obs = observation(dialogs=[question], elements=[Element(id='yes', role='button', name='Yes')])
    feedback = await agent.review(obs, phase='action_readback')
    assert agent.feedback_calls == 1 and feedback.last_outcome == 'confirmed'
    assert [n.quote for n in feedback.notes] == [question]
    assert agent.memory.evidence['old'] == before
    assert not agent.memory.key_nodes  # A stale critical note is not pinned as current.
    assert not any(e['source']['quote'] == unknown for e in agent.memory.evidence.values())
    historical = next(e for e in agent.events if e['kind'] == 'feedback_historical_references')
    assert not historical['promoted_to_current_evidence']
    assert historical['references'][0]['historical_source']['observation_id'] == 'old-page'
    assert not any(e['kind'] == 'invalid_feedback' for e in agent.events)


async def test_historical_quote_alone_cannot_confirm_and_exhaustion_preserves_pending():
    from jev_browser.dynamic import ReadbackUnresolved, semantic_key

    old = 'textbox Separation Begins On = 2026-06-30'

    class Brain:
        async def review(self, *args, **kwargs):
            return Feedback(next_goal='Need actual readback', last_outcome='confirmed',
                            notes=[EvidenceNote(quote=old)])

    obs = observation()
    agent = DynamicController(task(), None, None, feedback=Brain())
    archived_quote(agent, old)
    agent.pending = {'key': 'pending', 'before_semantics': semantic_key(obs),
                     'action': {'operation': Operation.CLICK}, 'waits': 0}
    agent.memory.pending_writes['pending'] = agent.pending
    with pytest.raises(ReadbackUnresolved):
        await agent.review(obs, phase='action_readback')
    assert agent.feedback_calls == 2
    assert agent.pending and agent.memory.pending_writes
    assert not agent.memory.confirmed_writes
    assert agent.memory.feedback['last_outcome'] == 'unknown'
    assert list(agent.memory.evidence) == ['old']


async def test_completion_still_rejects_unknown_notes_even_with_current_readback_quote():
    class Brain:
        async def review(self, *args, **kwargs):
            return Feedback(next_goal='Done', complete=True, answer='All done',
                            readback_quote='A visible form',
                            notes=[EvidenceNote(quote='Invented successful submission')])

    agent = DynamicController(task(), None, None, feedback=Brain())
    with pytest.raises(ValueError, match='schema/evidence'):
        await agent.review(observation(), phase='finish')
    assert agent.feedback_calls == 2 and not agent.memory.evidence


@pytest.mark.parametrize('case', ['unknown', 'same_frame', 'existing_dialog', 'different_url',
                                 'different_tab', 'wrong_question', 'error_popup', 'duplicate_yes'])
async def test_confirmation_dialog_guard_rejects_ambiguous_or_unknown_effects(case):
    class Backend:
        async def execute(self, action):
            return Receipt(action_id=action.id, status='unknown' if case == 'unknown' else 'ok')

    before = observation(elements=[Element(id='submit', role='button', name='S u bmit')])
    if case == 'existing_dialog':
        before.dialogs = ['Already open']
    agent = DynamicController(task(), Backend(), None, feedback=None)
    action = next(a for a in generate_dynamic(before, task()) if a.operation == Operation.CLICK)
    await agent.perform(action, before)
    after = observation(dialogs=['Confirm\nPermanently Submit RECORD-1?\nNo Yes'],
                        elements=[Element(id='yes', role='button', name='Yes'),
                                  Element(id='no', role='button', name='No')])
    after.observation_id = 'fresh'
    if case == 'same_frame':
        after.observation_id = before.observation_id
    if case == 'different_url':
        after.url = 'https://example.test/other'
    if case == 'different_tab':
        after.tab_id = 'other'
    if case == 'wrong_question':
        after.dialogs = ['Confirm\nPermanently Delete OTHER-1?\nNo Yes']
    if case == 'error_popup':
        after.errors = ['page_error:submit callback failed']
    if case == 'duplicate_yes':
        after.elements.append(Element(id='yes-2', role='button', name='Yes'))
    assert not agent.confirm_visible_dialog(after)
    assert agent.pending and not agent.memory.confirmed_writes


async def test_native_dialog_first_frame_readback_never_confirms_the_affirmative_commit():
    definition = task()
    async with PlaywrightBackend(definition) as browser:
        await browser.load_html('''<p>Draft</p><button onclick="document.querySelector('dialog').showModal()">S u bmit</button>
            <dialog><p>Permanently Submit RECORD-1?</p><button>No</button>
            <button onclick="window.commits=(window.commits||0)+1">Yes</button></dialog>''')
        before = await browser.observe()
        agent = DynamicController(definition, browser, None, feedback=None)
        action = next(a for a in generate_dynamic(before, definition) if a.operation == Operation.CLICK)
        await agent.perform(action, before)
        fresh = await browser.observe()
        assert [e.name for e in fresh.elements] == ['No', 'Yes']
        assert agent.confirm_visible_dialog(fresh)
        assert not agent.pending and agent.last_transition['confirmation_scope'] == 'dialog_opened'
        assert not agent.last_transition['business_commit_confirmed']
        assert not agent.memory.feedback.get('complete')
        assert await browser.page.evaluate('window.commits || 0') == 0
        assert agent.actions == 1 and not any(e['operation'] == Operation.WAIT for e in agent.memory.events)
        yes = next(a for a in generate_dynamic(fresh, definition) if a.element_ref == fresh.elements[1].id)
        await agent.perform(yes, fresh)
        current = await browser.observe()
        assert not agent.confirm_visible_dialog(current)  # Still-open dialog proves no commit.
        assert agent.pending and await browser.page.evaluate('window.commits') == 1
        assert 'observed_dialog_open_only' in [e['verification'] for e in agent.memory.evidence.values()]


async def test_feedback_wire_separates_modal_scope_and_direct_readback_from_context_notes():
    question = 'Permanently Submit RECORD-1?'

    def respond(request):
        payload = json.loads(request.content)
        content = json.loads(payload['messages'][1]['content'])
        assert content['current_page']['dialogs'] == [question]
        assert 'untrusted_memory' not in content
        assert set(content['schema']['properties']) == {'last_outcome', 'evidence_ids'}
        assert payload['max_tokens'] == 4096
        ref = next(key for key, quote in content['readback_evidence'].items() if quote == question)
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(
            {'last_outcome': 'confirmed', 'evidence_ids': [ref]})}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        brain = JsonFeedback(ModelTransport('https://test.example', 'test', 'test', client=client))
        result = await brain.review(task(), observation(dialogs=[question]), Memory(),
                                    phase='action_readback', transition={'resolved': False})
    assert result.readback_quote == question


@pytest.mark.parametrize('extra_control', ['yes', 'no', 'cancel', 'input'])
def test_readback_inspection_cannot_dismiss_confirmation_or_editable_dialog(extra_control):
    agent = DynamicController(task(), None, None, feedback=None)
    obs = observation(dialogs=['Message'], elements=[Element(id='close', role='button', name='Close')])
    agent.pending = {'key': 'original', 'dispatch_status': 'ok',
                     'before': {'url': obs.url, 'tab_id': obs.tab_id}}
    obs.elements.append(Element(id='other', role='textbox' if extra_control == 'input' else 'button',
                                name=extra_control.title(), editable=extra_control == 'input'))
    close = next(a for a in generate_dynamic(obs, task()) if a.element_ref == 'close')
    assert not agent.readback_dialog_close(obs, close)


@pytest.mark.parametrize('status', ['ok', 'stale', 'unknown'])
async def test_readback_close_preserves_original_pending_and_never_replays_unknown(status):
    class Backend:
        calls = 0

        async def execute(self, action):
            self.calls += 1
            return Receipt(action_id=action.id, status=status)

    obs = observation(dialogs=['Message\nShared with users'],
                      elements=[Element(id='close', role='button', name='close (icon control)')])
    backend = Backend()
    agent = DynamicController(task(), backend, None, feedback=None)
    original = {'key': 'original', 'dispatch_status': 'ok',
                'before': {'url': obs.url, 'tab_id': obs.tab_id},
                'action': {'operation': Operation.CLICK, 'description': 'Click Yes'}}
    agent.pending = original
    agent.memory.pending_writes['original'] = original
    close = next(a for a in generate_dynamic(obs, task()) if a.operation == Operation.CLICK)
    reason = await agent.close_for_readback(obs, close)
    assert agent.pending is original and agent.memory.pending_writes['original'] is original
    assert not agent.memory.confirmed_writes
    assert agent.memory.events[-1]['readback_for'] == 'original'
    if status == 'unknown':
        assert 'no resubmission' in reason
        assert 'already dispatched' in await agent.close_for_readback(obs, close)
        assert backend.calls == 1
    else:
        assert reason is None


async def test_native_confirmation_message_inspection_and_business_commit_have_separate_outcomes():
    definition = Task(id='dialog-chain', objective='Submit this document and read back its submitted state',
                      control_mode='dynamic', sandbox=True)
    html = '''<p id="state">Draft</p><button onclick="document.querySelector('#confirm').showModal()">Submit</button>
        <dialog id="confirm"><p>Permanently Submit RECORD-1?</p><button>No</button>
        <button onclick="document.querySelector('#state').textContent='Submitted';this.closest('dialog').close();
                         document.querySelector('#message').showModal()">Yes</button></dialog>
        <dialog id="message"><p>Shared with users</p><button onclick="this.closest('dialog').close()">Close</button></dialog>'''

    class Policy:
        async def choose(self, task, obs, memory, contract, candidates):
            if obs.dialogs:
                name = 'Close' if 'Shared with users' in obs.dialogs[0] else 'Yes'
                ref = next(e.id for e in obs.elements if e.name == name)
                chosen = next(a for a in candidates if a.element_ref == ref)
                return Decision(choice=chosen.id, outcome='pending')
            operation = Operation.FINISH if 'Submitted' in obs.text else Operation.CLICK
            return Decision(choice=next(a.id for a in candidates if a.operation == operation),
                            outcome='confirmed' if memory.pending_writes else 'none')

    class Brain:
        async def review(self, task, obs, memory, *, phase, transition, **kwargs):
            complete = not obs.dialogs and 'Submitted' in obs.text
            return Feedback(next_goal='Read the submitted state', complete=complete,
                            answer='Submitted' if complete else '',
                            notes=[EvidenceNote(quote='Submitted' if complete else 'Draft')])

    async with PlaywrightBackend(definition) as browser:
        await browser.load_html(html)
        agent = DynamicController(definition, browser, Policy(), feedback=Brain())
        result = await agent.run()
    assert result.status == 'success', result.reason
    assert result.actions == 3  # Submit, Yes, Close; no WAITs or resubmissions.
    assert not agent.pending and not agent.memory.pending_writes
    assert len(agent.memory.confirmed_writes) == 2  # The inspection close is never a business confirmation.
    confirmations = [e for e in agent.events if e['kind'] == 'transition_confirmed']
    assert [e['confirmation_scope'] for e in confirmations] == ['dialog_opened', 'business_commit']
    close_event = next(e for e in agent.events if e['kind'] == 'dialog_closed_for_readback')
    assert not close_event['original_action_confirmed']


async def test_invalid_explicit_readback_cannot_be_replaced_by_incidental_current_note():
    from jev_browser.dynamic import ReadbackUnresolved

    class Brain:
        async def review(self, *args, **kwargs):
            return Feedback(next_goal='Inspect', last_outcome='confirmed',
                            readback_quote='Invented successful write',
                            notes=[EvidenceNote(quote='A visible form')])

    agent = DynamicController(task(), None, None, feedback=Brain())
    with pytest.raises(ReadbackUnresolved):
        await agent.review(observation(), phase='action_readback')
    assert agent.feedback_calls == 2 and not agent.memory.evidence


async def test_exhausted_noncompletion_readback_is_attention_and_retains_unknown_operation():
    class Backend(UnknownBackend):
        async def execute(self, action):
            self.dispatched += 1
            return Receipt(action_id=action.id, status='ok')

    class Brain:
        async def review(self, *args, phase, **kwargs):
            if phase == 'initial':
                return Feedback(next_goal='Click once and inspect')
            return Feedback(next_goal='Need proof', last_outcome='confirmed',
                            notes=[EvidenceNote(quote='Invented success')])

    backend = Backend()
    agent = DynamicController(task(), backend, FirstClick(), feedback=Brain(),
                              budget=Budget(readback_waits=1, no_progress_limit=100))
    result = await agent.run()
    assert result.status == 'needs_attention' and 'lacks current evidence' in result.reason
    assert backend.dispatched == 1 and agent.pending and agent.memory.pending_writes
    assert not agent.memory.confirmed_writes


@pytest.mark.parametrize('receipt_status,changed,expected', [('ok',True,True),('ok',False,False),('unknown',True,False)])
async def test_unknown_readback_refresh_is_bounded_and_never_confirms_or_replays(receipt_status, changed, expected):
    from jev_browser.dynamic import semantic_key
    before = observation(text='Command palette')
    fresh = observation(text='Search Help' if changed else 'Command palette')
    class Backend:
        calls = 0
        async def observe(self):
            self.calls += 1
            return fresh
    backend = Backend()
    agent = DynamicController(task(), backend, None, feedback=None)
    agent.pending = {'key':'help', 'dispatch_status':receipt_status, 'before_semantics':semantic_key(before)}
    agent.memory.pending_writes['help'] = agent.pending
    agent.consumed.add('help')
    original = agent.pending
    assert await agent.refresh_unknown_readback(before) == expected
    assert not await agent.refresh_unknown_readback(before)
    assert backend.calls == int(receipt_status == 'ok')
    assert agent.pending is original and agent.memory.pending_writes['help'] is original
    assert agent.consumed == {'help'} and not agent.memory.confirmed_writes and not agent.memory.events


async def test_async_help_arriving_during_unknown_review_is_reassessed_without_repeat():
    class Backend:
        stage = 0
        actions = []
        async def observe(self):
            if self.stage == 0:
                return observation(text='Command palette', elements=[Element(id='help',role='button',name='help (icon control)')]).model_copy(update={'observation_id':'before-help'})
            if self.stage == 1:
                return observation(text='Command palette loading', elements=[Element(id='help',role='button',name='help (icon control)')]).model_copy(update={'observation_id':'loading-help'})
            if self.stage == 2:
                return observation(text='Search Help',dialogs=['Search Help'],elements=[Element(id='close',role='button',name='Close')]).model_copy(update={'observation_id':'fresh-help'})
            return observation(text='Help closed',elements=[])
        async def execute(self, action):
            self.actions.append(action.element_ref)
            self.stage = 1 if action.element_ref == 'help' else 3
            return Receipt(action_id=action.id,status='ok')
    backend = Backend()
    class Policy:
        async def choose(self, task, obs, memory, contract, candidates):
            if backend.stage == 3:
                return Decision(choice=next(a.id for a in candidates if a.operation==Operation.FINISH),outcome='confirmed')
            target = 'close' if backend.stage == 2 else 'help'
            return Decision(choice=next(a.id for a in candidates if a.element_ref==target),
                            outcome='confirmed' if backend.stage==2 else 'unknown')
    class Brain:
        async def review(self, task, obs, memory, *, phase, **kwargs):
            if phase=='initial':
                return Feedback(next_goal='Open help then close it')
            if phase=='uncertain_outcome':
                backend.stage = 2  # The asynchronous modal arrives during the slow model call.
                return Feedback(next_goal='Inspect the fresh modal',last_outcome='unknown')
            return Feedback(next_goal='Done', complete=True, answer='Help inspected and closed',
                            notes=[EvidenceNote(quote='Help closed')])
    agent = DynamicController(task(),backend,Policy(),feedback=Brain())
    result = await agent.run()
    assert result.status=='success', result.reason
    assert backend.actions == ['help','close']
    assert not agent.pending
    assert any(e['kind']=='unknown_readback_refreshed' and e['changed'] and not e['action_confirmed'] for e in agent.events)


def test_stale_shortcut_cannot_be_reselected_as_an_ordinary_button():
    old = observation(elements=[Element(id='e1', role='button', name='Close dialog (Escape shortcut)',
                                       context='esc to close', activation_key='Escape')])
    new = observation(elements=[old.elements[0].model_copy(update={'activation_key':None})])
    agent = DynamicController(task(), None, None, feedback=None)
    agent.stale_click = {'element':old.elements[0].model_dump(), 'attempts':1,
                        'url':old.url, 'tab_id':old.tab_id, 'title':old.title, 'dialogs':old.dialogs}
    assert agent.refreshed_stale_click(new, generate_dynamic(new, task())) is None


async def test_same_value_input_clears_retry_cache_and_suppresses_only_unchanged_scope():
    class Brain:
        calls = 0
        async def value(self, *args):
            self.calls += 1
            return 'Ada' if self.calls == 1 else 'Gold'

    el = Element(id='name', role='textbox', name='Name', value='Ada', editable=True)
    obs = observation(elements=[el])
    brain = Brain()
    agent = DynamicController(task(), None, None, feedback=brain)
    action = next(a for a in generate_dynamic(obs, task())
                  if a.operation == Operation.FILL and a.bound_value is None)
    await agent.bind_input(action, obs)
    assert agent.input_retry
    await agent.perform(action, obs)
    assert agent.input_retry is None and not agent.pending and not agent.consumed
    candidates = generate_dynamic(obs, task(), suppressed_inputs=agent.input_suppression(obs))
    assert not any(a.operation == Operation.FILL and a.bound_value is None for a in candidates)
    clear = next(a for a in candidates if a.operation == Operation.FILL and a.bound_value == '')
    await agent.bind_input(clear, obs)
    assert brain.calls == 1  # Visible reset binds empty text without another value request.
    assert any(e['kind'] == 'input_binding' and e.get('source', {}).get('kind') == 'observed_input_reset'
               for e in agent.events)

    fresh = obs.model_copy(update={'observation_id': 'o2'})
    assert agent.input_suppression(fresh)  # New capture IDs do not reset suppression.
    agent.memory.feedback['next_goal'] = 'Correct this field to Gold'
    assert not agent.input_suppression(fresh)
    action = next(a for a in generate_dynamic(fresh, task())
                  if a.operation == Operation.FILL and a.bound_value is None)
    await agent.bind_input(action, fresh)
    assert brain.calls == 2 and action.bound_value == 'Gold'
    await agent.perform(action.model_copy(update={'bound_value': 'Ada'}), fresh)
    changed = fresh.model_copy(update={'elements': [el.model_copy(update={'value': 'Gold'})]})
    assert not agent.input_suppression(changed)


def test_clear_candidate_never_resets_hidden_password_or_read_only_field():
    obs = observation(elements=[
        Element(id='password', role='textbox', name='Password', value='[redacted]', editable=True),
        Element(id='company', role='status', name='Company', value='Existing', read_only=True),
        Element(id='disabled', role='textbox', name='Disabled', value='Existing', editable=True, enabled=False),
    ])
    assert not any(a.operation == Operation.FILL and a.bound_value == ''
                   for a in generate_dynamic(obs, task()))


async def test_disappeared_stale_option_recovers_with_reset_without_same_value_loop():
    class Backend:
        stale = False
        committed = False
        value = 'Rajesh Kumar'
        observations = 0
        dispatched = []
        async def observe(self):
            self.observations += 1
            elements = [Element(id='name', role='textbox', name='Name', value='Ada', editable=True),
                        Element(id='user', role='combobox', name='User', value=self.value, editable=True)]
            if not self.committed:
                options = ['Rajesh Kumar'] if not self.stale or self.value == 'Rajesh' else ['Create a new User', 'Advanced Search']
                elements += [Element(id='option'+str(i), role='option', name=name) for i,name in enumerate(options)]
            return observation(text='User committed' if self.committed else 'Pick a linked User',
                               elements=elements).model_copy(update={'observation_id':f'o{self.observations}'})
        async def execute(self, action):
            self.dispatched.append((action.operation, action.element_ref, action.bound_value))
            if action.operation == Operation.CLICK and not self.stale:
                self.stale = True
                return Receipt(action_id=action.id, status='stale')
            if action.operation == Operation.FILL:
                assert action.element_ref == 'user'
                self.value = action.bound_value
            elif action.operation == Operation.CLICK:
                assert self.value == 'Rajesh' and action.element_ref == 'option0'
                self.committed = True
                self.value = 'rajesh@example.test'
            return Receipt(action_id=action.id, status='ok')

    class Policy:
        tested_noop = False
        async def choose(self, task, obs, memory, contract, candidates):
            if not backend.stale:
                chosen = next(a for a in candidates if a.operation == Operation.CLICK and a.element_ref == 'option0')
            elif not self.tested_noop:
                assert memory.feedback['next_goal'] == 'Reset User and query Rajesh'
                self.tested_noop = True
                chosen = next(a for a in candidates if a.operation == Operation.FILL and a.element_ref == 'name' and a.bound_value is None)
            elif backend.committed:
                chosen = next(a for a in candidates if a.operation == Operation.FINISH)
            elif backend.value == 'Rajesh':
                chosen = next(a for a in candidates if a.operation == Operation.CLICK and a.element_ref == 'option0')
            else:
                if backend.value == 'Rajesh Kumar':
                    assert not any(a.operation == Operation.FILL and a.element_ref == 'name' and a.bound_value is None for a in candidates)
                chosen = next(a for a in candidates if a.operation == Operation.FILL and a.element_ref == 'user'
                              and a.bound_value == ('' if backend.value else None))
            return Decision(choice=chosen.id, outcome='confirmed' if memory.pending_writes else 'none')

    class Brain:
        phases = []
        values = 0
        async def value(self, task, obs, memory, action):
            self.values += 1
            return 'Ada' if action.element_ref == 'name' else 'Rajesh'
        async def review(self, task, obs, memory, *, phase, transition, **kw):
            self.phases.append(phase)
            if phase == 'stale_target_changed':
                assert transition['receipt']['status'] == 'stale'
                assert not memory.pending_writes
            return Feedback(next_goal='Reset User and query Rajesh' if backend.stale else 'Select User',
                            complete=backend.committed and phase == 'finish',
                            answer='User committed' if backend.committed else '',
                            notes=[EvidenceNote(quote=obs.text)])

    backend, brain = Backend(), Brain()
    goal = task().model_copy(update={'objective':'Preserve Name Ada; select linked User Rajesh Kumar.'})
    agent = DynamicController(goal, backend, Policy(), feedback=brain,
                              budget=Budget(max_cycles=20, no_progress_limit=3))
    result = await agent.run()
    assert result.status == 'success', result.reason
    assert 'stale_target_changed' in brain.phases and 'no_progress' not in brain.phases
    assert [v for op, _, v in backend.dispatched if op == Operation.FILL] == ['', 'Rajesh']
    assert len(backend.dispatched) == 4 and brain.values == 2
    assert sum(e['kind'] == 'input_already_satisfied' for e in agent.events) == 1
    assert not any(e['kind'] == 'input_reused' for e in agent.events)
    assert not agent.pending
