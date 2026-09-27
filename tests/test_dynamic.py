import json

import httpx
import pytest
from pydantic import ValidationError

from jev_browser.browser import PlaywrightBackend
from jev_browser.controller import Controller
from jev_browser.dynamic import (
    DynamicController,
    EvidenceNote,
    Feedback,
    JsonFeedback,
    action_key,
    evidence_text,
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
        text="A visible form",
        **kw,
    )


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
            return Feedback(next_goal="Done", notes=[EvidenceNote(quote="Invented success")])

    backend = UnknownBackend()
    agent = DynamicController(task(), backend, FirstClick(), feedback=Hallucinating())
    result = await agent.run()
    assert result.status == "failed" and result.feedback_calls == 2
    assert backend.dispatched == 0
    assert all(
        "Invented success" not in e["source"]["quote"] for e in agent.memory.evidence.values()
    )


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
            assert set(content["schema"]["properties"]) == {"next_goal", "working_memory"}
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
