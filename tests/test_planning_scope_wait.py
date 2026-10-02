"""A failed UI handoff cannot reuse old form permissions during planner cooldown."""

from unittest.mock import AsyncMock

from jev_browser.dynamic import DynamicController, Feedback, StageControl, generate_dynamic
from jev_browser.observability import ModelCallTimeout
from jev_browser.protocol import Budget, Element, Observation, Operation, Task


def page():
    return Observation(observation_id="fresh", document_version="v2", tab_id="tab",
        url="about:blank", title="Separation", text="Two activity rows; selected User is visible",
        tabs={"other":"about:other"}, elements=[
            Element(id="add", role="button", name="Add row", grid_ref="grid"),
            Element(id="user", role="combobox", name="User", value="selected@example.test",
                    editable=True, grid_ref="grid", row_ref="2")])


async def waiting_controller():
    task = Task(id="scope-wait", sandbox=True, control_mode="dynamic",
                objective="Fill three activities, then save. Preserve the original instructions.")
    backend, brain, policy = AsyncMock(), AsyncMock(), AsyncMock()
    backend.observe.return_value = page()
    brain.review.return_value = Feedback(next_goal="Fill User then Add row",
        working_memory="Exact recovered memory; first row done; second row selected",
        inputs=[{"name":"User", "value":"search label", "grid_ref":"grid", "row_ref":"2"}],
        stage_controls=[StageControl(element_ref="add", operations=["click"]),
                        StageControl(element_ref="user", operations=["fill"])])
    controller = DynamicController(task, backend, policy, feedback=brain,
                                   budget=Budget(max_cycles=3, no_progress_limit=1))
    await controller.review(page(), phase="step")
    brain.review.side_effect = ModelCallTimeout("provider slow")
    await controller.review(page(), phase="ui_checkpoint")
    return controller


async def test_ui_timeout_invalidates_old_inputs_and_mutations_without_losing_memory_or_goal():
    controller = await waiting_controller()
    assert controller.fresh_scope_required
    assert controller.memory.feedback["inputs"] == []
    assert controller.memory.feedback["execution_scope"]["bindings"] == {}
    assert controller.memory.feedback["working_memory"].startswith("Exact recovered memory")
    assert controller.task.objective.endswith("Preserve the original instructions.")
    candidates = controller.generate_stage_candidates(page(), limit=250, offset=0)
    assert {a.operation for a in candidates} == {Operation.WAIT, Operation.REPLAN}
    old = next(a for a in generate_dynamic(page(), controller.task) if a.element_ref == "add")
    assert await controller.perform(old, page()) == "action outside current observed stage scope; fresh planning required"
    controller.backend.execute.assert_not_awaited()


async def test_cooldown_wait_skips_policy_and_browser_dispatch_instead_of_repeating_rows(monkeypatch):
    controller = await waiting_controller()
    sleep = AsyncMock()
    monkeypatch.setattr("jev_browser.dynamic.asyncio.sleep", sleep)
    result = await controller.run()
    assert result.status == "budget_exhausted"  # The test's three cycles; never no-progress recovery.
    assert sleep.await_count == 3
    controller.policy.choose.assert_not_awaited()
    controller.backend.execute.assert_not_awaited()
    assert controller.feedback_model.review.await_count == 2  # Old valid plan plus the timed-out handoff.
    assert controller.actions == 0 and controller.fresh_scope_required


async def test_only_new_valid_stage_releases_wait_and_explicitly_scopes_remaining_work():
    controller = await waiting_controller()
    controller.planning_retry_after.clear()
    controller.feedback_model.review.side_effect = None
    controller.feedback_model.review.return_value = Feedback(next_goal="Fill the third activity",
        stage_controls=[StageControl(element_ref="user", operations=["fill"])])
    await controller.review(page(), phase="ui_checkpoint")
    assert not controller.fresh_scope_required
    candidates = controller.generate_stage_candidates(page(), limit=250, offset=0)
    assert any(a.element_ref == "user" for a in candidates)
    assert not any(a.element_ref == "add" for a in candidates)
    assert controller.memory.feedback["working_memory"].startswith("Exact recovered memory")
