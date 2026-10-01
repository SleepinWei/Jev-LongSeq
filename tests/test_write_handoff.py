from unittest.mock import AsyncMock

from jev_browser.context_budget import archive_ref, memory_view
from jev_browser.dynamic import DynamicController, Feedback, generate_dynamic, planning_location
from jev_browser.models import state
from jev_browser.observability import ModelCallTimeout
from jev_browser.protocol import Decision, Element, Observation, Receipt, Task


def definition():
    return Task(id='handoff', sandbox=True, control_mode='dynamic',
                objective='Create the requested vendor, then create a journal.')


def form():
    return Observation(observation_id='draft', document_version='v1', tab_id='tab',
        url='about:blank', title='New Vendor', text='New Vendor', elements=[
            Element(id='email', role='textbox', name='Vendor Email', value='vendor@example.test', editable=True),
            Element(id='save', role='button', name='Save'),
            Element(id='new', role='button', name='New Vendor'),
            Element(id='back', role='button', name='Back to list'),
            Element(id='quick', role='button', name='Quick find')])


async def saved_controller():
    backend, brain, policy = AsyncMock(), AsyncMock(), AsyncMock()
    backend.execute.return_value = Receipt(action_id='save', status='ok')
    controller = DynamicController(definition(), backend, policy, feedback=brain)
    controller.memory.feedback = {'next_goal': 'Create the vendor',
        'working_memory': 'Separation already processed',
        'inputs': [{'name': 'Vendor Email', 'value': 'vendor@example.test'}]}
    before = form()
    save = next(a for a in generate_dynamic(before, definition()) if a.element_ref == 'save')
    await controller.perform(save, before)
    after = before.model_copy(update={'observation_id': 'saved', 'document_version': 'v2',
        'text': 'The vendor has been successfully created. Vendors List'})
    assert controller.confirm_transition('confirmed', after, 'fresh_model_readback')
    return controller, after


async def test_write_checkpoint_timeout_blocks_previous_form_but_keeps_navigation():
    controller, obs = await saved_controller()
    original = controller.task.objective
    controller.last_brain_location = planning_location(obs)
    controller.feedback_model.review.side_effect = ModelCallTimeout('slow planning')
    guidance = await controller.review(obs, phase='write_checkpoint')
    assert guidance.inputs == [] and guidance.verification is None
    assert 'do not recreate' in guidance.next_goal
    ledger = controller.memory.write_checkpoints
    assert ledger[0]['fields'][0]['value'] == 'vendor@example.test'
    assert ledger[0]['status'] == 'write_effect_confirmed'
    assert ledger[0]['source']['observation_id'] == 'saved'
    assert ledger[0]['visible_excerpt'].startswith('The vendor has been successfully created.')
    assert controller.task.objective == original
    assert not controller.memory.feedback['complete']
    candidates = generate_dynamic(obs, definition())
    async def choose(task, current, memory, contract, actions):
        assert not any(a.element_ref in {'email', 'save', 'new'} for a in actions)
        assert {'back', 'quick'} <= {a.element_ref for a in actions}
        return Decision(choice=next(a.id for a in actions if a.element_ref == 'quick'))
    controller.policy.choose.side_effect = choose
    await controller.choose_with_context_pages(obs, candidates, limit=250, offset=0)
    assert len(controller.memory.write_checkpoints) == 1
    controller.backend.execute.assert_awaited_once()
    # A direct stale dispatch path is guarded too, before binding or execution.
    bad = next(a for a in candidates if a.element_ref == 'new')
    assert await controller.perform(bad, obs)
    controller.backend.execute.assert_awaited_once()


async def test_fresh_planning_releases_handoff_without_erasing_checkpoint():
    controller, obs = await saved_controller()
    controller.feedback_model.review.return_value = Feedback(next_goal='Locate Journal Entries', inputs=[])
    await controller.review(obs, phase='write_checkpoint')
    assert not controller.memory.feedback.get('planning_handoff')
    assert len(controller.memory.write_checkpoints) == 1
    assert state(controller.task, obs, controller.memory, None)['untrusted_memory']['write_checkpoints']


async def test_new_environment_keeps_checkpoint_historical_and_does_not_block_new_work():
    controller, obs = await saved_controller()
    controller.memory.environment_id = 'new-environment'
    feedback = controller.degraded_planning(obs, 'resume', 'timeout')
    assert 'original task' in feedback.next_goal
    assert controller.memory.write_checkpoints[0]['environment_id'] != controller.memory.environment_id
    candidates = generate_dynamic(obs, definition())
    controller.policy.choose.return_value = Decision(choice=candidates[0].id)
    _, available, _ = await controller.choose_with_context_pages(obs, candidates, limit=250, offset=0)
    assert any(a.element_ref == 'email' for a in available)


async def test_pending_or_dialog_ui_effect_never_creates_write_checkpoint():
    controller, obs = await saved_controller()
    controller.memory.write_checkpoints.clear()
    controller.memory.feedback.pop('planning_handoff')
    before = form()
    action = next(a for a in generate_dynamic(before, definition()) if a.element_ref == 'save')
    controller.consumed.clear()
    await controller.perform(action, before)
    assert not controller.confirm_transition('unknown', obs, 'uncertain')
    assert not controller.memory.write_checkpoints and controller.pending
    controller.pending['confirmation_scope'] = 'dialog_opened'
    assert controller.confirm_transition('confirmed', obs, 'question_visible')
    assert not controller.memory.write_checkpoints


async def test_old_write_snapshot_is_retrievable_after_context_aging():
    controller, _ = await saved_controller()
    checkpoint = controller.memory.write_checkpoints[0]
    controller.memory.write_checkpoints.extend([checkpoint.copy(), checkpoint.copy()])
    raw = controller.memory.context()
    projected = memory_view(raw, level=2)
    archived = projected['write_checkpoints'][0]
    assert archived['archive_ref'] == archive_ref(checkpoint)
    assert archived['status'] == checkpoint['status']
    assert archived['fields']['archived'] is True
    assert raw['write_checkpoints'][0]['fields'][0]['value'] == 'vendor@example.test'
