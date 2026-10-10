"""Policy-led execution changes scheduling without releasing uncertain submissions."""
import time
from unittest.mock import AsyncMock

import pytest

from jev_browser.browser import PlaywrightBackend
from jev_browser.dynamic import DynamicController, Feedback, generate_dynamic, planning_location
from jev_browser.jev_loop import input_context_key, policy_context
from jev_browser.models import JevPolicy, state
from jev_browser.protocol import (
    AgentTuning,
    Budget,
    Decision,
    Element,
    Observation,
    Operation,
    Receipt,
    Task,
)


def page(**updates):
    obs = Observation(observation_id="before", document_version="v1", document_id="doc1",
        tab_id="tab", url="http://example.test/form", title="Draft", text="Name Date Save",
        elements=[Element(id="name", role="textbox", name="Name", editable=True),
                  Element(id="date", role="textbox", name="Date", editable=True,
                          input_type="text", placeholder="dd-mm-yyyy"),
                  Element(id="save", role="button", name="Save")])
    return obs.model_copy(update=updates, deep=True)


def controller(**kwargs):
    brain = AsyncMock()
    brain.review.return_value = Feedback(next_goal="Fill the form and finish the remaining work")
    brain.value.return_value = "2026-06-30"
    browser = AsyncMock()
    browser.execute.side_effect = lambda a: Receipt(action_id=a.id, status="ok")
    return DynamicController(Task(id="loop", objective="Create a record, then do other requested work",
        control_mode="dynamic", allowed_origins=["http://example.test"]), browser, AsyncMock(),
        feedback=brain, tuning=AgentTuning(feedback_mode="jev_led"),
        budget=Budget(max_cycles=3, confidence_threshold=.5), **kwargs)


def action(agent, obs, ref, operation):
    return next(a for a in generate_dynamic(obs, agent.task)
                if a.element_ref == ref and a.operation == operation and a.bound_value is None)


async def test_prepared_value_is_not_dispatched_until_fresh_jev_selection():
    agent, obs = controller(), page()
    agent.backend.observe.side_effect = [obs, page(observation_id="second"), page(observation_id="third")]
    seen = []

    async def choose(task, current, memory, contract, candidates):
        fills = [a for a in candidates if a.element_ref == "date" and a.operation == Operation.FILL]
        seen.append(fills[0].bound_value)
        return Decision(choice=fills[0].id, confidence=.9, outcome="pending" if agent.pending else None)

    agent.policy.choose.side_effect = choose
    await agent.run()
    assert seen[:2] == [None, "2026-06-30"]
    assert agent.feedback_model.value.await_count == 1
    assert agent.feedback_model.review.await_count == 1  # Initial only.
    assert agent.backend.execute.await_count == 2  # Fill, then bounded pending wait.
    assert agent.backend.execute.await_args_list[0].args[0].bound_value == "2026-06-30"
    assert next(e for e in agent.events if e["kind"] == "input_prepared")["browser_action_dispatched"] is False


async def test_changed_dependencies_expire_prepared_value():
    agent, obs = controller(), page()
    selected = action(agent, obs, "date", Operation.FILL)
    await agent.bind_input(selected, obs)
    key = input_context_key(agent.memory, obs, obs.elements[1])
    assert key in agent.jev_inputs
    fresh = page(observation_id="fresh")
    fresh.elements[0].value = "Different employee"
    candidates = agent.generate_stage_candidates(fresh, limit=250, offset=0)
    candidate = next(a for a in candidates if a.element_ref == "date" and a.operation == Operation.FILL)
    assert candidate.bound_value is None and candidate.description.startswith("NO ACTION")


async def test_new_dialog_can_offer_live_controls_without_old_ds_scope():
    agent, obs = controller(), page(dialogs=["New form"])
    agent.fresh_scope_required = True
    agent.memory.feedback["execution_scope"] = {"environment_id":agent.memory.environment_id,
        "location":["old"], "dialogs":[], "bindings":{}}
    candidates = agent.generate_stage_candidates(obs, limit=250, offset=0)
    assert any(a.element_ref == "date" for a in candidates)
    agent.task.allowed_operations = [Operation.WAIT]
    assert not any(a.element_ref == "date" for a in agent.generate_stage_candidates(obs, limit=250, offset=0))


async def test_navigation_and_interval_do_not_schedule_ds():
    agent = controller()
    agent.backend.observe.side_effect = [page(observation_id=str(i), url=f"http://example.test/page{i}")
                                        for i in range(3)]
    agent.budget.brain_interval = 1
    agent.effective_actions = 2

    async def choose(task, obs, memory, contract, candidates):
        return Decision(choice=next(a.id for a in candidates if a.operation == Operation.WAIT), confidence=.9)

    agent.policy.choose.side_effect = choose
    await agent.run()
    assert agent.feedback_model.review.await_count == 1
    assert any(e["kind"] == "brain_checkpoint_skipped" for e in agent.events)


async def test_low_confidence_still_escalates_without_dispatch():
    agent = controller()
    agent.backend.observe.return_value = page()
    agent.budget.max_cycles = 2

    async def choose(task, obs, memory, contract, candidates):
        return Decision(choice=next(a.id for a in candidates if a.element_ref == "save"), confidence=.29)

    agent.policy.choose.side_effect = choose
    await agent.run()
    agent.backend.execute.assert_not_awaited()
    assert [e["reason"] for e in agent.events if e["kind"] == "brain_requested"] == ["initial", "low_confidence"]


@pytest.mark.parametrize("escalation", ["low_confidence", "jev_requested"])
async def test_new_page_checkpoint_cannot_overwrite_explicit_upgrade(escalation):
    agent = controller()
    agent.backend.observe.side_effect = [page(), page(url="http://example.test/new", observation_id="second"),
                                        page(url="http://example.test/new", observation_id="third")]

    async def choose(task, obs, memory, contract, candidates):
        if obs.url.endswith("/form"):
            return Decision(choice=next(a.id for a in candidates if a.operation == Operation.WAIT), confidence=.9)
        selected = next(a for a in candidates if a.element_ref == "save") if escalation == "low_confidence" else next(
            a for a in candidates if a.operation == Operation.REPLAN)
        return Decision(choice=selected.id, confidence=.29)

    agent.policy.choose.side_effect = choose
    await agent.run()
    phases = [e["reason"] for e in agent.events if e["kind"] == "brain_requested"]
    assert phases == ["initial", escalation]
    assert agent.feedback_model.review.await_count == 2
    assert all(call.args[0].operation == Operation.WAIT for call in agent.backend.execute.await_args_list)


async def test_unchanged_new_page_still_reaches_no_progress_watchdog():
    agent = controller()
    agent.budget.max_cycles = 5
    agent.budget.no_progress_limit = 2
    agent.backend.observe.side_effect = [page()] + [page(url="http://example.test/new", observation_id=str(i))
                                                   for i in range(4)]

    async def choose(task, obs, memory, contract, candidates):
        return Decision(choice=next(a.id for a in candidates if a.operation == Operation.WAIT), confidence=.9)

    agent.policy.choose.side_effect = choose
    await agent.run()
    assert "no_progress" in [e["reason"] for e in agent.events if e["kind"] == "brain_requested"]


async def test_local_failed_fill_can_be_released_once_without_confirmation():
    agent, obs = controller(), page()
    selected = action(agent, obs, "date", Operation.FILL)
    selected.bound_value = "2026-06-30"
    await agent.perform(selected, obs)
    agent.pending_started = time.monotonic() - 2
    fresh = page(observation_id="fresh")
    assert agent.release_unapplied_local_fill(fresh)
    assert not agent.pending and not agent.memory.pending_writes
    assert not agent.memory.confirmed_actions and not agent.memory.write_checkpoints
    selected = action(agent, fresh, "date", Operation.FILL)
    selected.bound_value = "30-06-2026"
    await agent.perform(selected, fresh)
    agent.pending_started = time.monotonic() - 2
    assert not agent.release_unapplied_local_fill(page(observation_id="fresh2"))


@pytest.mark.parametrize("change", ["save", "unknown", "different_document", "nonempty", "loading", "password"])
async def test_not_applied_never_releases_uncertain_submit_or_ambiguous_field(change):
    agent, obs = controller(), page()
    selected = action(agent, obs, "save" if change == "save" else "date",
                      Operation.CLICK if change == "save" else Operation.FILL)
    if change == "password":
        obs.elements[1].input_type = "password"
    if selected.operation == Operation.FILL:
        selected.bound_value = "2026-06-30"
    await agent.perform(selected, obs)
    agent.pending_started = time.monotonic() - 2
    fresh = page(observation_id="fresh")
    if change == "unknown":
        agent.pending["dispatch_status"] = "unknown"
    elif change == "different_document":
        fresh.document_id = "other"
    elif change == "nonempty":
        fresh.elements[1].value = "30-06-2026"
    elif change == "loading":
        fresh.loading = True
    assert not agent.release_unapplied_local_fill(fresh)
    assert agent.pending and agent.memory.pending_writes


def test_policy_context_preserves_goal_pending_and_business_proofs():
    agent, obs = controller(), page()
    agent.memory.pending_writes["p"] = {"action":{"operation":"fill", "description":"Date", "bound_value":"2026-06-30"},
        "waits":1, "expected_goal":"Date visibly populated"}
    agent.memory.write_checkpoints.append({"environment_id":agent.memory.environment_id,
        "target":"Save", "status":"business_commit_confirmed", "fields":[{"name":"Name","value":"Alice"}]})
    agent.memory.feedback.update(execution_scope={"environment_id":agent.memory.environment_id,
        "location":list(planning_location(obs)), "dialogs":[], "bindings":{}},
        inputs=[{"name":"Date", "value":"2026-06-30"}], working_memory="Payment still pending")
    projected = policy_context(agent.memory, obs, state(agent.task, obs, agent.memory, None))
    assert projected["trusted_goal"] == agent.task.objective
    m = projected["untrusted_memory"]
    assert m["pending_writes"][0]["expected_goal"] == "Date visibly populated"
    assert m["business_checkpoints"][0]["fields"][0]["value"] == "Alice"
    assert m["planned_inputs"][0]["value"] == "2026-06-30"
    assert m["working_memory"] == "Payment still pending"
    assert "write_checkpoints" not in m
    assert len(agent.memory.write_checkpoints) == 1  # Projection does not erase archives.


async def test_date_widget_metadata_is_observed_without_application_internals():
    task = Task(id="date-widget", objective="Fill date", control_mode="dynamic", sandbox=True)
    async with PlaywrightBackend(task) as browser:
        await browser.load_html('<label>Date<input type="text" placeholder="dd-mm-yyyy" required></label>')
        obs = await browser.observe()
        e = next(e for e in obs.elements if e.name == "Date")
        assert e.input_type == "text" and e.placeholder == "dd-mm-yyyy"
        assert e.required and e.validation_message


async def test_actual_jev_request_exposes_local_failure_choice_and_keeps_original_goal():
    agent, obs = controller(), page()
    agent.memory.pending_writes["p"] = {"action":{"operation":"fill", "description":"Date", "bound_value":"2026-06-30"},
        "waits":1, "expected_goal":"Date visibly populated"}
    transport = AsyncMock()
    transport.model, transport.observer = "jev-latest", None

    async def respond(payload, kind):
        assert kind == "jev"
        assert payload["state"]["trusted_goal"] == agent.task.objective
        assert payload["state"]["policy_context_profile"] == "jev_led_v1"
        assert "not_applied" in payload["questions"]["outcome"]["criteria"]
        choices = payload["questions"]["action"]["criteria"]
        choice = next(k for k, v in choices.items() if v["operation"] == "wait")
        return {"answers":{"action":{"type":"choice", "choice":choice, "confidence":.9},
                           "outcome":{"type":"choice", "choice":"not_applied"}}}

    transport.post.side_effect = respond
    policy = JevPolicy(transport, context_max_bytes=200000)
    candidates = agent.generate_stage_candidates(obs, limit=250, offset=0)
    decision = await policy.choose(agent.task, obs, agent.memory, None, candidates)
    assert decision.outcome == "not_applied"
