from unittest.mock import AsyncMock

import pytest

from jev_browser.browser import PlaywrightBackend
from jev_browser.dynamic import DynamicController, Feedback, action_key, generate_dynamic
from jev_browser.protocol import Budget, Decision, Element, Observation, Operation, Receipt, Task


def definition():
    return Task(id='search-readback', objective='Find the rumor', control_mode='dynamic',
                start_url='https://example.test/', allowed_origins=['https://example.test'])


def observation(text='Old page', query='OpenAI Aeron', index='button'):
    return Observation(observation_id='obs', document_version='v', tab_id='tab-0',
        url='https://example.test/search?q=old', title='Search', text=text, elements=[
            Element(id='input', role='textbox', name='Query', editable=True, value=query),
            Element(id=index, role='button', name='search (icon control)',
                    search_query=query, search_scope='query-input')])


def click(obs):
    return next(a for a in generate_dynamic(obs, definition()) if a.operation == Operation.CLICK)


def test_search_key_ignores_feed_changes_but_not_query_tab_or_scope():
    before = observation()
    key = action_key(click(before), before)
    after = observation(text='New results and notifications', index='replaced-button')
    after.url = 'https://example.test/results?q=OpenAI%20Aeron'
    assert action_key(click(after), after) == key
    assert not any(a.operation == Operation.CLICK
                   for a in generate_dynamic(after, definition(), consumed={key}))
    for changed in [observation(query='Aeron'), after.model_copy(update={'tab_id': 'tab-1'})]:
        assert action_key(click(changed), changed) != key
    after.elements[-1].search_scope = 'other-search-widget'
    assert action_key(click(after), after) != key
    assert not any(a.operation == Operation.CLICK
                   for a in generate_dynamic(observation(query=''), definition()))


@pytest.mark.parametrize('status', ['stale', 'unknown', 'ok'])
async def test_search_replay_only_allowed_after_undispatched_stale(status):
    obs = observation()
    backend = AsyncMock()
    backend.execute.return_value = Receipt(action_id='a', status=status)
    agent = DynamicController(definition(), backend, None, feedback=None)
    agent.memory.feedback['next_goal'] = 'Find the rumor and finish research'
    await agent.perform(click(obs), obs)
    newer = observation(text='Feed changed')
    offered = any(a.operation == Operation.CLICK
                  for a in generate_dynamic(newer, definition(), consumed=agent.consumed))
    assert offered == (status == 'stale')
    if status != 'stale':
        assert agent.pending['search_query'] == 'OpenAI Aeron'
        assert 'Find the rumor' not in agent.pending['expected_goal']


@pytest.mark.parametrize('mode', ['results', 'no_results', 'pending', 'unknown', 'unchanged'])
async def test_pending_search_gets_one_bounded_review_without_resubmission(mode):
    class Backend:
        clicked = False
        operations = []

        async def observe(self):
            text = 'Old page'
            if self.clicked and mode != 'unchanged':
                text = ('No results for OpenAI Aeron' if mode == 'no_results' else
                        'Loading' if mode in {'pending', 'unknown'} else
                        'Results for OpenAI Aeron: unrelated news')
            return observation(text=text)

        async def execute(self, action):
            self.operations.append(action.operation)
            if action.operation == Operation.CLICK:
                assert not self.clicked
                self.clicked = True
            return Receipt(action_id=action.id, status='ok')

    class Policy:
        async def choose(self, task, obs, memory, contract, candidates):
            op = Operation.CLICK if not backend.clicked else (
                Operation.WAIT if memory.pending_writes else Operation.FINISH)
            return Decision(choice=next(a.id for a in candidates if a.operation == op),
                            outcome='pending' if memory.pending_writes else 'none')

    class Brain:
        phases = []

        async def review(self, task, obs, memory, *, phase, transition, **kwargs):
            self.phases.append(phase)
            outcome = 'none'
            if phase == 'search_readback':
                assert transition['search_query'] == 'OpenAI Aeron'
                outcome = ('pending' if mode == 'pending' else
                           'unknown' if mode == 'unknown' else 'confirmed')
            return Feedback(next_goal='Inspect sources; search execution is not task completion',
                            last_outcome=outcome, complete=False)

    backend, brain = Backend(), Brain()
    agent = DynamicController(definition(), backend, Policy(), feedback=brain,
                              budget=Budget(max_cycles=8, readback_waits=5))
    result = await agent.run()
    assert brain.phases.count('search_readback') == 1
    assert backend.operations.count(Operation.CLICK) == 1
    assert result.status != 'success'
    if mode in {'results', 'no_results'}:
        assert not agent.pending
        assert any(e.get('basis') == 'search_readback_review' for e in agent.events)
    else:
        assert agent.pending and result.status == 'needs_attention'
        assert len(backend.operations) <= 5


async def test_dom_binds_only_unambiguous_search_and_does_not_name_links_from_url():
    task = Task(id='dom', objective='Inspect', control_mode='dynamic', sandbox=True)
    async with PlaywrightBackend(task) as browser:
        await browser.load_html('''<section><textarea id="query">OpenAI Aeron</textarea>
          <div style="cursor:pointer" class="search-icon"><img alt="搜索" width=24 height=24></div>
          </section><section><input><input><button>搜索</button></section>
          <a href="https://example.test/search?from=search" style="display:block;width:24px;height:24px"></a>
          <section><input type=password value=secret><button>搜索</button></section>''')
        obs = await browser.observe()
        bound = [e for e in obs.elements if e.search_query is not None]
        assert len(bound) == 1
        assert bound[0].search_query == 'OpenAI Aeron' and bound[0].search_scope == 'query'
        assert next(e for e in obs.elements if e.role == 'link').name == ''
        before = action_key(click(obs), obs)
        await browser.page.locator('textarea').fill('Aeron')
        fresh = await browser.observe()
        assert action_key(click(fresh), fresh) != before
