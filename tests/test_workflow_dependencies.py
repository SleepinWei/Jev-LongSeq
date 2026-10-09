import json

import httpx
import pytest

from jev_browser.dynamic import JsonFeedback, workflow_dependencies
from jev_browser.memory import Memory
from jev_browser.models import ModelTransport
from jev_browser.protocol import Element, Observation, Task


def frame():
    return Observation(observation_id='now', document_version='v2', document_id='doc',
                       tab_id='tab', url='https://hr.test/full-form', title='New Record - draft-id',
                       text='New Record Company Not Saved', elements=[
                           Element(id='company', name='Company', role='status', read_only=True),
                           Element(id='date', name='Date', role='textbox', editable=True, required=True),
                           Element(id='optional', name='Optional Link', role='combobox', editable=True)])


def notebook():
    memory = Memory()
    memory.dynamic_mode = True
    memory.key_nodes['validation'] = {
        'verification': 'form_validation_rejected', 'environment_id': memory.environment_id,
        'missing_fields': ['Company'], 'form_scope': {'document_id': 'doc', 'form_title': 'New Record'},
        'source': {'tab_id': 'tab', 'url': 'https://hr.test/source-record', 'observation_id': 'old',
                   'quote': 'Following fields have missing values: Company'}}
    return memory


def test_explicit_refusal_survives_same_document_form_navigation_without_requiring_dom_flag():
    obs, memory = frame(), notebook()
    snapshot = json.dumps(memory.export(), sort_keys=True)
    hints = workflow_dependencies(obs, memory)
    assert hints['prior_refusals'][0]['name'] == 'Company'
    assert hints['prior_refusals'][0]['current_state'] == 'blank'
    assert hints['prior_refusals'][0]['element_ref'] == 'company'
    assert hints['prior_refusals'][0]['source']['observation_id'] == 'old'
    assert hints['visible_required_fields'][0]['name'] == 'Date'
    assert 'Optional Link' not in [r['name'] for r in hints['prior_refusals']]
    assert hints['scope'].endswith('not evidence that it is mandatory')
    assert json.dumps(memory.export(), sort_keys=True) == snapshot
    obs.elements[0].value = 'Observed Company'
    assert workflow_dependencies(obs, memory)['prior_refusals'][0]['current_state'] == 'populated'
    assert not memory.confirmed_actions and not memory.pending_writes


@pytest.mark.parametrize('change', ['environment', 'tab', 'document', 'origin', 'form', 'legacy'])
def test_old_refusal_is_not_applied_to_another_form_or_unreconstructable_scope(change):
    obs, memory = frame(), notebook()
    if change == 'environment':
        memory.key_nodes['validation']['environment_id'] = 'old-environment'
    elif change == 'tab':
        obs.tab_id = 'other-tab'
    elif change == 'document':
        obs.document_id = 'new-document'
    elif change == 'origin':
        obs.url = 'https://finance.test/full-form'
    elif change == 'form':
        obs.title = 'Another Form'
    else:
        memory.key_nodes['validation'].pop('form_scope')
    assert not workflow_dependencies(obs, memory)['prior_refusals']


def test_ambiguous_missing_control_is_exposed_without_binding_a_guessed_target():
    obs, memory = frame(), notebook()
    obs.elements.append(obs.elements[0].model_copy(update={'id': 'other-company'}))
    hint = workflow_dependencies(obs, memory)['prior_refusals'][0]
    assert hint['current_state'] == 'ambiguous' and 'element_ref' not in hint
    obs.elements = []
    hint = workflow_dependencies(obs, memory)['prior_refusals'][0]
    assert hint['current_state'] == 'not_observed' and 'element_ref' not in hint


async def test_actual_projected_planner_payload_contains_scoped_requirements_and_original_task():
    obs, memory = frame(), notebook()
    definition = Task(id='plan', objective='Create the requested record.', control_mode='dynamic',
                      allowed_origins=['https://hr.test'])
    seen = []

    def respond(request):
        payload = json.loads(request.content)
        content = json.loads(payload['messages'][1]['content'])
        seen.append(content)
        assert content['trusted_goal'] == definition.objective
        hint = content['workflow_dependencies']['prior_refusals'][0]
        assert hint['name'] == 'Company' and hint['current_state'] == 'blank'
        assert hint['read_only'] and hint['source']['observation_id'] == 'old'
        assert "even when the user did not name those fields" in payload['messages'][0]['content']
        assert 'guessed defaults' in payload['messages'][0]['content']
        assert 'dependency_reviews' in content['schema']['properties']
        assert 'An unresolved read-only field blocks the write.' in payload['messages'][0]['content']
        assert 'Company' not in payload['messages'][0]['content']  # No benchmark-specific instruction.
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({
            'next_goal': 'Locate an observed source control to repair the required field.',
            'working_memory': 'Keep the required field unresolved; no save confirmed.',
            'stage_entry': {'intent': 'locate', 'operation': 'request_replan'},
            'stage_controls': []})}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        brain = JsonFeedback(ModelTransport('https://model.test', 'test-key', 'test', client=client))
        await brain.review(definition, obs, memory, phase='ui_checkpoint', transition=None)
    assert len(seen) == 1 and not memory.pending_writes and not memory.confirmed_actions
