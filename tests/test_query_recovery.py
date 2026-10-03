"""Unknown query UI may be deferred without weakening durable-write readback."""
from unittest.mock import AsyncMock

import pytest

from jev_browser.context_budget import ContextBudgetExceeded
from jev_browser.dynamic import DynamicController, Feedback, generate_dynamic
from jev_browser.protocol import Element, Observation, Operation, Receipt, Task


def view(elements, text='Vendor list'):
    return Observation(observation_id=text, document_version=text, tab_id='tab',
        url='https://example.test/vendors', title='Vendors', text=text, elements=elements)


def plan():
    return Feedback(next_goal='Verify saved vendor', working_memory='Preserve original remaining work',
        stage_entry={'intent': 'verify', 'operation': 'request_replan'},
        verification={'goal': 'Read vendor row', 'fallback_goal': 'Continue independent journal work'}).model_dump()


def agent():
    c = DynamicController(Task(id='query', sandbox=True, control_mode='dynamic',
        objective='Create vendor then journal', allowed_origins=['https://example.test']),
        AsyncMock(), AsyncMock(), feedback=AsyncMock())
    c.memory.feedback = plan()
    c.backend.execute.return_value = Receipt(action_id='click', status='ok')
    return c


async def click(c, obs, ref):
    a = next(a for a in generate_dynamic(obs, c.task) if a.element_ref == ref and a.operation == Operation.CLICK)
    assert await c.perform(a, obs) is None
    return a


async def query_pending():
    c = agent()
    start = view([Element(id='filter', role='button', name='Filter')])
    opened = view(start.elements + [
        Element(id='selector', role='button', name='Select an item ...'),
        Element(id='op', role='button', name='Contain'),
        Element(id='value', role='textbox', name='Value', editable=True),
        Element(id='add', role='button', name='+ New Conditional')], 'Filter panel')
    options = view(opened.elements + [Element(id='field', role='menuitem', name='Display Name')], 'Field options')
    await click(c, start, 'filter')
    assert c.confirm_transition('confirmed', opened, 'observed panel')
    await click(c, opened, 'selector')
    assert c.pending['query_ui_kind'] == 'field_selector'
    assert c.confirm_transition('confirmed', options, 'observed fields')
    await click(c, options, 'field')
    assert c.pending['query_ui_kind'] == 'field_option'
    # Menu closes but the selector still shows its placeholder: no success proof.
    return c, opened


async def test_query_deferral_keeps_uncertainty_consumption_and_original_memory():
    c, obs = await query_pending()
    key = c.pending['key']
    confirmed = set(c.memory.confirmed_writes)
    assert c.defer_uncertain_query(obs, 'unknown after refresh')
    assert c.pending is None and not c.memory.pending_writes
    assert key in c.consumed and c.memory.confirmed_writes == confirmed
    assert not c.memory.write_checkpoints
    obligation = c.memory.unresolved_verifications[0]
    assert obligation['status'] == 'unresolved'
    assert obligation['unconfirmed_ui_actions'][0]['status'] == 'deferred_unconfirmed'
    assert not obligation['unconfirmed_ui_actions'][0]['action_confirmed']
    assert c.fresh_scope_required and c.verification_exhausted(obs)
    assert c.memory.feedback['working_memory'] == 'Preserve original remaining work'
    assert c.task.objective == 'Create vendor then journal'
    c.backend.execute.assert_awaited()
    assert c.backend.execute.await_count == 3  # No dismissal or replay.


async def test_overflow_unknown_query_requests_fresh_scope_instead_of_stopping():
    c, obs = await query_pending()
    c.pending['unknown_frame_refreshes'] = 1
    c.review = AsyncMock(return_value=Feedback(next_goal='Read field', last_outcome='unknown'))
    result = await c.readback_after_policy_overflow(obs, ContextBudgetExceeded('large', {'after_bytes': 53000}))
    assert result is None
    assert c.fresh_scope_required and c.pending is None
    c.policy.choose.assert_not_awaited()
    assert c.backend.execute.await_count == 3


@pytest.mark.parametrize('alter', ['dialog', 'route', 'other_pending', 'unknown_dispatch', 'no_verification', 'environment'])
async def test_defer_rejects_ambiguous_or_changed_context(alter):
    c, obs = await query_pending()
    if alter == 'dialog':
        obs.dialogs = ['Confirm payment?']
    elif alter == 'route':
        obs.url += '/new'
    elif alter == 'other_pending':
        c.memory.pending_writes['other'] = {'action': 'Save'}
    elif alter == 'unknown_dispatch':
        c.pending['dispatch_status'] = 'unknown'
    elif alter == 'no_verification':
        c.memory.feedback['verification'] = None
    else:
        c.memory.environment_id = 'new-environment'
    assert not c.defer_uncertain_query(obs, 'unknown')
    assert c.pending and not c.memory.unresolved_verifications


@pytest.mark.parametrize('role,name', [('button', 'Save'), ('button', 'Submit'), ('menuitem', 'Display Name'),
                                       ('menuitem', 'Pay'), ('button', 'Select an item ...')])
async def test_verify_intent_or_names_without_observed_lineage_never_release_pending(role, name):
    c = agent()
    obs = view([Element(id='target', role=role, name=name)])
    await click(c, obs, 'target')
    assert not c.defer_uncertain_query(obs, 'unknown')
    assert c.pending and c.memory.pending_writes


async def test_business_form_named_filter_does_not_create_query_provenance():
    c = agent()
    start = view([Element(id='filter', role='button', name='Filter')])
    after = view([Element(id='selector', role='button', name='Select an item ...'),
                  Element(id='value', role='textbox', name='Value', editable=True)], 'Edit form')
    await click(c, start, 'filter')
    assert c.confirm_transition('confirmed', after, 'opened')
    assert not c.query_controls
