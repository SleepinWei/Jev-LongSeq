"""One DS plan, several Jev execution groups, fresh readback and write checkpoints."""
from unittest.mock import AsyncMock

import pytest

from jev_browser.dynamic import DynamicController, Feedback, stage_plan_diagnostics
from jev_browser.models import JevPolicy
from jev_browser.protocol import Budget, Element, Observation, Operation, Receipt, Task


def page():
    return Observation(observation_id="o1", document_version="v1", document_id="doc", tab_id="t",
        url="about:blank", title="New vendor", text="New vendor", elements=[
            Element(id="first", role="textbox", name="First Name", editable=True),
            Element(id="last", role="textbox", name="Last Name", editable=True),
            Element(id="email", role="textbox", name="Email", editable=True),
            Element(id="country", role="combobox", name="Country", selectable=True,
                    options=["UK", "US"], value="US"),
            Element(id="save", role="button", name="Save"),
            Element(id="other", role="button", name="Other task", href="about:blank#other")])


def plan(obs=None):
    obs = obs or page()
    refs = {e.name: e.id for e in obs.elements}
    values = {"First Name": "Ada", "Last Name": "Lovelace", "Email": "ada@example.test", "Country": "UK"}
    return Feedback(next_goal="Create vendor, then handle remaining finance and CRM tasks",
        working_memory="Finance and CRM pending", inputs=[{"name": k, "value": v} for k, v in values.items()],
        stage_controls=[{"element_ref": refs[k], "operations": ["select" if k == "Country" else "fill"]}
                        for k in values] + [{"element_ref": refs["Save"], "operations": ["click"]}],
        stage_entry={"intent": "act", "operation": "fill", "element_ref": refs["First Name"]},
        execution_groups=[
            {"goal": "Set identity", "actions": [{"element_ref": refs[k], "operation": "fill"}
                                                  for k in ("First Name", "Last Name")]},
            {"goal": "Set contact details", "actions": [
                {"element_ref": refs["Email"], "operation": "fill"},
                {"element_ref": refs["Country"], "operation": "select"}]},
            {"goal": "Save and independently verify", "actions": [
                {"element_ref": refs["Save"], "operation": "click"}]}])


def task():
    return Task(id="groups", sandbox=True, control_mode="dynamic",
                objective="Create vendor Ada Lovelace, ada@example.test, UK; then finance and CRM.")


async def controller(obs=None, *, review_initial=True, **kwargs):
    brain = AsyncMock()
    brain.review.return_value = plan(obs)
    policy = JevPolicy(AsyncMock(), context_max_bytes=200000)
    policy.transport.model = "jev-ultrafast"
    policy.transport.observer = None
    agent = DynamicController(task(), AsyncMock(), policy, feedback=brain, **kwargs)
    if review_initial:
        await agent.review(obs or page(), phase="initial")
    return agent


def candidates(agent, obs):
    agent.refresh_stage_bindings(obs)
    agent.refresh_execution_groups(obs)
    return agent.generate_stage_candidates(obs, limit=250, offset=0)


async def test_one_ds_plan_runs_three_groups_with_jev_and_no_input_rpcs():
    obs = page()
    agent = await controller(review_initial=False, budget=Budget(max_cycles=5))
    version = 1
    dispatched = []

    async def observe():
        nonlocal version
        version += 1
        fresh = obs.model_copy(deep=True)
        fresh.observation_id, fresh.document_version = f"o{version}", f"v{version}"
        return fresh

    async def execute(action):
        dispatched.append(action.element_ref)
        if action.operation in {Operation.FILL, Operation.SELECT}:
            next(e for e in obs.elements if e.id == action.element_ref).value = action.bound_value
        return Receipt(action_id=action.id, status="ok")

    async def respond(payload, kind):
        assert kind == "jev"
        assert agent.memory.context()["execution_window"]["status"] == "active"
        options = payload["questions"]["action"]["criteria"]
        choice = next(key for key, option in options.items()
                      if option["operation"] in {"fill", "select", "click"} and option.get("value") is None)
        return {"answers": {"action": {"type": "choice", "choice": choice, "confidence": 1}}}

    agent.backend.observe.side_effect = observe
    agent.backend.execute.side_effect = execute
    agent.policy.transport.post.side_effect = respond
    await agent.dynamic_loop()
    assert dispatched == ["first", "last", "email", "country", "save"]
    assert [e.value for e in obs.elements[:4]] == ["Ada", "Lovelace", "ada@example.test", "UK"]
    assert agent.policy.transport.post.await_count == 5  # Jev actually selects; DS does not micromanage.
    assert agent.feedback_model.review.await_count == 1
    agent.feedback_model.value.assert_not_awaited()
    assert sum(e["kind"] == "execution_group_completed" for e in agent.events) == 2
    assert agent.pending["click_target"]["name"] == "Save"
    assert agent.memory.pending_writes and not agent.memory.write_checkpoints


async def test_later_groups_and_navigation_are_inaccessible_until_readback():
    obs = page()
    agent = await controller()
    assert {a.element_ref for a in candidates(agent, obs) if a.operation in
            {Operation.FILL, Operation.SELECT, Operation.CLICK}} == {"first", "last"}
    action = next(a for a in candidates(agent, obs) if a.element_ref == "first" and a.bound_value is None)
    await agent.bind_input(action, obs)
    agent.backend.execute.return_value = Receipt(action_id=action.id, status="ok")
    await agent.perform(action, obs)
    fresh = obs.model_copy(deep=True)
    fresh.observation_id, fresh.document_version = "o2", "v2"
    fresh.elements[0].value = "Ada"
    assert agent.confirm_visible_input(fresh)
    assert {a.element_ref for a in candidates(agent, fresh) if a.operation in
            {Operation.FILL, Operation.SELECT, Operation.CLICK}} == {"last"}
    fresh.elements[1].value = "Lovelace"
    assert {a.element_ref for a in candidates(agent, fresh) if a.operation in
            {Operation.FILL, Operation.SELECT, Operation.CLICK}} == {"email", "country"}
    assert agent.memory.context()["execution_window"]["group"] == 1
    assert "Finance and CRM pending" in agent.memory.context()["working_memory"]


@pytest.mark.parametrize("change", ["route", "tab", "document", "frame", "environment", "generation",
                                    "error", "dialog", "challenge", "ambiguous", "readonly", "prerequisite", "orphan"])
async def test_changed_execution_frame_requires_ds_without_dispatch(change):
    obs = page()
    agent = await controller()
    if change == "route":
        obs.url = "https://other.test/different-route"
    elif change == "tab":
        obs.tab_id = "other"
    elif change == "document":
        obs.document_id = "other"
    elif change == "frame":
        obs.frame_id = "other"
    elif change == "environment":
        agent.memory.environment_id = "other"
    elif change == "generation":
        agent.memory.feedback["execution_scope"]["generation"] += 1
    elif change == "error":
        obs.errors.append("page_error: unexpected")
    elif change == "dialog":
        obs.dialogs = ["Unexpected confirmation"]
    elif change == "challenge":
        obs.challenge = True
    elif change == "ambiguous":
        obs.elements.append(obs.elements[0].model_copy(update={"id": "duplicate"}))
    elif change == "readonly":
        obs.elements[0].read_only = True
    elif change == "prerequisite":
        obs.elements[0].value, obs.elements[1].value = "Ada", "Lovelace"
        agent.refresh_execution_groups(obs)
        obs.elements[0].value = "Someone else"
    elif change == "orphan":
        agent.memory.pending_writes["unknown"] = {}
    agent.refresh_execution_groups(obs)
    assert not agent.execution_groups and agent.fresh_scope_required
    assert all(a.operation not in {Operation.FILL, Operation.SELECT, Operation.CLICK}
               for a in candidates(agent, obs))
    agent.backend.execute.assert_not_awaited()


@pytest.mark.parametrize("change", ["unknown", "timeout", "error", "stale"])
async def test_dispatch_receipt_alone_does_not_advance_groups(change):
    obs = page()
    agent = await controller()
    action = next(a for a in candidates(agent, obs) if a.element_ref == "first" and a.bound_value is None)
    await agent.bind_input(action, obs)
    agent.backend.execute.return_value = Receipt(action_id=action.id, status=change)
    await agent.perform(action, obs)
    fresh = obs.model_copy(deep=True)
    fresh.observation_id, fresh.elements[0].value = "o2", "Ada"
    assert not agent.confirm_visible_input(fresh)
    assert agent.execution_groups["index"] == 0
    assert not any(s["done"] for s in agent.execution_groups["groups"][0]["steps"])
    if change != "stale":
        assert agent.pending and agent.memory.pending_writes


@pytest.mark.parametrize("change", ["unknown_ref", "wrong_operation", "missing_input", "linked_input",
                                    "unobserved_select", "duplicate", "early_save", "verification"])
def test_planner_cannot_create_an_ungrounded_or_unsafe_queue(change):
    obs, feedback = page(), plan()
    if change == "unknown_ref":
        feedback.execution_groups[0].actions[0].element_ref = "invented"
    elif change == "wrong_operation":
        feedback.execution_groups[0].actions[0].operation = "click"
    elif change == "missing_input":
        feedback.inputs = feedback.inputs[1:]
    elif change == "linked_input":
        obs.elements[0].popup_open = False
    elif change == "unobserved_select":
        feedback.inputs[-1].value = "Invented country"
    elif change == "duplicate":
        feedback.execution_groups[1].actions.append(feedback.execution_groups[0].actions[0])
    elif change == "early_save":
        feedback.execution_groups.reverse()
    elif change == "verification":
        from jev_browser.dynamic import VerificationStage
        feedback.verification = VerificationStage(goal="Inspect", fallback_goal="Other work")
    assert stage_plan_diagnostics(feedback, obs, task())


async def test_historical_errors_do_not_disable_a_new_explicit_plan_but_new_errors_do():
    obs = page()
    obs.errors = ["page_error: historical other application"]
    agent = await controller(obs)
    assert agent.execution_groups
    obs.errors.append("page_error: new failure")
    agent.refresh_execution_groups(obs)
    assert not agent.execution_groups and agent.fresh_scope_required


async def test_rendered_form_runs_three_groups_without_a_ds_call_between_them():
    from jev_browser.browser import PlaywrightBackend

    async with PlaywrightBackend(task()) as browser:
        await browser.load_html('''<label>First Name<input name="first"></label>
            <label>Last Name<input name="last"></label><label>Email<input name="email"></label>
            <label>Country<select name="country"><option>US</option><option>UK</option></select></label>
            <button>Save</button>''')
        agent = await controller(review_initial=False, budget=Budget(max_cycles=5))
        agent.backend = browser
        agent.feedback_model.review.side_effect = lambda task, obs, memory, **kwargs: plan(obs)

        async def respond(payload, kind):
            choice = next(key for key, option in payload["questions"]["action"]["criteria"].items()
                          if option["operation"] in {"fill", "select", "click"} and "value" not in option)
            return {"answers": {"action": {"type": "choice", "choice": choice, "confidence": 1}}}

        agent.policy.transport.post.side_effect = respond
        await agent.dynamic_loop()
        assert [await browser.page.locator(f'[name="{name}"]').input_value()
                for name in ("first", "last", "email", "country")] == ["Ada", "Lovelace", "ada@example.test", "UK"]
        assert agent.feedback_model.review.await_count == 1
        agent.feedback_model.value.assert_not_awaited()
        assert agent.policy.transport.post.await_count == 5
        assert agent.pending["click_target"]["name"] == "Save"
        assert not agent.memory.write_checkpoints
