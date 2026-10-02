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


@pytest.mark.parametrize('case', ['new_input', 'changed_handle', 'existing_value', 'disabled', 'readonly', 'unknown'])
async def test_confirmed_click_replans_for_new_inputs_without_authorizing_them(case):
    before = form()
    before.elements = [Element(id='quick', role='button', name='Quick find')]
    if case in {'changed_handle', 'existing_value'}:
        before.elements.append(Element(id='old-search', role='textbox', name='Search...', editable=True))
    backend = AsyncMock()
    backend.execute.return_value = Receipt(action_id='quick', status='unknown' if case == 'unknown' else 'ok')
    controller = DynamicController(definition(), backend, None, feedback=None)
    action = next(a for a in generate_dynamic(before, definition()) if a.element_ref == 'quick')
    await controller.perform(action, before)
    after = before.model_copy(deep=True, update={'observation_id': 'fresh', 'text': 'Vendors To select ESC To close'})
    after.elements = [before.elements[0], Element(id='search', role='textbox', name='Search...', editable=True,
        value='Journal Entry' if case == 'existing_value' else '',
        enabled=case != 'disabled', read_only=case == 'readonly')]
    confirmed = controller.confirm_transition('confirmed', after, 'scoped_model_readback')
    assert confirmed is (case != 'unknown')
    assert controller.ui_review_due is (case == 'new_input')
    assert not controller.memory.write_checkpoints
    assert controller.memory.feedback.get('stage_controls') is None
    backend.execute.assert_awaited_once()


async def test_search_palette_handoff_discards_old_next_click_and_replans_scope():
    from jev_browser.dynamic import Feedback, StageControl
    from jev_browser.protocol import Decision, Observation, Task
    task = Task(id='palette', control_mode='dynamic', sandbox=True,
                objective='Open search and enter "Journal Entry".')
    phases, calls = [], []

    class Backend:
        count = 0

        async def observe(self):
            self.count += 1
            opened = bool(calls)
            value = 'Journal Entry' if 'search' in calls else ''
            return Observation(observation_id=f'o{self.count}', document_version='v', tab_id='tab',
                url='about:blank', title='Vendors', text=f'Search {value}' if opened else 'Vendors',
                elements=[Element(id='quick', role='button', name='Quick find')]
                    + ([Element(id='search', role='textbox', name='Search...', editable=True, value=value)]
                       if opened else []))

        async def execute(self, action):
            calls.append(action.element_ref)
            return Receipt(action_id=action.id, status='ok')

    class Brain:
        async def review(self, task, obs, memory, *, phase, **kwargs):
            phases.append(phase)
            complete = phase == 'finish'
            return Feedback(next_goal='Open search' if phase == 'initial' else 'Enter Journal Entry',
                stage_controls=[StageControl(element_ref='quick', operations=['click'])] if phase == 'initial'
                    else [StageControl(element_ref='search', operations=['fill'])],
                complete=complete, answer='Search input populated' if complete else '',
                notes=[{'quote': obs.text}])

    class Policy:
        async def choose(self, task, obs, memory, contract, candidates):
            if not calls:
                target, outcome = 'quick', None
            elif calls == ['quick'] and phases[-1] == 'initial':
                # The action head proposes the opener again while confirming it.
                target, outcome = 'quick', 'confirmed'
            elif calls == ['quick']:
                assert phases[-1] == 'ui_checkpoint'
                assert not any(a.element_ref == 'quick' and a.operation == 'fill' for a in candidates)
                target, outcome = 'search', None
            else:
                return Decision(choice=next(a.id for a in candidates if a.operation == 'request_finish'))
            return Decision(choice=next(a.id for a in candidates if a.element_ref == target), outcome=outcome)

    result = await DynamicController(task, Backend(), Policy(), feedback=Brain()).run()
    assert result.status == 'success'
    assert calls == ['quick', 'search'] and phases == ['initial', 'ui_checkpoint', 'finish']


async def test_row_record_links_require_stage_scope_but_global_navigation_remains_available():
    from jev_browser.dynamic import Feedback, StageControl
    task = definition().model_copy(update={'allowed_origins': ['https://example.test']})
    obs = form()
    obs.elements += [Element(id='record-link', role='link', name='Open Link',
                            href='https://example.test/users/Rajesh', grid_ref='activities', row_ref='1'),
                     Element(id='home', role='link', name='Home', href='https://example.test/')]
    brain = AsyncMock()
    brain.review.return_value = Feedback(next_goal='Complete the current row',
        stage_controls=[StageControl(element_ref='email', operations=['fill'])])
    backend = AsyncMock()
    controller = DynamicController(task, backend, None, feedback=brain)
    await controller.review(obs, phase='step')
    actions = generate_dynamic(obs, task)
    row_link = next(a for a in actions if a.element_ref == 'record-link')
    assert not controller.stage_action_allowed(row_link, obs)
    assert await controller.perform(row_link, obs)
    backend.execute.assert_not_awaited()
    available = controller.generate_stage_candidates(obs, limit=250, offset=0)
    assert not any(a.element_ref == 'record-link' for a in available)
    assert any(a.element_ref == 'home' for a in available)
    # A task may explicitly need that record; fresh planning can authorize it.
    brain.review.return_value = Feedback(next_goal='Inspect this linked user record',
        stage_controls=[StageControl(element_ref='record-link', operations=['click'])])
    await controller.review(obs, phase='step')
    assert controller.stage_action_allowed(row_link, obs)
