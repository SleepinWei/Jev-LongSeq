"""DS may batch independent inputs; readback and business decisions remain separate."""
from unittest.mock import AsyncMock

import pytest

from jev_browser.dynamic import DynamicController, Feedback, InvalidInputValue
from jev_browser.models import JsonPolicy
from jev_browser.protocol import Budget, Decision, Element, Observation, Operation, Receipt, Task


def page():
    return Observation(observation_id="o1", document_version="v1", tab_id="t", url="about:blank",
        title="New vendor", text="First Name Last Name Email Save", elements=[
            Element(id="first", role="textbox", name="First Name", editable=True),
            Element(id="last", role="textbox", name="Last Name", editable=True),
            Element(id="email", role="textbox", name="Email", editable=True),
            Element(id="save", role="button", name="Save")])


async def controller(obs=None, **kwargs):
    obs = obs or page()
    brain = AsyncMock()
    brain.review.return_value = Feedback(next_goal="Fill independent vendor fields then Save",
        working_memory="Journal, Payment and CRM remain pending",
        inputs=[{"name": "First Name", "value": "Ada"}, {"name": "Last Name", "value": "Lovelace"},
                {"name": "Email", "value": "ada@example.test"}],
        stage_controls=[{"element_ref": ref, "operations": ["fill"]} for ref in ("first", "last", "email")]
                       + [{"element_ref": "save", "operations": ["click"]}],
        stage_entry={"intent": "act", "operation": "fill", "element_ref": "first"},
        input_sequence=["first", "last", "email"])
    brain.value.side_effect = lambda task, obs, memory, action: next(
        p["value"] for p in memory.feedback["inputs"]
        if p["name"] == next(e.name for e in obs.elements if e.id == action.element_ref))
    agent = DynamicController(Task(id="sequence", sandbox=True, control_mode="dynamic",
        objective="Create vendor Ada Lovelace, ada@example.test; then Journal, Payment and CRM."),
        AsyncMock(), JsonPolicy(AsyncMock()), feedback=brain, **kwargs)
    await agent.review(obs, phase="initial")
    return agent


def candidates(agent, obs):
    agent.refresh_stage_bindings(obs)
    return agent.generate_stage_candidates(obs, limit=250, offset=0)


async def first_input(agent, obs):
    actions = candidates(agent, obs)
    decision = agent.stage_entry_decision(obs, actions)
    action = next(a for a in actions if a.id == decision.choice)
    await agent.bind_input(action, obs)
    agent.backend.execute.return_value = Receipt(action_id=action.id, status="ok")
    await agent.perform(action, obs)
    fresh = obs.model_copy(deep=True)
    fresh.observation_id, fresh.document_version = "o2", "v2"
    fresh.elements[0].value = "Ada"
    assert agent.confirm_visible_input(fresh)
    return fresh


async def test_three_inputs_save_two_policy_calls_and_keep_save_pending():
    agent = await controller(budget=Budget(max_cycles=4))
    obs = page()
    version = 1

    async def observe():
        nonlocal version
        version += 1
        fresh = obs.model_copy(deep=True)
        fresh.observation_id, fresh.document_version = f"o{version}", f"v{version}"
        return fresh

    async def execute(action):
        if action.operation == Operation.FILL:
            next(e for e in obs.elements if e.id == action.element_ref).value = action.bound_value
        return Receipt(action_id=action.id, status="ok")

    async def choose(task, fresh, memory, contract, actions):
        return Decision(choice=next(a.id for a in actions if a.element_ref == "save"))

    # Review against the actual first observed frame, as the production loop does.
    agent.backend.observe.side_effect = observe
    agent.backend.execute.side_effect = execute
    agent.policy.choose = AsyncMock(side_effect=choose)
    result = await agent.dynamic_loop()
    assert result.status == "budget_exhausted"
    assert [e.value for e in obs.elements[:3]] == ["Ada", "Lovelace", "ada@example.test"]
    assert agent.policy.choose.await_count == 1  # Save still needs a separate decision.
    assert agent.feedback_model.value.await_count == 3
    assert sum(e["kind"] == "input_sequence_selected" for e in agent.events) == 2
    assert agent.pending["click_target"]["name"] == "Save"
    assert not agent.memory.write_checkpoints and agent.memory.pending_writes


async def test_real_rendered_form_continues_after_native_blur_without_skipping_save():
    from jev_browser.browser import PlaywrightBackend

    definition = Task(id="rendered-sequence", sandbox=True, control_mode="dynamic",
                      objective="Create Ada Lovelace with email ada@example.test.")
    async with PlaywrightBackend(definition) as browser:
        await browser.load_html('''<label>First Name<input name="first"></label>
            <label>Last Name<input name="last"></label><label>Email<input name="email"></label>
            <button>Save</button>''')
        brain = AsyncMock()

        async def plan(task, obs, memory, **kwargs):
            fields = [next(e for e in obs.elements if e.name == name and e.editable)
                      for name in ("First Name", "Last Name", "Email")]
            save = next(e for e in obs.elements if e.name == "Save")
            return Feedback(next_goal="Fill fields then Save", inputs=[
                {"name": name, "value": value} for name, value in
                zip(("First Name", "Last Name", "Email"), ("Ada", "Lovelace", "ada@example.test"), strict=True)],
                stage_controls=[{"element_ref": e.id, "operations": ["fill"]} for e in fields]
                               + [{"element_ref": save.id, "operations": ["click"]}],
                stage_entry={"intent": "act", "operation": "fill", "element_ref": fields[0].id},
                input_sequence=[e.id for e in fields])

        brain.review.side_effect = plan
        brain.value.side_effect = lambda task, obs, memory, action: next(
            p["value"] for p in memory.feedback["inputs"]
            if p["name"] == next(e.name for e in obs.elements if e.id == action.element_ref))
        policy = JsonPolicy(AsyncMock())

        async def choose(task, obs, memory, contract, actions):
            return Decision(choice=next(a.id for a in actions if a.element_ref ==
                next(e.id for e in obs.elements if e.name == "Save")))

        policy.choose = AsyncMock(side_effect=choose)
        agent = DynamicController(definition, browser, policy, feedback=brain, budget=Budget(max_cycles=4))
        await agent.dynamic_loop()
        assert [await browser.page.locator(f'[name="{name}"]').input_value()
                for name in ("first", "last", "email")] == ["Ada", "Lovelace", "ada@example.test"]
        assert policy.choose.await_count == 1
        assert brain.review.await_count == 1
        assert sum(e["kind"] == "input_sequence_selected" for e in agent.events) == 2
        assert agent.pending["click_target"]["name"] == "Save"
        assert not agent.memory.write_checkpoints


@pytest.mark.parametrize("change", ["text", "error", "menu", "unplanned_value", "planned_value",
    "navigation", "tab", "frame", "document", "environment", "generation", "ambiguous", "readonly", "pending",
    "unknown", "stale", "model_confirmation", "review", "consumed", "permission", "challenge"])
async def test_continuation_requires_same_stage_and_exact_current_evidence(change):
    obs = page()
    agent = await controller(obs)
    fresh = await first_input(agent, obs)
    if change == "text":
        fresh.text += " Account already exists"
    elif change == "error":
        fresh.errors.append("page_error: validation")
    elif change == "menu":
        fresh.elements.append(Element(id="opt", role="option", name="Other person"))
    elif change == "unplanned_value":
        fresh.elements.append(Element(id="derived", role="textbox", name="Company", value="Changed", read_only=True))
    elif change == "planned_value":
        fresh.elements[1].value = "Unexpected"
    elif change == "navigation":
        fresh.url = "https://other.test"
    elif change == "tab":
        fresh.tab_id = "other"
    elif change == "frame":
        fresh.frame_id = "other"
    elif change == "document":
        fresh.document_id = "new-document"
    elif change == "environment":
        agent.memory.environment_id = "other"
    elif change == "generation":
        agent.memory.feedback["execution_scope"]["generation"] += 1
    elif change == "ambiguous":
        fresh.elements.append(fresh.elements[1].model_copy(update={"id": "duplicate"}))
    elif change == "readonly":
        fresh.elements[1].read_only = True
    elif change == "pending":
        agent.memory.pending_writes["unknown"] = {}
    elif change in {"unknown", "stale"}:
        agent.last_transition["resolved"] = False
    elif change == "model_confirmation":
        agent.last_transition["basis"] = "jev_outcome_or_brain_review"
    elif change == "review":
        local = Feedback(next_goal="Reassess", last_outcome="confirmed")
        local._local_readback = True
        agent.feedback_model.review.return_value = local
        await agent.review(fresh, phase="action_readback")
    elif change == "consumed":
        from jev_browser.dynamic import action_key
        action = next(a for a in candidates(agent, fresh) if a.element_ref == "last" and a.bound_value is None)
        agent.consumed.add(action_key(action, fresh))
    elif change == "permission":
        agent.task.allowed_operations = [Operation.WAIT]
    elif change == "challenge":
        fresh.challenge = True
    assert agent.input_sequence_decision(fresh, candidates(agent, fresh)) is None
    assert agent.input_sequence is None
    agent.backend.execute.assert_awaited_once()


async def test_unique_fresh_dom_rebind_keeps_value_helper_and_scope_checks():
    agent = await controller()
    fresh = await first_input(agent, page())
    fresh.elements[1].id = "fresh-last"
    actions = candidates(agent, fresh)
    decision = agent.input_sequence_decision(fresh, actions)
    action = next(a for a in actions if a.id == decision.choice)
    assert action.element_ref == "fresh-last" and action.bound_value is None
    agent.feedback_model.value.side_effect = None
    agent.feedback_model.value.return_value = "Wrong"
    agent.feedback_model.repair_value.return_value = "Wrong"
    with pytest.raises(InvalidInputValue):
        await agent.bind_input(action, fresh)
    agent.backend.execute.assert_awaited_once()


@pytest.mark.parametrize("change", ["combobox", "search", "named_search", "grid", "missing_value", "duplicate", "save", "verification"])
async def test_optional_sequence_does_not_authorize_complex_or_unplanned_fields(change):
    agent = await controller()
    obs = page()
    feedback = agent.feedback_model.review.return_value.model_copy(deep=True)
    if change == "combobox":
        obs.elements[1].role = "combobox"
    elif change == "search":
        obs.elements[1].search_query = ""
    elif change == "named_search":
        obs.elements[1].name = "Search..."
        feedback.inputs[1].name = "Search..."
    elif change == "grid":
        obs.elements[1].grid_ref, obs.elements[1].row_ref = "g", "r"
    elif change == "missing_value":
        feedback.inputs = feedback.inputs[:1]
    elif change == "duplicate":
        feedback.input_sequence = ["first", "first"]
    elif change == "save":
        feedback.input_sequence = ["first", "save"]
    elif change == "verification":
        feedback.verification = {"goal": "Check", "fallback_goal": "Journal"}
    agent.input_sequence = None
    agent.memory.feedback["inputs"] = [p.model_dump() for p in feedback.inputs]
    agent.arm_input_sequence(obs, feedback)
    assert agent.input_sequence is None
    agent.backend.execute.assert_not_awaited()
