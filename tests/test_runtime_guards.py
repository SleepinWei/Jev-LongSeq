import asyncio
import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from jev_browser.dynamic import DynamicController, Feedback, JsonFeedback, planning_location
from jev_browser.memory import Memory
from jev_browser.models import ModelTransport, state
from jev_browser.observability import ModelCallTimeout, Observer, ResourceLimit
from jev_browser.protocol import Element, Observation, Operation, Task


def definition():
    return Task(id='guards', control_mode='dynamic', sandbox=True,
                objective='Verify the report, then create the requested vendor. Use dates 2026-06-01 to 2026-07-31.')


def page():
    return Observation(observation_id='o1', document_version='v1', tab_id='tab',
                       url='about:blank', title='Report', text='Nothing to show',
                       elements=[Element(id='to', role='textbox', name='To Date', editable=True,
                                         value='2026-10-01')])


async def test_outer_deadline_retains_unknown_write_and_confirmations(tmp_path):
    class Interrupted(DynamicController):
        async def _loop(self):
            self.memory.pending_writes['write'] = {'dispatch_status': 'unknown', 'waits': 0,
                                                  'action': {'operation': 'click', 'description': 'Submit'}}
            self.memory.confirmed_actions.append({'environment_id': self.memory.environment_id,
                'target': 'Submit', 'business_commit_confirmed': True, 'source': {'observation_id': 'submitted'}})
            await asyncio.Event().wait()
    controller = Interrupted(definition(), None, None, feedback=None, output=tmp_path)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(.02):
            await controller.run()
    saved = json.loads((tmp_path / 'memory.json').read_text())
    assert saved['pending_writes']['write']['dispatch_status'] == 'unknown'
    assert saved['confirmed_actions_archive'][0]['source']['observation_id'] == 'submitted'
    assert not (tmp_path / 'memory.json.tmp').exists()


def test_verification_deferral_records_absence_without_releasing_a_pending_commit():
    controller = DynamicController(definition(), None, None, feedback=None)
    controller.memory.feedback = {'next_goal': 'Keep retrying empty report', 'verification': {
        'goal': 'Find required report row', 'fallback_goal': 'Create requested vendor'}}
    controller.pending = {'confirmation_scope': 'business_commit'}
    assert not controller.defer_verification(page())
    assert not controller.memory.unresolved_verifications
    controller.pending = None
    assert controller.defer_verification(page())
    assert controller.memory.feedback['next_goal'] == 'Create requested vendor'
    assert controller.memory.unresolved_verifications[0]['status'] == 'unresolved'
    assert controller.memory.unresolved_verifications[0]['visible_excerpt'] == 'Nothing to show'
    assert not controller.memory.feedback.get('complete')


async def test_planned_date_mismatch_repairs_before_dispatch():
    class Brain:
        value = AsyncMock(return_value='2026-10-01')
        repair_value = AsyncMock(return_value='2026-07-31')
    from jev_browser.dynamic import generate_dynamic
    obs = page()
    controller = DynamicController(definition(), AsyncMock(), None, feedback=Brain())
    controller.last_brain_location = planning_location(obs)
    controller.memory.feedback = {'inputs': [{'name': 'To Date', 'value': '2026-07-31'}]}
    action = next(a for a in generate_dynamic(obs, definition())
                  if a.operation == Operation.FILL and a.bound_value is None)
    await controller.bind_input(action, obs)
    assert action.bound_value == '2026-07-31'
    assert Brain.repair_value.call_args.kwargs['diagnostic'] == 'planned_input_mismatch'
    controller.backend.execute.assert_not_awaited()


async def test_selected_input_intent_sent_to_helper():
    from jev_browser.dynamic import generate_dynamic
    memory, obs = Memory(), page()
    memory.dynamic_mode = True
    memory.feedback = {'inputs': [{'name': 'To Date', 'value': '2026-07-31'}]}
    action = next(a for a in generate_dynamic(obs, definition())
                  if a.operation == Operation.FILL and a.bound_value is None)
    def respond(request):
        content = json.loads(json.loads(request.content)['messages'][-1]['content'])
        assert content['planned_input']['value'] == '2026-07-31'
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"value":"2026-07-31"}'}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        value = await JsonFeedback(ModelTransport('https://test.example', 'test', 'test', client=client)).value(
            definition(), obs, memory, action)
    assert value == '2026-07-31'


async def test_dynamic_retry_is_bounded_and_run_reserve_prevents_a_request():
    requests = []
    def respond(request):
        requests.append(request)
        raise httpx.ReadTimeout('test', request=request)
    payload = {'messages': [{'role': 'user', 'content': json.dumps({'trusted_goal': 'test'})}]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = ModelTransport('https://test.example', 'test', 'test', retries=9, client=client)
        with pytest.raises(ModelCallTimeout):
            await transport.post(payload, 'dynamic_readback')
        assert len(requests) == 2 and len(transport.ledger) == 2
        transport.observer = Observer()
        transport.observer.deadline = time.monotonic() + 4
        with pytest.raises(ResourceLimit):
            await transport.post(payload, 'dynamic_readback')
        assert len(requests) == 2


@pytest.mark.parametrize('kind,allowance', [('dynamic_feedback', 120), ('dynamic_readback', 120),
                                          ('dynamic_input', 60)])
async def test_model_request_can_use_whole_logical_allowance(kind, allowance):
    def respond(request):
        assert allowance - 1 < request.extensions['timeout']['read'] <= allowance
        return httpx.Response(200, json={'choices': []})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = ModelTransport('https://test.example', 'test', 'test', client=client, timeout_s=180)
        await transport.post({'messages': [{'role': 'user', 'content': json.dumps(
            state(definition(), page(), Memory(), None))}]},
                             kind)


async def test_model_timeout_is_not_run_budget_exhaustion(tmp_path):
    async def respond(request):
        await asyncio.Event().wait()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = ModelTransport('https://test.example', 'test', 'test', client=client,
                                   retries=0, timeout_s=.01)
        class SlowModel(DynamicController):
            async def _loop(self):
                self.memory.pending_writes['write'] = {'dispatch_status': 'unknown', 'waits': 0,
                    'action': {'operation': 'click', 'description': 'Submit'}}
                await transport.post({'messages': [{'role': 'user', 'content': json.dumps(
                    state(self.task, page(), self.memory, None))}]},
                                     'dynamic_feedback')
        controller = SlowModel(definition(), None, None, feedback=None, output=tmp_path)
        result = await controller.run()
    assert result.status == 'needs_attention'
    assert 'dynamic_feedback' in result.reason and 'timed out' in result.reason
    assert result.elapsed_s < controller.budget.max_seconds
    saved = json.loads((tmp_path / 'memory.json').read_text())
    assert saved['pending_writes']['write']['dispatch_status'] == 'unknown'


async def test_local_readback_preserves_input_and_verification_contracts():
    transport = AsyncMock()
    transport.model = 'test'
    obs, memory = page(), Memory()
    memory.feedback = Feedback(next_goal='Verify report', inputs=[{'name': 'To Date', 'value': '2026-07-31'}],
        verification={'goal': 'Verify report', 'fallback_goal': 'Create vendor'}).model_dump()
    from jev_browser.dynamic import evidence_text
    from jev_browser.protocol import digest
    ref = 'v' + digest([obs.observation_id, evidence_text(obs).splitlines()[0]])[:16]
    transport.post.return_value = {'choices': [{'message': {'content': json.dumps(
        {'last_outcome': 'confirmed', 'evidence_ids': [ref]})}}]}
    result = await JsonFeedback(transport).readback(definition(), obs, memory, {})
    assert result.inputs[0].value == '2026-07-31'
    assert result.verification.fallback_goal == 'Create vendor'


async def test_optional_planning_timeout_keeps_memory_and_cools_down_without_confirming(tmp_path):
    brain = AsyncMock()
    brain.review.side_effect = ModelCallTimeout('provider slow')
    controller = DynamicController(definition(), None, None, feedback=brain, output=tmp_path)
    controller.task.objective += '\n' + 'Long original benchmark instructions. ' * 200
    controller.memory.feedback = {'next_goal': 'Old page goal', 'working_memory': 'Exact recovered history',
                                 'inputs': [{'name': 'To Date', 'value': '2026-07-31'}]}
    controller.memory.evidence['archive'] = {'source': {'url': 'about:blank', 'quote': 'historical only'}}
    guidance = await controller.review(page(), phase='resume')
    assert 'original task' in guidance.next_goal and len(guidance.next_goal) < 4000
    assert len(controller.task.objective) > 4000
    assert state(controller.task, page(), controller.memory, None)['trusted_goal'] == controller.task.objective
    assert guidance.working_memory == 'Exact recovered history'
    assert not guidance.inputs and not guidance.complete and guidance.last_outcome != 'confirmed'
    assert len(controller.memory.evidence) == 1 and not controller.memory.confirmed_writes
    await controller.review(page(), phase='draft_row_added')
    assert brain.review.await_count == 1  # No request in the cooldown window.
    saved = json.loads((tmp_path / 'memory.json').read_text())
    assert saved['working_memory'] == 'Exact recovered history'


def test_verification_allowance_starts_after_planned_inputs_match():
    controller = DynamicController(definition(), None, None, feedback=None)
    controller.memory.feedback = {'inputs': [{'name': 'To Date', 'value': '2026-07-31'}],
        'verification': {'goal': 'Find required report row', 'fallback_goal': 'Create requested vendor'}}
    obs = page()
    controller.arm_verification(obs)
    assert not controller.verification_runs
    assert not controller.defer_verification(obs)
    obs.elements[0].value = '2026-07-31'
    controller.actions = 5
    controller.arm_verification(obs)
    assert controller.verification_runs[planning_location(obs)][0] == 5
    assert controller.defer_verification(obs)


async def test_confirmed_write_retires_old_verification_without_completing_other_goals():
    from jev_browser.dynamic import generate_dynamic
    from jev_browser.protocol import Receipt
    before = page()
    before.elements = [Element(id='save', role='button', name='S a ve')]
    backend = AsyncMock()
    backend.execute.return_value = Receipt(action_id='a', status='ok')
    controller = DynamicController(definition(), backend, None, feedback=None)
    controller.memory.feedback = {'verification': {'goal': 'Read back this draft',
                                                   'fallback_goal': 'Keep checking the draft'}}
    route = planning_location(before)
    controller.verification_runs[route] = (0, time.monotonic() - 200)
    controller.exhausted_verifications.add(route)
    action = next(a for a in generate_dynamic(before, definition()) if a.operation == Operation.CLICK)
    await controller.perform(action, before)
    after = before.model_copy(update={'text': 'Saved HR-001', 'observation_id': 'o2'})
    assert controller.confirm_transition('confirmed', after, 'model_readback')
    assert controller.stage_review_due and controller.memory.feedback['verification'] is None
    assert route not in controller.verification_runs and route not in controller.exhausted_verifications
    assert not controller.defer_verification(after) and not controller.memory.unresolved_verifications
    assert not controller.memory.feedback.get('complete')
    # A styled Save label remains a boundary even after ordinary UI activity.
    controller.memory.confirmed_actions += [{**controller.memory.confirmed_actions[0],
                                            'target': f'UI action {i}'} for i in range(6)]
    assert controller.memory.current_readbacks()['actions'][0]['target'] == 'S a ve'


@pytest.mark.parametrize('phase,pending,orphan', [
    ('finish', False, False), ('action_readback', False, False),
    ('stage_budget', True, False), ('step', False, True),
])
async def test_required_review_or_pending_write_never_degrades_on_timeout(phase, pending, orphan):
    brain = AsyncMock()
    brain.review.side_effect = ModelCallTimeout('provider slow')
    controller = DynamicController(definition(), None, None, feedback=brain)
    if pending:
        controller.pending = {'key': 'pending', 'action': {'operation': 'click'}}
    if orphan:
        controller.memory.pending_writes['pending'] = {'dispatch_status': 'unknown'}
    with pytest.raises(ModelCallTimeout):
        await controller.review(page(), phase=phase)
    assert not controller.planning_retry_after and not controller.memory.confirmed_writes
