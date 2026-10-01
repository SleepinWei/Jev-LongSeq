import asyncio
import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from jev_browser.dynamic import DynamicController, Feedback, JsonFeedback, planning_location
from jev_browser.memory import Memory
from jev_browser.models import ModelTransport
from jev_browser.observability import Observer, ResourceLimit
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
        with pytest.raises(httpx.ReadTimeout):
            await transport.post(payload, 'dynamic_readback')
        assert len(requests) == 2 and len(transport.ledger) == 2
        transport.observer = Observer()
        transport.observer.deadline = time.monotonic() + 4
        with pytest.raises(ResourceLimit):
            await transport.post(payload, 'dynamic_readback')
        assert len(requests) == 2


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
