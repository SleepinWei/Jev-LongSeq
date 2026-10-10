import json
from unittest.mock import AsyncMock

import pytest

from jev_browser.browser import PlaywrightBackend
from jev_browser.dynamic import (
    DynamicController,
    Feedback,
    JsonFeedback,
    RecoveryProbe,
    generate_dynamic,
    planning_location,
    write_prerequisite_diagnostics,
)
from jev_browser.jev_recovery import StallGuard, probe_key, work_signature
from jev_browser.memory import Memory
from jev_browser.models import JevPolicy
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
    return Observation(observation_id="one", document_version="v1", document_id="document",
        url="http://example.test/form", tab_id="tab", title="Draft", text="Employee Company Save",
        elements=[Element(id="employee", role="combobox", name="Employee", editable=True,
                          value="EMP-7", popup_open=False),
                  Element(id="company", role="textbox", name="Company", read_only=True, required=True),
                  Element(id="save", role="button", name="Save")]).model_copy(update=updates, deep=True)


def controller(**tuning):
    browser, brain, policy = AsyncMock(), AsyncMock(), AsyncMock()
    browser.observe.return_value = page()
    browser.execute.side_effect = lambda a: Receipt(action_id=a.id, status="ok")
    brain.review.return_value = Feedback(next_goal="Resolve Employee link, then Save")
    brain.inspect_recovery.return_value = RecoveryProbe(choice="stop", reason="No useful probe")
    policy.minimum_action_confidence = .5
    async def choose(task, obs, memory, contract, candidates):
        return Decision(choice=next(a.id for a in candidates if a.operation == Operation.REPLAN),
                        confidence=.37, probabilities={"a4": .37})
    policy.choose.side_effect = choose
    return DynamicController(Task(id="recover", objective="Create the Employee Separation",
        control_mode="dynamic", allowed_origins=["http://example.test"]), browser, policy,
        feedback=brain, tuning=AgentTuning(feedback_mode="jev_led", **tuning),
        budget=Budget(max_cycles=15, max_seconds=60, confidence_threshold=.5))


def test_work_state_ignores_popup_handle_churn_and_retains_oscillation_allowance():
    obs, guard = page(), StallGuard(2, 3)
    record = guard.current(obs)
    record["interventions"] = 2
    fresh = page(observation_id="new", document_id="reloaded", text="Additional popup text")
    fresh.elements[0].id, fresh.elements[0].popup_open = "new-handle", True
    fresh.elements.append(Element(id="option", role="option", name="EMP-7 Ada", option_owner="new-handle"))
    assert work_signature(obs) == work_signature(fresh)
    assert guard.current(fresh) is record
    fresh.elements[1].value = "Company Ltd"
    assert guard.current(fresh)["interventions"] == 0
    fresh.elements[1].value = ""
    assert guard.current(fresh)["interventions"] == 2


async def test_ds_notes_and_new_evidence_cannot_reset_repeated_interventions():
    agent = controller()
    async def review(task, obs, memory, **kwargs):
        memory.evidence[str(len(memory.evidence))] = {"source": {"quote": "Employee"}}
        return Feedback(next_goal=f"Fresh planner wording {len(memory.evidence)}")
    agent.feedback_model.review.side_effect = review
    result = await agent.run()
    assert result.status == "needs_attention" and "declined" in result.reason
    assert agent.feedback_model.review.await_count == 3  # Initial + two interventions.
    assert agent.feedback_model.inspect_recovery.await_count == 1
    agent.backend.execute.assert_not_awaited()


async def test_recovery_requests_and_dispatches_have_a_hard_per_state_cap():
    agent = controller(jev_recovery_probes=1)
    async def probe(task, obs, memory, candidates, diagnostic):
        return RecoveryProbe(choice=next(a.id for a in candidates if a.operation == Operation.SCROLL),
                             reason="Inspect another visible region")
    agent.feedback_model.inspect_recovery.side_effect = probe
    result = await agent.run()
    assert result.status == "needs_attention" and "exhausted" in result.reason
    assert agent.feedback_model.review.await_count == 3
    assert agent.feedback_model.inspect_recovery.await_count == 1
    assert agent.backend.execute.await_count == 1
    assert agent.backend.execute.await_args.args[0].operation == Operation.SCROLL


async def test_no_recovery_probes_with_pending_write_and_no_resubmission():
    agent, obs = controller(), page()
    obs.elements[1].value = "Company Ltd"
    agent.backend.observe.return_value = obs
    save = next(a for a in generate_dynamic(obs, agent.task) if a.element_ref == "save")
    assert await agent.perform(save, obs) is None
    pending = agent.pending
    agent.initial_phase = ""
    result = await agent.run()
    assert "unconfirmed action" in result.reason
    assert agent.pending is pending and agent.memory.pending_writes
    agent.feedback_model.inspect_recovery.assert_not_awaited()
    assert agent.backend.execute.await_count == 1


async def test_new_dispatch_gets_bounded_readback_even_after_same_state_planning_exhausted():
    agent, obs = controller(), workspace()
    opener = Element(id="opener", role="button", name="Frappe HR")
    obs.elements.append(opener)
    agent.stall_guard.current(obs)["interventions"] = 2
    click = next(a for a in generate_dynamic(obs, agent.task) if a.element_ref == "opener")
    assert await agent.perform(click, obs) is None
    pending = agent.pending
    # Opening the workspace menu is new information, not a new work state.
    fresh = workspace(observation_id="new", text="Frappe HR People", dialogs=["Frappe HR\nPeople"])
    agent.backend.observe.return_value = fresh
    assert work_signature(fresh) == work_signature(obs)
    agent.initial_phase = "jev_requested"
    result = await agent.run()
    assert result.status == "needs_attention" and "unconfirmed action" in result.reason
    assert agent.pending is pending and pending["stall_readback_reviews"] == 2
    assert agent.feedback_model.review.await_count == 2
    assert all(call.kwargs["phase"] == "action_readback" for call in agent.feedback_model.review.await_args_list)
    agent.feedback_model.inspect_recovery.assert_not_awaited()
    assert agent.backend.execute.await_count == 1  # Never replay opener or navigate.


async def test_fresh_local_readback_releases_opener_without_business_confirmation():
    agent, obs = controller(), workspace()
    obs.elements.append(Element(id="opener", role="button", name="Frappe HR"))
    agent.stall_guard.current(obs)["interventions"] = 2
    click = next(a for a in generate_dynamic(obs, agent.task) if a.element_ref == "opener")
    await agent.perform(click, obs)
    agent.backend.observe.return_value = workspace(observation_id="fresh", text="Frappe HR People",
                                                 dialogs=["Frappe HR\nPeople"])
    agent.feedback_model.review.return_value = Feedback(next_goal="Choose observed People link",
        last_outcome="confirmed", readback_quote="Frappe HR People")
    agent.initial_phase = "jev_requested"
    await agent.run()
    assert agent.pending is None and not agent.memory.pending_writes
    assert not agent.memory.write_checkpoints
    assert agent.memory.confirmed_actions[-1]["business_commit_confirmed"] is False
    assert agent.feedback_model.review.await_args_list[0].kwargs["phase"] == "action_readback"
    assert agent.backend.execute.await_count == 1


async def test_controller_local_context_survives_ds_feedback_replacement():
    agent, obs = controller(), page()
    agent.memory.feedback.update(jev_loop={"mode": "jev_led", "last_action": "open"},
                                 execution_feedback={"required_blank": ["Company"]})
    await agent.review(obs, phase="jev_requested")
    assert agent.memory.feedback["jev_loop"]["last_action"] == "open"
    assert agent.memory.feedback["execution_feedback"]["required_blank"] == ["Company"]


async def test_repeated_input_helpers_with_dom_handle_churn_are_also_bounded():
    agent = controller()
    agent.feedback_model.value.return_value = "EMP-7"
    count = 0
    async def observe():
        nonlocal count
        count += 1
        obs = page(observation_id=str(count))
        obs.elements[0].id = f"employee-{count}"
        return obs
    agent.backend.observe.side_effect = observe
    async def choose(task, obs, memory, contract, candidates):
        return Decision(choice=next(a.id for a in candidates if a.operation == Operation.FILL
                                   and a.bound_value is None), confidence=.9)
    agent.policy.choose.side_effect = choose
    result = await agent.run()
    assert result.status == "needs_attention"
    assert agent.feedback_model.value.await_count == 2
    assert agent.feedback_model.inspect_recovery.await_count == 1
    agent.backend.execute.assert_not_awaited()


def test_combobox_open_candidates_are_capability_scoped_and_not_native_select_clicks():
    agent, obs = controller(), page()
    assert any(a.element_ref == "employee" and a.operation == Operation.CLICK
               for a in generate_dynamic(obs, agent.task))
    for updates in ({"read_only": True}, {"popup_open": True}, {"selectable": True},
                    {"enabled": False}, {"value": "[redacted]"}):
        fresh = page()
        fresh.elements[0] = fresh.elements[0].model_copy(update=updates)
        assert not any(a.element_ref == "employee" and a.operation == Operation.CLICK
                       for a in generate_dynamic(fresh, agent.task))


@pytest.mark.parametrize("failure", [None, "wrong_owner", "changed_value", "new_error", "new_document"])
async def test_opening_combobox_requires_fresh_owned_options_and_never_confirms_business(failure):
    agent, obs = controller(), page(dialogs=["Edit row"])
    action = next(a for a in generate_dynamic(obs, agent.task)
                  if a.element_ref == "employee" and a.operation == Operation.CLICK)
    await agent.perform(action, obs)
    fresh = page(observation_id="two", document_version="v2", dialogs=["Edit row"])
    fresh.elements[0].popup_open = True
    fresh.elements.append(Element(id="option", role="option", name="EMP-7 Ada", option_owner="employee"))
    if failure == "wrong_owner":
        fresh.elements[-1].option_owner = "another"
    elif failure == "changed_value":
        fresh.elements[0].value = "EMP-8"
    elif failure == "new_error":
        fresh.errors = ["page_error:new failure"]
    elif failure == "new_document":
        fresh.document_id = "other"
    assert agent.confirm_visible_combobox(fresh) is (failure is None)
    assert not agent.memory.write_checkpoints
    assert all(not c["business_commit_confirmed"] for c in agent.memory.confirmed_actions)


def test_probe_excludes_save_arbitrary_fill_and_unowned_or_unplanned_options():
    agent, obs = controller(), page()
    obs.elements[0].popup_open = True
    obs.elements.extend([Element(id="mine", role="option", name="EMP-7 Ada", option_owner="employee"),
                         Element(id="wrong", role="option", name="EMP-8 Other", option_owner="employee"),
                         Element(id="unowned", role="option", name="EMP-7 Ada")])
    assert all(a.operation == Operation.SCROLL for a in agent.recovery_candidates(obs))
    owner = obs.elements[0]
    agent.last_brain_location = planning_location(obs)
    agent.memory.feedback.update(inputs=[{"name": "Employee", "value": "EMP-7"}],
        execution_scope={"environment_id": agent.memory.environment_id, "generation": agent.scope_generation,
            "location": list(planning_location(obs)), "dialogs": [],
            "bindings": {owner.id: {"operations": ["fill"], "role": owner.role,
                                   "name": owner.name, "grid_ref": None, "row_ref": None}}})
    probes = agent.recovery_candidates(obs)
    assert {a.element_ref for a in probes if a.element_ref} == {"mine"}
    assert all(a.operation not in {Operation.FILL, Operation.SELECT} for a in probes)


async def test_probe_reobserves_and_does_not_execute_an_old_ds_choice():
    agent, obs = controller(), page()
    async def probe(task, current, memory, candidates, diagnostic):
        return RecoveryProbe(choice=next(a.id for a in candidates if a.element_ref == "employee"),
                             reason="Open Employee options")
    agent.feedback_model.inspect_recovery.side_effect = probe
    fresh = page(observation_id="new", text="Changed form")
    fresh.elements[0].value = "Other"
    agent.backend.observe.return_value = fresh
    assert await agent.recover_stall(obs) is None
    agent.backend.execute.assert_not_awaited()
    assert any(e["kind"] == "recovery_probe_stale" for e in agent.events)


def workspace(**updates):
    return page(url="http://example.test/desk/people", title="People", text="People Employee",
        elements=[Element(id="employee-link", role="link", name="Employee",
                          href="http://example.test/desk/employee")]).model_copy(update=updates, deep=True)


def test_static_workspace_exposes_navigation_and_destination_is_part_of_probe_identity():
    agent, obs = controller(), workspace()
    obs.elements.append(Element(id="second", role="link", name="Employee",
                                href="http://example.test/desk/employee-separation"))
    links = [a for a in agent.recovery_candidates(obs) if a.element_ref]
    assert {a.element_ref for a in links} == {"employee-link", "second"}
    assert all("destination=" in a.description for a in links)
    assert probe_key(links[0], obs) != probe_key(links[1], obs)
    agent.stall_guard.current(obs)["tried"].add(probe_key(links[0], obs))
    fresh = obs.model_copy(deep=True)
    fresh.elements[0].id = "new-handle"
    assert {a.element_ref for a in agent.recovery_candidates(fresh) if a.element_ref} == {"second"}


async def test_equivalent_sidebar_and_workspace_links_remain_one_fresh_navigation_probe():
    agent, obs = controller(), workspace()
    obs.elements.append(obs.elements[0].model_copy(update={"id": "workspace-link"}))
    links = [a for a in agent.recovery_candidates(obs) if a.element_ref]
    assert len(links) == 1 and links[0].element_ref == "employee-link"
    async def choose(task, current, memory, candidates, diagnostic):
        return RecoveryProbe(choice=next(a.id for a in candidates if a.element_ref), reason="Open Employee list")
    agent.feedback_model.inspect_recovery.side_effect = choose
    fresh = obs.model_copy(update={"observation_id": "fresh"}, deep=True)
    fresh.elements[0].id, fresh.elements[1].id = "new-sidebar", "new-card"
    agent.backend.observe.return_value = fresh
    assert await agent.recover_stall(obs) is None
    assert agent.backend.execute.await_args.args[0].element_ref == "new-sidebar"
    agent.pending = None
    agent.memory.pending_writes.clear()
    assert not any(a.element_ref for a in agent.recovery_candidates(fresh))


@pytest.mark.parametrize("change", ["editable", "selectable", "save", "unsaved", "dialog",
    "disabled", "readonly", "row", "same_url", "cross_origin", "javascript", "mutating", "api"])
def test_navigation_probe_cannot_abandon_form_or_dispatch_non_navigation(change):
    agent, obs = controller(), workspace()
    link = obs.elements[0]
    if change in {"editable", "selectable"}:
        obs.elements.append(Element(id="field", role="textbox", name="Draft value", **{change: True}))
    elif change == "save":
        obs.elements.append(Element(id="save", role="button", name="Save"))
    elif change == "unsaved":
        obs.text += " Not Saved"
    elif change == "dialog":
        obs.dialogs = ["Confirm leaving"]
    elif change == "disabled":
        link.enabled = False
    elif change == "readonly":
        link.read_only = True
    elif change == "row":
        link.grid_ref, link.row_ref = "grid", "draft-row"
    elif change == "same_url":
        link.href = obs.url
    elif change == "cross_origin":
        agent.task.allowed_origins.append("http://other.test")
        link.href = "http://other.test/desk/employee"
    elif change == "javascript":
        link.href = "javascript:deleteRecord()"
    elif change == "mutating":
        link.name, link.href = "Delete Employee", "http://example.test/delete/employee"
    elif change == "api":
        link.href = "http://example.test/api/method/action"
    assert not any(a.element_ref for a in agent.recovery_candidates(obs))


async def test_ds_selected_navigation_uses_fresh_handle_and_confirms_only_destination():
    agent, obs = controller(), workspace()
    async def choose(task, current, memory, candidates, diagnostic):
        return RecoveryProbe(choice=next(a.id for a in candidates if a.element_ref), reason="Open Employee list")
    agent.feedback_model.inspect_recovery.side_effect = choose
    fresh = workspace(observation_id="fresh")
    fresh.elements[0].id = "fresh-link"
    agent.backend.observe.return_value = fresh
    assert await agent.recover_stall(obs) is None
    action = agent.backend.execute.await_args.args[0]
    assert action.element_ref == "fresh-link" and action.observation_id == "fresh"
    assert agent.pending["navigation_target"] == "http://example.test/desk/employee"
    arrived = workspace(observation_id="arrived", url="http://example.test/desk/employee")
    assert agent.transition_is_observed(arrived)
    assert agent.confirm_transition("confirmed", arrived, "observed_navigation_destination")
    assert not agent.memory.write_checkpoints
    assert not agent.memory.confirmed_actions[-1]["business_commit_confirmed"]


async def test_changed_navigation_destination_after_ds_choice_is_not_dispatched():
    agent, obs = controller(), workspace()
    async def choose(task, current, memory, candidates, diagnostic):
        return RecoveryProbe(choice=next(a.id for a in candidates if a.element_ref), reason="Open observed link")
    agent.feedback_model.inspect_recovery.side_effect = choose
    fresh = workspace(observation_id="new")
    fresh.elements[0].href = "http://example.test/desk/other"
    agent.backend.observe.return_value = fresh
    assert await agent.recover_stall(obs) is None
    agent.backend.execute.assert_not_awaited()


async def test_ds_probe_schema_only_accepts_supplied_candidates_and_rejects_truncation():
    agent, obs = controller(), page()
    transport = AsyncMock()
    candidates = agent.recovery_candidates(obs)
    transport.post.return_value = {"choices": [{"message": {"content": json.dumps(
        {"choice": "stop", "reason": "No useful new view"})}, "finish_reason": "stop"}]}
    verdict = await JsonFeedback(transport).inspect_recovery(agent.task, obs, agent.memory, candidates, {})
    assert verdict.choice == "stop"
    payload, kind = transport.post.await_args.args
    context = json.loads(payload["messages"][1]["content"])
    assert kind == "dynamic_recovery_probe" and payload["max_tokens"] == 2048
    assert context["trusted_goal"] == agent.task.objective
    assert "save" not in {a["element_ref"] for a in context["candidates"]}
    transport.post.return_value["choices"][0]["message"]["content"] = '{"choice":"save","reason":"Try Save"}'
    with pytest.raises(ValueError, match="unknown candidate"):
        await JsonFeedback(transport).inspect_recovery(agent.task, obs, agent.memory, candidates, {})
    transport.post.return_value["choices"][0]["finish_reason"] = "length"
    with pytest.raises(ValueError, match="truncated"):
        await JsonFeedback(transport).inspect_recovery(agent.task, obs, agent.memory, candidates, {})


async def test_long_advisory_reason_does_not_invalidate_exact_probe_choice():
    agent, obs = controller(), page()
    candidates = agent.recovery_candidates(obs)
    opener = next(a for a in candidates if a.element_ref == "employee")
    transport = AsyncMock()
    transport.post.return_value = {"choices": [{"message": {"content": json.dumps(
        {"choice": opener.id, "reason": "Open observed Employee options. " * 25})},
        "finish_reason": "stop"}]}
    verdict = await JsonFeedback(transport).inspect_recovery(agent.task, obs, agent.memory, candidates, {})
    assert verdict.choice == opener.id and len(verdict.reason) == 500
    assert RecoveryProbe.model_json_schema()["properties"]["reason"]["maxLength"] == 500
    transport.post.return_value["choices"][0]["message"]["content"] = json.dumps(
        {"choice": "save", "reason": "Try writing. " * 100})
    with pytest.raises(ValueError, match="unknown candidate"):
        await JsonFeedback(transport).inspect_recovery(agent.task, obs, agent.memory, candidates, {})


@pytest.mark.parametrize("response", [
    {"choice": "stop", "reason": []}, {"choice": "stop"},
    {"choice": "stop", "reason": "Stop", "value": "invented"},
])
async def test_invalid_probe_schema_stops_without_dispatch_or_retry(response):
    agent, obs = controller(), page()
    agent.feedback_model.inspect_recovery.return_value = response
    reason = await agent.recover_stall(obs)
    assert "invalid choice/schema" in reason
    agent.backend.execute.assert_not_awaited()
    agent.backend.observe.assert_not_awaited()
    assert agent.feedback_model.inspect_recovery.await_count == 1
    assert agent.stall_guard.current(obs)["probes"] == 1
    assert any(e["kind"] == "recovery_probe_invalid" for e in agent.events)


async def test_rejected_probe_choice_stops_without_retrying_model_or_action():
    agent, obs = controller(), page()
    agent.feedback_model.inspect_recovery.side_effect = ValueError("unknown candidate")
    assert "invalid choice/schema" in await agent.recover_stall(obs)
    agent.feedback_model.inspect_recovery.assert_awaited_once()
    agent.backend.execute.assert_not_awaited()


@pytest.mark.parametrize("endpoint,model,disabled", [
    ("https://api.deepseek.com/chat/completions", "deepseek-flash", True),
    ("https://api.deepseek.com/v1/chat/completions", "deepseek-pro", True),
    ("https://example.test/chat/completions", "deepseek-flash", False),
    ("https://api.deepseek.com/chat/completions", "other-model", False),
])
async def test_only_official_ds_recovery_choice_disables_hidden_thinking(endpoint, model, disabled):
    agent, obs = controller(), page()
    transport = AsyncMock()
    transport.endpoint, transport.model = endpoint, model
    transport.post.return_value = {"choices": [{"message": {"content":
        '{"choice":"stop","reason":"No useful probe"}'}, "finish_reason": "stop"}]}
    result = await JsonFeedback(transport).inspect_recovery(
        agent.task, obs, agent.memory, agent.recovery_candidates(obs), {})
    payload, kind = transport.post.await_args.args
    assert result.choice == "stop" and kind == "dynamic_recovery_probe"
    assert payload["max_tokens"] == 2048
    if disabled:
        assert payload["thinking"] == {"type": "disabled"}
    else:
        assert "thinking" not in payload


async def test_provider_probabilities_are_advisory_and_invalid_or_unknown_entries_discarded():
    agent, obs = controller(), page()
    candidates = generate_dynamic(obs, agent.task)
    transport = AsyncMock()
    transport.model, transport.observer = "jev-latest", None
    transport.post.return_value = {"answers": {"action": {"type": "choice", "choice": "a4",
        "confidence": .37, "probabilities": {"a4": .37, "a0": .23, "a1": True, "a2": 4,
                                               "a3": float("nan"), "unknown": .24}}}}
    decision = await JevPolicy(transport, context_max_bytes=100000).choose(
        agent.task, obs, agent.memory, None, candidates)
    assert decision.probabilities == {"a4": .37, "a0": .23}
    assert decision.confidence == .37 and decision.choice == "a4"
    opener = next(a.id for a in candidates if a.element_ref == "employee" and a.operation == Operation.CLICK)
    sent = transport.post.await_args.args[0]
    assert "no value is selected or saved" in sent["questions"]["action"]["criteria"][opener]["meaning"]


async def test_required_field_label_does_not_make_its_open_link_button_a_blank_field():
    task = Task(id="required", objective="Fill fields", control_mode="dynamic", sandbox=True)
    async with PlaywrightBackend(task) as browser:
        await browser.load_html('''<style>.req::after { content: '*'; }</style>
          <div class="frappe-control"><label class="req">Employee</label>
            <input role="combobox" value="EMP-7"><button>Open Link</button></div>
          <label>Company<input aria-required="true" readonly></label>''')
        obs = await browser.observe()
        employee = next(e for e in obs.elements if e.role == "combobox")
        link = next(e for e in obs.elements if e.name == "Open Link")
        assert employee.required and not link.required
        errors = write_prerequisite_diagnostics(obs, Memory(), [])
        assert all(e["name"] != "Open Link" for e in errors)
        assert any(e["name"] == "Company" for e in errors)


async def test_actual_browser_combobox_open_and_option_resolves_derived_field():
    task = Task(id="combo", objective="Select Employee EMP-7", control_mode="dynamic", sandbox=True)
    async with PlaywrightBackend(task) as browser:
        await browser.load_html('''<div><label>Employee<input id="employee" role="combobox" value="EMP-7"
          onclick="document.getElementById('options').hidden=false"></label>
          <div id="options" hidden><div role="option" onclick="
            document.getElementById('company').value='Company Ltd';
            document.getElementById('options').hidden=true;">EMP-7 Ada</div></div></div>
          <label>Company<input id="company" readonly required></label><button>Save</button>''')
        obs = await browser.observe()
        agent = DynamicController(task, browser, AsyncMock(), feedback=AsyncMock(),
                                  tuning=AgentTuning(feedback_mode="jev_led"))
        opener = next(a for a in generate_dynamic(obs, task) if a.operation == Operation.CLICK
                      and a.element_ref == next(e.id for e in obs.elements if e.name == "Employee"))
        assert await agent.perform(opener, obs) is None
        opened = await browser.observe()
        assert agent.confirm_visible_combobox(opened)
        option = next(e for e in opened.elements if e.role == "option")
        assert option.option_owner == opener.element_ref
        select = next(a for a in generate_dynamic(opened, task) if a.element_ref == option.id)
        assert await agent.perform(select, opened) is None
        after = await browser.observe()
        assert agent.confirm_visible_option(after)
        assert next(e for e in after.elements if e.name == "Company").value == "Company Ltd"
        assert not agent.memory.write_checkpoints  # Local UI, no business commit.
