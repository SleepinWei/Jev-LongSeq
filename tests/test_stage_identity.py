from unittest.mock import AsyncMock

import pytest
from test_stage_scope import planned_controller
from test_write_handoff import definition, form

from jev_browser.dynamic import DynamicController, StageControl, generate_dynamic
from jev_browser.protocol import Element, Operation, Receipt


async def test_same_stage_unique_field_rebinds_after_dom_index_changes_without_new_brain_call():
    controller = await planned_controller([StageControl(element_ref='email', operations=['fill'])])
    obs = form()
    obs.elements[0].id = 'new-email-handle'
    controller.refresh_stage_bindings(obs)
    action = next(a for a in generate_dynamic(obs, definition()) if a.element_ref == 'new-email-handle')
    assert controller.stage_action_allowed(action, obs)
    assert controller.feedback_model.review.await_count == 1
    assert 'binding_origins' not in controller.memory.context()['execution_scope']
    assert controller.memory.feedback['execution_scope']['binding_origins']['email']['name'] == 'Vendor Email'
    assert any(e['kind'] == 'stage_control_rebound' for e in controller.events)
    controller.backend.execute.assert_not_awaited()


@pytest.mark.parametrize('case', ['ambiguous', 'another_row', 'new_route', 'new_environment', 'dialog'])
async def test_changed_handle_never_transfers_scope_to_ambiguous_or_other_stage_field(case):
    controller = await planned_controller([StageControl(element_ref='email', operations=['fill'])])
    obs = form()
    obs.elements[0].id = 'replacement'
    if case == 'ambiguous':
        obs.elements.append(obs.elements[0].model_copy(update={'id': 'duplicate'}))
    elif case == 'another_row':
        obs.elements[0].grid_ref, obs.elements[0].row_ref = 'grid', '2'
    elif case == 'new_route':
        obs.url = 'https://example.test/new'
    elif case == 'new_environment':
        controller.memory.environment_id = 'new-env'
    elif case == 'dialog':
        obs.dialogs = ['Another form']
    controller.refresh_stage_bindings(obs)
    action = next(a for a in generate_dynamic(obs, definition()) if a.element_ref == 'replacement')
    assert not controller.stage_action_allowed(action, obs)


@pytest.mark.parametrize('case', ['valid', 'changed_id', 'other_row', 'other_grid', 'ambiguous', 'unknown', 'dialog'])
async def test_grid_input_readback_uses_scoped_cell_identity_when_neighbor_popup_changes_context(case):
    before = form()
    before.elements = [Element(id='activity', role='textbox', name='Activity Name', editable=True,
                               grid_ref='activities', row_ref='2', context='2 Begin typing for results.')]
    backend = AsyncMock()
    backend.execute.return_value = Receipt(action_id='fill', status='unknown' if case == 'unknown' else 'ok')
    controller = DynamicController(definition(), backend, None, feedback=None)
    action = next(a for a in generate_dynamic(before, definition()) if a.operation == Operation.FILL)
    action.bound_value = 'Revoke system access'
    await controller.perform(action, before)
    after = before.model_copy(deep=True)
    after.observation_id = 'fresh'
    field = after.elements[0]
    field.value, field.context = 'Revoke system access', '2 7 results found'
    if case == 'changed_id':
        field.id = 'new-activity-handle'
    elif case == 'other_row':
        field.row_ref = '1'
    elif case == 'other_grid':
        field.grid_ref = 'other-grid'
    elif case == 'ambiguous':
        after.elements.append(field.model_copy(update={'id': 'duplicate'}))
    elif case == 'dialog':
        after.dialogs = ['Different active form']
    assert controller.confirm_visible_input(after) is (case in {'valid', 'changed_id'})
    if case in {'valid', 'changed_id'}:
        assert controller.pending is None
        assert controller.memory.confirmed_actions[-1]['business_commit_confirmed'] is False
        assert not controller.memory.write_checkpoints
    else:
        assert controller.pending
    backend.execute.assert_awaited_once()


async def test_menu_select_intent_uses_observed_click_capability_without_new_value_or_target():
    from jev_browser.dynamic import Feedback
    obs = form()
    obs.elements = [Element(id='name-option', role='menuitem', name='Ananya Reddy Ananya Reddy')]
    brain = AsyncMock()
    brain.review.return_value = Feedback(next_goal='Select observed display name',
        stage_controls=[StageControl(element_ref='name-option', operations=['select'])])
    controller = DynamicController(definition(), AsyncMock(), None, feedback=brain)
    await controller.review(obs, phase='step')
    candidates = controller.generate_stage_candidates(obs, limit=250, offset=0)
    choice = next(a for a in candidates if a.element_ref == 'name-option')
    assert choice.operation == Operation.CLICK and choice.bound_value is None
    assert controller.stage_action_allowed(choice, obs)
    assert any(e['kind'] == 'stage_operation_normalized' for e in controller.events)
    controller.backend.execute.assert_not_awaited()


def test_icon_only_menu_entry_is_not_offered_as_a_business_choice():
    obs = form()
    obs.elements = [Element(id='blank-option', role='menuitem', name='menu (icon control)'),
                    Element(id='actual-option', role='menuitem', name='Ananya Reddy')]
    candidates = generate_dynamic(obs, definition())
    assert not any(a.element_ref == 'blank-option' for a in candidates)
    assert any(a.element_ref == 'actual-option' for a in candidates)


async def test_required_readback_can_resolve_pending_write_when_fast_context_cannot_fit():
    import time

    from jev_browser.context_budget import ContextBudgetExceeded
    from jev_browser.dynamic import Feedback
    before = form()
    backend, brain = AsyncMock(), AsyncMock()
    backend.execute.return_value = Receipt(action_id='save', status='ok')
    controller = DynamicController(definition(), backend, None, feedback=brain)
    controller.started = time.monotonic()
    save = next(a for a in generate_dynamic(before, definition()) if a.element_ref == 'save')
    await controller.perform(save, before)
    after = before.model_copy(update={'observation_id': 'fresh', 'text': 'Vendor has been created.'})
    response = Feedback(next_goal='Current stage', last_outcome='confirmed', readback_quote='Vendor has been created.')
    response._local_readback = True
    brain.review.return_value = response
    assert await controller.readback_after_policy_overflow(after, ContextBudgetExceeded('oversize')) is None
    assert controller.pending is None and controller.stage_review_due
    assert controller.memory.write_checkpoints[-1]['source']['observation_id'] == 'fresh'
    backend.execute.assert_awaited_once()  # Only original save, never resubmitted.
    assert brain.review.call_args.kwargs['phase'] == 'action_readback'


async def test_policy_overflow_pending_review_waits_are_bounded_and_do_not_repeat_same_model_call():
    import time

    from jev_browser.context_budget import ContextBudgetExceeded
    from jev_browser.dynamic import Feedback
    from jev_browser.protocol import Budget
    before = form()
    backend, brain = AsyncMock(), AsyncMock()
    backend.execute.return_value = Receipt(action_id='save', status='ok')
    controller = DynamicController(definition(), backend, None, feedback=brain,
                                   budget=Budget(readback_waits=2))
    controller.started = time.monotonic()
    save = next(a for a in generate_dynamic(before, definition()) if a.element_ref == 'save')
    await controller.perform(save, before)
    after = before.model_copy(update={'observation_id': 'fresh'})
    response = Feedback(next_goal='Wait', last_outcome='pending')
    response._local_readback = True
    brain.review.return_value = response
    overflow = ContextBudgetExceeded('oversize')
    assert await controller.readback_after_policy_overflow(after, overflow) is None
    result = await controller.readback_after_policy_overflow(after, overflow)
    assert result.status == 'needs_attention' and controller.pending
    assert brain.review.await_count == 1
    assert not controller.memory.write_checkpoints
    assert backend.execute.await_count == 2  # Original Save plus a WAIT, never a second Save.
