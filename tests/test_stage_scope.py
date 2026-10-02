import time
from unittest.mock import AsyncMock

from test_write_handoff import definition, form

from jev_browser.dynamic import DynamicController, Feedback, StageControl, generate_dynamic
from jev_browser.protocol import Decision, Element, Operation


async def planned_controller(controls):
    brain = AsyncMock()
    brain.review.return_value = Feedback(next_goal='Navigate to journals; vendor is already created',
        stage_controls=controls)
    controller = DynamicController(definition(), AsyncMock(), AsyncMock(), feedback=brain)
    await controller.review(form(), phase='step')
    return controller


async def test_navigation_stage_blocks_wrong_form_fill_and_menu_without_confidence_threshold():
    obs = form()
    obs.elements.append(Element(id='display', role='button', name='Select display name as'))
    controller = await planned_controller([StageControl(element_ref='quick', operations=['click'])])
    actions = generate_dynamic(obs, definition())
    async def choose(task, current, memory, contract, candidates):
        assert not {'email', 'save', 'new', 'display'} & {a.element_ref for a in candidates}
        assert {'quick', 'back'} <= {a.element_ref for a in candidates}
        return Decision(choice=next(a.id for a in candidates if a.element_ref == 'quick'), confidence=.17)
    controller.policy.choose.side_effect = choose
    decision, _, _ = await controller.choose_with_context_pages(obs, actions, limit=250, offset=0)
    assert decision.confidence == .17  # Stage intent, not a blanket confidence cutoff.
    assert controller.budget.confidence_threshold is None
    controller.backend.execute.assert_not_awaited()


async def test_stage_scope_binds_observed_identity_and_operation_not_just_reused_dom_id():
    controller = await planned_controller([StageControl(element_ref='email', operations=['fill'])])
    obs = form()
    action = next(a for a in generate_dynamic(obs, definition()) if a.element_ref == 'email')
    assert controller.stage_action_allowed(action, obs)
    obs.elements[0].name = 'Payment Amount'
    assert not controller.stage_action_allowed(action, obs)
    obs.elements[0].name = 'Vendor Email'
    obs.elements[0].row_ref = 'different-row'
    assert not controller.stage_action_allowed(action, obs)
    obs.elements[0].row_ref = None
    obs.url = 'about:new-form'
    assert not controller.stage_action_allowed(action, obs)
    obs.url = 'about:blank'
    controller.memory.environment_id = 'new-environment'
    assert not controller.stage_action_allowed(action, obs)


async def test_unknown_stage_refs_do_not_authorize_unobserved_accounting_control():
    controller = await planned_controller([StageControl(element_ref='invented-accounting', operations=['click'])])
    obs = form()
    actions = generate_dynamic(obs, definition())
    assert not controller.memory.feedback['execution_scope']['bindings']
    assert not controller.stage_action_allowed(next(a for a in actions if a.element_ref == 'new'), obs)
    assert any(e['kind'] == 'stage_control_discarded' for e in controller.events)
    assert controller.stage_action_allowed(next(a for a in actions if a.operation == Operation.REPLAN), obs)


async def test_local_readback_preserves_stage_and_fresh_new_plan_authorizes_repeated_work():
    controls = [StageControl(element_ref='email', operations=['fill'])]
    controller = await planned_controller(controls)
    original = controller.memory.feedback['execution_scope']
    local = Feedback(next_goal='Continue', last_outcome='pending')
    local._local_readback = True
    controller.feedback_model.review.return_value = local
    await controller.review(form(), phase='action_readback')
    assert controller.memory.feedback['execution_scope'] == original
    # Explicit new requested work can receive a fresh scope; no permanent object blacklist.
    controller.feedback_model.review.return_value = Feedback(next_goal='Create the next requested vendor',
        stage_controls=[StageControl(element_ref='new', operations=['click'])])
    await controller.review(form(), phase='jev_requested')
    action = next(a for a in generate_dynamic(form(), definition()) if a.element_ref == 'new')
    assert controller.stage_action_allowed(action, form())


async def test_pending_uncertain_write_is_preserved_even_if_action_outside_stage():
    controller = await planned_controller([])
    controller.pending = {'key': 'uncertain-write', 'dispatch_status': 'unknown'}
    controller.memory.pending_writes['uncertain-write'] = controller.pending.copy()
    obs = form()
    actions = generate_dynamic(obs, definition())
    controller.policy.choose.return_value = Decision(choice=actions[0].id, outcome='unknown')
    _, retained, _ = await controller.choose_with_context_pages(obs, actions, limit=250, offset=0)
    assert len(retained) == len(actions)
    assert controller.pending['dispatch_status'] == 'unknown'
    assert controller.memory.pending_writes['uncertain-write']['dispatch_status'] == 'unknown'
    controller.backend.execute.assert_not_awaited()


async def test_scoped_link_field_allows_fresh_owned_options_but_not_another_rows_options():
    controller = await planned_controller([StageControl(element_ref='email', operations=['fill'])])
    obs = form()
    obs.elements[0].role = 'combobox'
    obs.elements[0].popup_open = True
    # Refresh the scope after the fresh combobox was observed.
    await controller.review(obs, phase='step')
    obs.elements.append(Element(id='linked', role='option', name='Requested vendor', option_owner='email'))
    action = next(a for a in generate_dynamic(obs, definition()) if a.element_ref == 'linked')
    assert controller.stage_action_allowed(action, obs)
    obs.elements[-1].row_ref = 'another-row'
    assert not controller.stage_action_allowed(action, obs)
    obs.elements[-1].row_ref = None
    obs.elements[0].popup_open = False
    assert not controller.stage_action_allowed(action, obs)


async def test_out_of_stage_proposal_is_discarded_after_pending_ui_readback():
    from jev_browser.protocol import Budget, Receipt
    controller = await planned_controller([StageControl(element_ref='quick', operations=['click'])])
    before = form()
    controller.backend.execute.return_value = Receipt(action_id='quick', status='ok')
    opener = next(a for a in generate_dynamic(before, definition()) if a.element_ref == 'quick')
    await controller.perform(opener, before)
    after = before.model_copy(update={'observation_id': 'fresh', 'document_version': 'v2',
                                     'text': 'Search overlay opened'})
    controller.backend.observe.return_value = after
    async def choose(task, obs, memory, contract, candidates):
        # The outcome head confirms an old UI click while the action head proposes a wrong fill.
        return Decision(choice=next(a.id for a in candidates if a.element_ref == 'email'), outcome='confirmed')
    controller.policy.choose.side_effect = choose
    controller.initial_phase = ''
    controller.budget = Budget(max_cycles=1)
    controller.started = time.monotonic()
    await controller.dynamic_loop()
    assert any(e['kind'] == 'stage_action_rejected' for e in controller.events)
    controller.backend.execute.assert_awaited_once()  # Only original opener; email never dispatched.
    assert controller.pending is None and not controller.memory.write_checkpoints


async def test_stage_filter_precedes_pagination_so_matching_target_is_not_buried():
    obs = form()
    obs.elements = [Element(id=f'irrelevant{i}', role='button', name=f'Other action {i}') for i in range(150)]
    obs.elements.append(Element(id='journal', role='button', name='Journal Entries'))
    controller = await planned_controller([StageControl(element_ref='journal', operations=['click'])])
    await controller.review(obs, phase='step')
    candidates = controller.generate_stage_candidates(obs, limit=12, offset=0)
    assert any(a.element_ref == 'journal' for a in candidates)
    assert not any(a.operation == Operation.MORE_CANDIDATES for a in candidates)
    assert not any(a.element_ref and a.element_ref.startswith('irrelevant') for a in candidates)


async def test_dense_page_candidate_reduction_does_not_hide_fields_on_next_route():
    from jev_browser.context_budget import ContextBudgetExceeded
    from jev_browser.dynamic import planning_location
    from jev_browser.protocol import Budget, Receipt
    dense = form()
    dense.elements = [Element(id=f'e{i}', role='button', name=f'Row {i}') for i in range(100)]
    vendor = form().model_copy(update={'url': 'https://example.test/vendor', 'observation_id': 'vendor',
                                     'tabs': {'previous-app': dense.url}})
    controller = DynamicController(definition(), AsyncMock(), AsyncMock(), feedback=AsyncMock())
    controller.task.allowed_origins = ['https://example.test']
    controller.started = time.monotonic()
    controller.budget = Budget(max_cycles=2, candidate_limit=250)
    controller.initial_phase = ''
    controller.backend.observe.side_effect = [dense, vendor]
    controller.backend.execute.return_value = Receipt(action_id='wait', status='ok')
    seen = []
    async def choose(task, obs, memory, contract, candidates):
        seen.append((obs.url, len(candidates)))
        if obs.url == dense.url and len(candidates) > 12:
            raise ContextBudgetExceeded('dense page')
        if obs.url == vendor.url:
            assert len(candidates) > 12
            assert any(a.element_ref == 'email' and a.operation == Operation.FILL for a in candidates)
        return Decision(choice=next(a.id for a in candidates if a.operation == Operation.WAIT))
    controller.policy.choose.side_effect = choose
    await controller.dynamic_loop()
    assert controller.candidate_page_limits[planning_location(dense)] <= 12
    assert controller.candidate_page_limits[planning_location(vendor)] == 250
    assert seen[-1][0] == vendor.url
