import json
import time

import pytest

from jev_browser.browser import PlaywrightBackend
from jev_browser.dynamic import DynamicController, Feedback
from jev_browser.observability import Observer
from jev_browser.protocol import Budget, Decision, Element, Observation, Operation, Task

FAILURE = "Cannot read properties of undefined (reading 'fields_dict')"


def definition():
    return Task(id='runtime-recovery', objective='Enter employee Ada in the current draft.',
                control_mode='dynamic', sandbox=True)


def frame(required=True):
    return Observation(observation_id='o1', document_version='v1', document_id='document',
                       tab_id='tab', url='about:blank', title='Draft', text='Not Saved',
                       errors=['page_error:' + FAILURE],
                       elements=[Element(id='company', role='status', name='Company',
                                         required=required, read_only=True, enabled=False)])


def test_optional_empty_display_is_not_a_terminal_runtime_failure():
    agent = DynamicController(definition(), None, None, feedback=None)
    obs = frame(required=False)
    assert agent.blocked(obs) is None
    assert agent.runtime_recovery_step(obs) == (None, None)


@pytest.mark.parametrize('boundary', ['actions', 'seconds'])
def test_runtime_recovery_waits_then_allows_one_bounded_plan_without_confirming(boundary):
    agent = DynamicController(definition(), None, None, feedback=None)
    obs = frame()
    assert agent.runtime_recovery_step(obs)[0] == 'wait'
    assert agent.runtime_recovery_step(obs)[0] == 'wait'
    state, recovery = agent.runtime_recovery_step(obs)
    assert state == 'review' and agent.fresh_scope_required
    assert not agent.memory.confirmed_actions and not agent.memory.pending_writes
    # A failed/timeout plan does not consume the recovery attempt.
    assert agent.runtime_recovery_step(obs)[0] == 'review'
    recovery.update(planned=True, action_start=0, started=time.monotonic())
    assert agent.runtime_recovery_step(obs)[0] is None
    if boundary == 'actions':
        agent.effective_actions = 6
    else:
        recovery['started'] -= 61
    assert agent.runtime_recovery_step(obs)[0] == 'stop'
    assert not agent.memory.confirmed_actions and not agent.memory.pending_writes


def test_populated_readonly_field_resolves_symptom_but_not_business_commit():
    agent = DynamicController(definition(), None, None, feedback=None)
    obs = frame()
    agent.runtime_recovery_step(obs)
    obs.elements[0].value = 'TechVista'
    assert agent.runtime_recovery_step(obs)[0] is None
    assert not agent.memory.confirmed_actions
    obs.elements[0].value = ''
    recovery = agent.runtime_recoveries[('tab', 'about:blank', 'document')]
    recovery.update(planned=True, action_start=0, started=time.monotonic(), waits=2)
    assert agent.runtime_recovery_step(obs)[0] == 'stop'


async def throw(page):
    async with page.expect_event('pageerror'):
        await page.evaluate('(message) => {setTimeout(() => {throw new Error(message)}, 0)}', FAILURE)


async def test_real_errors_are_scoped_to_tab_route_and_navigation_epoch_with_archive(tmp_path):
    async with PlaywrightBackend(definition()) as browser:
        browser.observer = Observer(tmp_path)
        await browser.load_html('<p>First page</p>')
        first = browser.page
        await throw(first)
        assert any(FAILURE in error for error in (await browser.observe()).errors)
        second = await browser.context.new_page()
        await second.set_content('<p>Second page</p>')
        browser.page = second
        assert not (await browser.observe()).errors
        await throw(second)  # Same text, different tab; acknowledgement must be scoped.
        browser.page = first
        browser.acknowledge_runtime_recovery(await browser.observe())
        assert not (await browser.observe()).errors
        browser.page = second
        assert any(FAILURE in error for error in (await browser.observe()).errors)
        await second.goto('about:blank#new-form')
        assert not (await browser.observe()).errors
        await throw(second)
        assert any(FAILURE in error for error in (await browser.observe()).errors)
        # Reloading the same URL must not re-activate previous-document errors.
        await second.reload()
        assert not (await browser.observe()).errors
        await throw(second)
        assert any(FAILURE in error for error in (await browser.observe()).errors)
        rows = [json.loads(row) for row in (tmp_path / 'spans.jsonl').read_text().splitlines()]
        errors = [row for row in rows if row['name'] == 'browser.error']
        assert len(errors) == 4 and len(browser.errors) == 4
        assert errors[0]['tab_id'] != errors[1]['tab_id']
        assert errors[2]['url'] == errors[3]['url']
        assert errors[2]['navigation_epoch'] != errors[3]['navigation_epoch']
        assert all(row['stack'] and row['started_at'] for row in errors)


async def test_acknowledgement_does_not_retire_identical_error_arriving_after_observation():
    async with PlaywrightBackend(definition()) as browser:
        await browser.load_html('<p>Draft</p>')
        await throw(browser.page)
        observed = await browser.observe()
        await throw(browser.page)
        browser.acknowledge_runtime_recovery(observed)
        assert any(FAILURE in error for error in (await browser.observe()).errors)
        assert len(browser.errors) == 2 and browser.recovered_errors == {0}


async def test_required_field_can_recover_through_fresh_plan_and_actual_visible_input(tmp_path):
    class Brain:
        phases = []

        async def review(self, task, obs, memory, *, phase, transition, diagnostic=None):
            self.phases.append(phase)
            assert memory.context()['execution_feedback']['runtime_recovery'][
                'required_blank_readonly_fields'] == ['Company']
            employee = next(element for element in obs.elements if element.name == 'Employee')
            return Feedback(next_goal='Fill Employee with Ada to refresh the current derived Company.',
                            stage_controls=[{'element_ref': employee.id, 'operations': ['fill']}],
                            inputs=[{'name': 'Employee', 'value': 'Ada'}])

        async def value(self, task, obs, memory, action):
            return 'Ada'

    class Policy:
        async def choose(self, task, obs, memory, contract, candidates):
            action = next(action for action in candidates if action.operation == Operation.FILL)
            return Decision(choice=action.id)

    async with PlaywrightBackend(definition()) as browser:
        await browser.load_html('''<style>label.reqd::after {content:'*'}</style>
            <label>Employee<input oninput="document.getElementById('company').textContent='TechVista'"></label>
            <div class="frappe-control"><label class="reqd">Company</label>
            <div id="company" class="control-value like-disabled-input" style="height:30px"></div></div>''')
        await throw(browser.page)
        brain = Brain()
        agent = DynamicController(definition(), browser, Policy(), feedback=brain,
                                  budget=Budget(max_cycles=3), output=tmp_path)
        result = await agent.run()
        assert result.status == 'budget_exhausted'  # Test ends before any business commit.
        assert brain.phases == ['ui_checkpoint']
        assert await browser.page.locator('input').input_value() == 'Ada'
        assert next(element for element in (await browser.observe()).elements
                    if element.name == 'Company').value == 'TechVista'
        assert not agent.memory.confirmed_actions  # Draft input receipt is still pending.
