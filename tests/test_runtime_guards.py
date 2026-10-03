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


async def test_context_overflow_shrinks_candidate_page_without_losing_state_or_navigation():
    from jev_browser.context_budget import ContextBudgetExceeded
    from jev_browser.dynamic import action_key, generate_dynamic
    from jev_browser.protocol import Decision

    obs = page()
    obs.elements = [Element(id=f'e{i}', role='button', name=f'Record {i}') for i in range(100)]
    policy = AsyncMock()
    seen = []
    controller = DynamicController(definition(), AsyncMock(), policy, feedback=None)
    controller.pending = {'key': 'saved-write', 'dispatch_status': 'ok', 'waits': 2,
                          'action': {'operation': 'click', 'description': 'Save vendor'}}
    controller.memory.pending_writes['saved-write'] = controller.pending.copy()
    snapshot = json.dumps(controller.memory.export(), sort_keys=True)
    async def choose(task, current, memory, contract, candidates):
        assert task is controller.task and current is obs and memory is controller.memory
        seen.append(len(candidates))
        if len(candidates) > 25:
            raise ContextBudgetExceeded('too many candidates', {'after_bytes': 48005})
        return Decision(choice=next(a.id for a in candidates if a.operation == Operation.WAIT))
    policy.choose.side_effect = choose
    full = generate_dynamic(obs, controller.task)
    decision, candidates, limit = await controller.choose_with_context_pages(
        obs, full, limit=250, offset=0)
    assert seen == sorted(seen, reverse=True) and len(seen) > 1
    assert next(a for a in candidates if a.id == decision.choice).operation == Operation.WAIT
    assert any(a.operation == Operation.MORE_CANDIDATES for a in candidates)
    # Every observed action remains reachable through pages, without dispatch.
    reachable = set()
    for offset in range(100):
        reachable.update(action_key(a, obs) for a in generate_dynamic(obs, controller.task, limit=limit, offset=offset))
    assert {action_key(a, obs) for a in full} <= reachable
    assert json.dumps(controller.memory.export(), sort_keys=True) == snapshot
    assert controller.pending['key'] == 'saved-write'
    controller.backend.execute.assert_not_awaited()


@pytest.mark.parametrize('pagination', [True, False])
async def test_context_overflow_stops_when_protected_state_cannot_fit_or_paging_is_disallowed(pagination):
    from jev_browser.context_budget import ContextBudgetExceeded
    from jev_browser.dynamic import generate_dynamic

    task = definition()
    if not pagination:
        task.allowed_operations = [op for op in task.allowed_operations if op != Operation.MORE_CANDIDATES]
    obs = page()
    obs.elements = [Element(id=f'e{i}', role='button', name=f'Record {i}') for i in range(40)]
    policy = AsyncMock()
    policy.choose.side_effect = ContextBudgetExceeded('protected state too large')
    controller = DynamicController(task, AsyncMock(), policy, feedback=None)
    controller.pending = {'key': 'must-retain'}
    with pytest.raises(ContextBudgetExceeded):
        await controller.choose_with_context_pages(obs, generate_dynamic(obs, task), limit=250, offset=0)
    assert policy.choose.await_count <= 6
    if not pagination:
        assert policy.choose.await_count == 1
    assert controller.pending == {'key': 'must-retain'}
    controller.backend.execute.assert_not_awaited()


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


@pytest.mark.parametrize("failure", ["asyncio", "httpx"])
async def test_action_model_timeout_is_not_reported_as_run_deadline(failure):
    def respond(request):
        if failure == "asyncio":
            raise TimeoutError("inference timed out")
        raise httpx.ReadTimeout("inference timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = ModelTransport("https://model.test/v1/chat/completions", "test", "test",
                                   client=client, retries=0)

        class InferenceController(DynamicController):
            async def _loop(self):
                self.pending = {"key": "write", "dispatch_status": "ok", "waits": 0,
                                "action": {"operation": "click", "description": "Save vendor"}}
                self.memory.pending_writes["write"] = self.pending
                await transport.post({"messages": [{"role": "user", "content": json.dumps(
                    state(self.task, page(), self.memory, None))}]}, "llm_policy")

        backend = AsyncMock()
        controller = InferenceController(definition(), backend, None, feedback=None)
        result = await controller.run()
    assert result.status == "needs_attention", result.reason
    assert "llm_policy" in result.reason and "run budget not exhausted" in result.reason
    assert "wall-clock deadline" not in result.reason
    assert controller.pending and controller.memory.pending_writes
    assert len(transport.ledger) == 1 and transport.ledger[0]["error"] in {"TimeoutError", "ReadTimeout"}
    backend.execute.assert_not_awaited()


@pytest.mark.parametrize('kind,allowance', [('dynamic_feedback', 120),
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


async def test_readback_reserves_retry_time_and_retries_identical_inference_only():
    requests, allowances = [], []
    def respond(request):
        requests.append(request.content)
        allowances.append(request.extensions['timeout']['read'])
        if len(requests) == 1:
            raise httpx.ReadTimeout('provider response headers stalled', request=request)
        return httpx.Response(200, json={'choices': []})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = ModelTransport('https://test.example', 'test', 'test', client=client, timeout_s=180)
        await transport.post({'messages': [{'role': 'user', 'content': '{"trusted_goal":"test"}'}]},
                             'dynamic_readback')
    assert len(requests) == 2 and requests[0] == requests[1]
    assert 59 < allowances[0] <= 60 and 60 < allowances[1] <= 120
    assert transport.ledger[0]['remaining_call_seconds'] == allowances[0]
    assert transport.ledger[0]['error'] == 'ReadTimeout' and transport.ledger[1]['status'] == 200


async def test_dialog_dismissal_confirms_ui_only_after_animation_without_a_model_or_second_close():
    from jev_browser.dynamic import generate_dynamic
    from jev_browser.protocol import Receipt

    before = page().model_copy(update={'dialogs': ['Message\nShared with users'],
        'elements': [Element(id='modal-close', role='button', name='close (icon control)')]})
    backend, brain = AsyncMock(), AsyncMock()
    backend.execute.return_value = Receipt(action_id='a', status='ok')
    controller = DynamicController(definition(), backend, None, feedback=brain)
    close = next(a for a in generate_dynamic(before, controller.task) if a.element_ref == 'modal-close')
    await controller.perform(close, before)
    pending = controller.pending
    animating = before.model_copy(update={'observation_id': 'o2', 'dialogs': ['Message']})
    assert not controller.confirm_visible_dialog_close(animating)
    assert not controller.readback_dialog_close(animating, close)
    assert controller.pending is pending
    after = before.model_copy(update={'observation_id': 'o3', 'document_version': 'v3', 'dialogs': [],
        'elements': [Element(id='reports', role='button', name='Reports'),
                     Element(id='background-close', role='button', name='close (icon control)')]})
    assert controller.confirm_visible_dialog_close(after)
    assert controller.pending is None and not controller.memory.pending_writes
    record = controller.memory.confirmed_actions[-1]
    assert record['confirmation_scope'] == 'dialog_closed_ui'
    assert record['business_commit_confirmed'] is False
    assert record['proof']['observation_id'] == 'o3'
    assert controller.ui_review_due and not controller.stage_review_due
    assert not controller.memory.feedback.get('complete')
    brain.review.assert_not_awaited()
    assert backend.execute.await_count == 1


@pytest.mark.parametrize('unsafe', ['loading', 'same_observation', 'dialog_present', 'wrong_url',
    'wrong_tab', 'page_error', 'target_present', 'empty_page', 'business_pending', 'unknown_dispatch'])
async def test_dialog_dismissal_requires_fresh_scoped_evidence_and_never_confirms_a_business_write(unsafe):
    from jev_browser.dynamic import generate_dynamic
    from jev_browser.protocol import Receipt

    before = page().model_copy(update={'dialogs': ['Message'],
        'elements': [Element(id='close', role='button', name='Close')]})
    backend = AsyncMock()
    backend.execute.return_value = Receipt(action_id='a', status='ok')
    controller = DynamicController(definition(), backend, None, feedback=None)
    await controller.perform(next(a for a in generate_dynamic(before, controller.task)
                                  if a.element_ref == 'close'), before)
    after = before.model_copy(update={'observation_id': 'o2', 'dialogs': [],
        'elements': [Element(id='main', role='button', name='Reports')]})
    updates = {'loading': {'loading': True}, 'same_observation': {'observation_id': 'o1'},
        'dialog_present': {'dialogs': ['Message']}, 'wrong_url': {'url': 'about:blank#other'},
        'wrong_tab': {'tab_id': 'other'}, 'page_error': {'errors': ['page_error:render failed']},
        'target_present': {'elements': before.elements}, 'empty_page': {'elements': []}}
    if unsafe == 'business_pending':
        controller.pending['confirmation_scope'] = 'business_commit'
    elif unsafe == 'unknown_dispatch':
        controller.pending['dispatch_status'] = 'unknown'
    else:
        after = after.model_copy(update=updates[unsafe])
    assert not controller.confirm_visible_dialog_close(after)
    assert controller.pending and controller.memory.pending_writes
    assert not controller.memory.confirmed_actions


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
