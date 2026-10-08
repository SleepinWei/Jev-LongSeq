from unittest.mock import AsyncMock

import pytest

from jev_browser.dynamic import DynamicController, Feedback, StageControl, action_key, generate_dynamic
from jev_browser.protocol import Budget, Decision, Element, Observation, Receipt, Task


ERROR = "Missing Values Required\nFollowing fields have missing values:\n\nCompany"


def definition():
    return Task(id="form-repair", control_mode="dynamic", sandbox=True,
                objective='Create the requested record at company "Example Ltd".')


def form():
    return Observation(observation_id="before", document_version="v1", document_id="doc1",
        tab_id="tab", url="about:blank", title="New Record", text="New Record Company Save",
        dialogs=["New Record"], elements=[
            Element(id="company", role="textbox", name="Company", editable=True),
            Element(id="save", role="button", name="Save")])


def rejection():
    return form().model_copy(update={"observation_id": "error", "document_version": "v2",
        "text": ERROR, "dialogs": [ERROR],
        "elements": [Element(id="close", role="button", name="Close")]})


async def saved_attempt(status="ok", before=None):
    before = before or form()
    backend, brain, policy = AsyncMock(), AsyncMock(), AsyncMock()
    backend.execute.side_effect = lambda action: Receipt(action_id=action.id, status=status)
    agent = DynamicController(definition(), backend, policy, feedback=brain)
    agent.memory.feedback = {"next_goal": "Fill date, then Save", "working_memory": "Keep remaining work"}
    save = next(a for a in generate_dynamic(before, agent.task) if a.element_ref == "save")
    await agent.perform(save, before)
    return agent, save


async def test_refusal_archived_without_confirming_or_replaying_write():
    agent, save = await saved_attempt()
    original_key = agent.pending["key"]
    assert agent.reject_visible_form_validation(rejection())
    assert agent.pending is None and not agent.memory.pending_writes
    assert not agent.memory.confirmed_writes and not agent.memory.write_checkpoints
    assert not agent.memory.confirmed_actions
    assert agent.last_transition["outcome"] == "rejected"
    assert agent.last_transition["business_commit_confirmed"] is False
    assert original_key in agent.consumed
    assert original_key == action_key(save, form())
    assert agent.memory.feedback["working_memory"] == "Keep remaining work"
    node = agent.memory.context()["key_nodes"][0]
    assert node["missing_fields"] == ["Company"]
    assert node["source"]["quote"] == ERROR
    assert node["source"]["observation_id"] == "error"
    assert agent.fresh_scope_required
    assert not agent.reject_visible_form_validation(rejection())
    agent.backend.execute.assert_awaited_once()


@pytest.mark.parametrize("case", ["unknown", "timeout", "same_observation", "new_document",
    "new_url", "new_tab", "loading", "two_dialogs", "no_dialog", "prior_error", "not_blank",
    "wrong_field", "duplicate_field", "duplicate_error", "submit", "post_confirm",
    "other_pending", "confirmation", "editable_dialog", "generic_error"])
async def test_unproven_errors_retain_pending_and_consumed_save(case):
    before = form()
    if case == "prior_error":
        before.dialogs = [ERROR]
    if case == "not_blank":
        before.elements[0].value = "Example Ltd"
    if case == "duplicate_field":
        before.elements.insert(0, before.elements[0].model_copy(update={"id": "duplicate"}))
    if case == "submit":
        before.elements[-1].name = "Submit"
    agent, _ = await saved_attempt(before=before)
    pending = agent.pending
    current = rejection()
    if case in {"unknown", "timeout"}:
        pending["dispatch_status"] = case
    elif case == "same_observation":
        current.observation_id = "before"
    elif case == "new_document":
        current.document_id = "doc2"
    elif case == "new_url":
        current.url = "about:elsewhere"
    elif case == "new_tab":
        current.tab_id = "other"
    elif case == "loading":
        current.loading = True
    elif case == "two_dialogs":
        current.dialogs.append("Another error")
    elif case == "no_dialog":
        current.dialogs = []
    elif case == "wrong_field":
        current.dialogs = [ERROR.replace("Company", "Unrelated Field")]
    elif case == "duplicate_error":
        current.dialogs = [ERROR + "\nCompany"]
    elif case == "post_confirm":
        pending["confirmation_scope"] = "business_commit"
    elif case == "other_pending":
        agent.memory.pending_writes["other"] = pending.copy()
    elif case == "confirmation":
        current.elements.append(Element(id="yes", role="button", name="Yes"))
    elif case == "editable_dialog":
        current.elements.append(Element(id="field", role="textbox", name="Company", editable=True))
    elif case == "generic_error":
        current.dialogs = ["Server error: Company required"]
    assert not agent.reject_visible_form_validation(current)
    assert agent.pending is pending and pending["key"] in agent.memory.pending_writes
    assert not agent.memory.confirmed_writes and not agent.memory.key_nodes
    assert not agent.fresh_scope_required


async def test_loop_replans_from_visible_error_before_policy_can_close_and_confirm_save():
    agent, _ = await saved_attempt()
    agent.budget = Budget(max_cycles=1)
    agent.backend.observe.return_value = rejection()
    agent.feedback_model.review.return_value = Feedback(next_goal="Close the validation message",
        stage_controls=[StageControl(element_ref="close", operations=["click"])])
    agent.policy.choose.side_effect = lambda task, obs, memory, contract, candidates: Decision(
        choice=next(a.id for a in candidates if a.element_ref == "close"), outcome="confirmed")
    await agent.dynamic_loop()
    assert agent.feedback_model.review.await_args.kwargs["phase"] == "ui_checkpoint"
    assert any(e["kind"] == "form_validation_rejected" for e in agent.events)
    assert agent.memory.context()["key_nodes"][0]["verification"] == "form_validation_rejected"
    assert agent.pending["click_target"]["name"] == "Close"
    assert agent.pending["confirmation_scope"] == "dialog_closed_ui"
    assert not agent.memory.confirmed_writes
    assert [c.args[0].element_ref for c in agent.backend.execute.await_args_list] == ["save", "close"]
    assert not any(e["operation"] == "wait" for e in agent.memory.events)


async def test_fresh_repair_scope_is_required_and_corrected_save_has_new_key():
    agent, save = await saved_attempt()
    original_key = agent.pending["key"]
    agent.reject_visible_form_validation(rejection())
    assert not agent.stage_action_allowed(save, form())
    assert "scope" in await agent.perform(save, form())
    agent.backend.execute.assert_awaited_once()
    repaired = form().model_copy(deep=True)
    repaired.observation_id = "repaired"
    repaired.elements[0].value = "Example Ltd"
    agent.feedback_model.review.return_value = Feedback(next_goal="Save the corrected form",
        stage_controls=[StageControl(element_ref="save", operations=["click"])])
    await agent.review(repaired, phase="ui_checkpoint")
    fresh_save = next(a for a in generate_dynamic(repaired, agent.task) if a.element_ref == "save")
    assert agent.stage_action_allowed(fresh_save, repaired)
    assert action_key(fresh_save, repaired) != original_key
    await agent.perform(fresh_save, repaired)
    assert agent.pending and agent.pending["key"] != original_key
    assert not agent.memory.confirmed_writes  # New Save still requires genuine readback.
    assert len(agent.backend.execute.await_args_list) == 2
