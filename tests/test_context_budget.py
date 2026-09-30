import copy
import json

import httpx
import pytest

from jev_browser.context_budget import (
    ContextBudgetExceeded,
    aged_text,
    archive_ref,
    history_view,
    project_chat_request,
    project_request,
    wire_bytes,
)
from jev_browser.dynamic import DynamicController, EvidenceNote, Feedback, generate_dynamic
from jev_browser.memory import Memory
from jev_browser.models import JevPolicy, ModelTransport
from jev_browser.protocol import Element, Observation, Task


def sample():
    task = Task(id="form", control_mode="dynamic", objective='Enter "Ada" in row 2.')
    obs = Observation(observation_id="o1", document_version="v1", tab_id="tab0",
                      url="about:blank", title="Form", text="Visible form " * 1200,
                      elements=[Element(id=f"e{i}", role="textbox", name="Employee",
                                        context=f"Row {i}: " + "Form contents " * 200,
                                        value=f"Person {i}", editable=True) for i in range(3)])
    memory = Memory()
    memory.dynamic_mode = True
    memory.feedback = {"working_memory": "\n\n".join(f"Stage {i}\n" + "detail " * 200
                                                    for i in range(50)),
                       "blockers": ["Await unsaved draft readback"]}
    memory.key_nodes["id"] = {"source": {"url": "about:blank", "quote": "Record HR-007"},
                              "verification": "quote_grounded_only"}
    memory.pending_writes["pending"] = {
        "action": {"operation": "fill", "bound_value": "Ada", "description": "Row 2"},
        "before_excerpt": "Row 2: Person 2", "expected_goal": "Row 2 displays Ada", "waits": 0}
    return task, obs, memory


class Capture:
    model = "offline"
    observer = None

    def __init__(self):
        self.requests = []

    async def post(self, payload, kind):
        self.requests.append(copy.deepcopy(payload))
        return {"answers": {"action": {"type": "choice", "choice": "a0", "confidence": 1},
                            "outcome": {"type": "choice", "choice": "pending"}}}


def test_temporal_decay_keeps_recent_details_and_older_mentions():
    blocks = [f"Stage {i}\n" + f"detail-{i} " * 150 for i in range(50)]
    view = aged_text("\n\n".join(blocks))
    assert "Stage 0" not in view
    assert "Stage 20" in view and "detail-20" not in view
    assert view.count("detail-49") > view.count("detail-40") > view.count("detail-20")


def test_history_counts_old_repetitions_and_retains_recent_bound_values():
    events = [{"operation": "click", "description": "button: Add row | huge form text",
               "receipt": {"status": "ok"}} for _ in range(80)]
    events.append({"operation": "fill", "description": "Fill User",
                   "receipt": {"status": "ok"}, "action": {"bound_value": "Ada"}})
    projected = history_view(events)
    assert len(projected["recent"]) == 30
    assert projected["counts"][0]["count"] == 80
    assert projected["recent"][-1]["bound_value"] == "Ada"
    assert "not proof" in projected["meaning"]
    assert projected["omitted_details"] == 51


def test_bound_literal_history_keeps_the_target_identity():
    events = [{"operation": "fill", "description":
               f'Fill with quoted user text "Ada" | textbox: {name} | Large form context',
               "receipt": {"status": "ok"}} for name in ("Employee", "Approver")]
    projected = history_view(events)
    assert len(projected["counts"]) == 2
    assert "Employee" in projected["recent"][0]["description"]
    assert "Approver" in projected["recent"][1]["description"]


async def test_policy_preserves_goal_pins_pending_values_and_original_candidates():
    task, obs, memory = sample()
    candidates = generate_dynamic(obs, task)
    saved = copy.deepcopy(memory.export())
    original_candidates = [a.model_dump() for a in candidates]
    transport = Capture()
    policy = JevPolicy(transport)
    await policy.choose(task, obs, memory, None, candidates)
    payload = transport.requests[0]
    assert wire_bytes(payload) <= 24_000
    assert payload["state"]["trusted_goal"] == task.objective
    projected = payload["state"]["untrusted_memory"]
    assert projected["key_nodes"] == list(memory.key_nodes.values())
    assert projected["blockers"] == memory.feedback["blockers"]
    assert projected["pending_writes"] == memory.context()["pending_writes"]
    controls = payload["state"]["untrusted_observation"]["controls"]
    contexts = payload["state"]["untrusted_observation"]["contexts"]
    defaults = payload["state"]["untrusted_observation"]["control_defaults"]
    for element in obs.elements:
        restored = {**defaults, **controls[element.id]}
        restored.pop("context_ref")
        expected = element.model_dump(exclude={"id", "context"})
        assert {k: restored[k] for k in expected} == expected
    for candidate in candidates:
        option = payload["questions"]["action"]["criteria"][candidate.id]
        if candidate.element_ref:
            control = controls[option["target"]]
            assert control["value"] == obs.elements[int(candidate.element_ref[1:])].value
            assert contexts[control["context_ref"]].startswith(f"Row {candidate.element_ref[1:]}")
        if candidate.bound_value is not None:
            value = option.get("value", payload['state'].get('literal_values', {}).get(option.get('value_ref')))
            assert value == candidate.bound_value
    assert memory.export() == saved
    assert [a.model_dump() for a in candidates] == original_candidates


def test_emergency_pin_table_retains_all_refs_recent_facts_and_pending_state():
    from jev_browser.context_budget import _pool

    task, obs, memory = sample()
    memory.key_nodes = {str(i): {
        'source': {'url': 'about:blank', 'quote': f'Critical record {i}: ' + 'exact detail ' * 50},
        'interpretation': f'Stage {i}: ' + 'important fact ' * 30,
        'verification': 'quote_grounded_only',
    } for i in range(40)}
    payload = {'state': {'trusted_goal': task.objective, 'hard_constraints': ['No replay'],
                         'untrusted_observation': obs.model_dump(),
                         'untrusted_memory': memory.context()},
               'questions': {'action': {'criteria': {'a1': {'target': 'e0', 'value': 'Ada'}}}}}
    saved = copy.deepcopy(payload)
    normal = _pool(payload, 2)
    compact = _pool(payload, 3)
    view = compact['state']['untrusted_memory']
    table = view['historical_key_nodes']
    records = [dict(zip(table['columns'], row, strict=True)) for row in table['rows']]
    assert table['historical'] and table['excerpted']
    assert [r['archive_ref'] for r in records] == [archive_ref(n) for n in list(memory.key_nodes.values())[:-4]]
    assert len(records[0]['quote_excerpt']) < len(records[-1]['quote_excerpt'])
    assert view['key_nodes'] == list(memory.key_nodes.values())[-4:]
    assert view['pending_writes'] == payload['state']['untrusted_memory']['pending_writes']
    assert compact['state']['trusted_goal'] == task.objective
    assert compact['state']['hard_constraints'] == ['No replay']
    assert compact['state']['untrusted_observation'] == normal['state']['untrusted_observation']
    assert compact['questions'] == payload['questions']
    projected, metrics = project_request(payload, max_bytes=wire_bytes(normal) - 1)
    assert metrics['level'] == 3 and metrics['after_bytes'] <= metrics['max_bytes']
    assert projected['state']['untrusted_memory']['key_nodes'] == view['key_nodes']
    assert payload == saved

    chat = chat_payload(payload['state'])
    # Force the emergency tier after all ordinary projections have failed.
    boundary = wire_bytes({**chat, 'messages': [chat['messages'][0],
        {'role': 'user', 'content': json.dumps(normal['state'], ensure_ascii=False, separators=(',', ':'))}]}) - 1
    projected, metrics = project_chat_request(chat, max_bytes=boundary, purpose='dynamic_feedback')
    assert metrics['level'] == 3
    content = json.loads(projected['messages'][-1]['content'])
    assert content['untrusted_memory']['historical_key_nodes'] == table
    assert content['untrusted_memory']['pending_writes'] == view['pending_writes']


def test_byte_budget_matches_actual_httpx_unicode_serialization():
    payload = {'quoted': '含义 "quoted"\n新行', 'values': [False, None, 10.25]}
    assert wire_bytes(payload) == len(httpx.Request('POST', 'https://example.test', json=payload).content)


def test_old_critical_nodes_are_addressable_and_recent_quotes_remain_exact():
    task, obs, memory = sample()
    memory.key_nodes = {str(i): {'source': {'url': f'about:blank#stage-{i}',
                                        'quote': f'Record HR-{i:03} ' + 'older detail ' * 150},
                               'verification': 'quote_grounded_only'} for i in range(12)}
    saved = copy.deepcopy(memory.export())
    payload = {'state': {'trusted_goal': task.objective,
                         'untrusted_observation': obs.model_dump(),
                         'untrusted_memory': memory.context()}, 'questions': {}}
    projected, metrics = project_request(payload, max_bytes=20000)
    assert metrics['level'] > 0
    pins = projected['state']['untrusted_memory']['key_nodes']
    for i, pin in enumerate(pins[:-4]):
        assert pin['archive_ref'] == archive_ref(memory.key_nodes[str(i)])
        assert pin['historical'] and pin['excerpted']
        assert 'source' not in pin  # An excerpt is not a new exact evidence source.
        assert projected['state']['untrusted_memory']['key_node_urls'][pin['url_ref']] == memory.key_nodes[str(i)]['source']['url']
    assert pins[-4:] == list(memory.key_nodes.values())[-4:]
    assert memory.export() == saved
    assert 'recent_events' not in projected['state']['untrusted_memory']
    assert 'actions_since_brain' not in projected['state']['untrusted_memory']


async def test_read_only_archive_lookup_precedes_guidance_and_keeps_original_provenance():
    from jev_browser.dynamic import ReadbackUnresolved

    task, obs, memory = sample()
    old = {'source': {'url': 'about:blank#old', 'quote': 'Earlier record HR-007',
                      'observation_id': 'old-observation'}, 'verification': 'quote_grounded_only'}
    ref = archive_ref(old)

    class Brain:
        calls = 0
        async def review(self, *args, retrieved_evidence=None, **kwargs):
            self.calls += 1
            if retrieved_evidence is None:
                return Feedback(next_goal='Need exact history', complete=True,
                                evidence_requests=[ref])  # Lookup claims are never accepted yet.
            assert retrieved_evidence[ref] == old
            return Feedback(next_goal='Continue with the observed form', working_memory='Earlier record HR-007')

    brain = Brain()
    agent = DynamicController(task, None, None, feedback=brain)
    agent.memory.evidence['old'] = copy.deepcopy(old)
    feedback = await agent.review(obs)
    assert not feedback.complete and brain.calls == agent.feedback_calls == 2
    assert agent.memory.evidence['old'] == old and not agent.memory.events
    assert any(e['kind'] == 'evidence_retrieved' and not e['browser_action_dispatched'] for e in agent.events)

    class Unknown:
        async def review(self, *args, **kwargs):
            return Feedback(next_goal='Fetch', evidence_requests=['not-a-supplied-archive-ref'])
    agent.feedback_model = Unknown()
    with pytest.raises(ReadbackUnresolved):
        await agent.review(obs)


def chat_payload(content):
    return {'model': 'offline', 'messages': [
        {'role': 'system', 'content': 'Use only the original goal.'},
        {'role': 'user', 'content': json.dumps(content, ensure_ascii=False)}]}


def test_shared_chat_projection_preserves_selected_options_pending_and_readback_evidence():
    task, obs, memory = sample()
    obs.elements[0].options = ['Ada', 'Another exact option']
    content = {'trusted_goal': task.objective, 'hard_constraints': task.constraints,
               'untrusted_observation': obs.model_dump(), 'untrusted_memory': memory.context(),
               'selected_action': {'element_ref': 'e0', 'operation': 'select', 'bound_value': 'Ada'},
               'current_visible_evidence': 'Exact fresh evidence',
               'sourced_evidence_archive': [{'url': 'about:blank#old', 'quote': 'An early quoted record'}]}
    payload = chat_payload(content)
    saved = copy.deepcopy(payload)
    projected, metrics = project_chat_request(payload, max_bytes=96000, purpose='dynamic_input')
    restored = json.loads(projected['messages'][-1]['content'])
    assert restored['trusted_goal'] == task.objective
    assert list(restored['untrusted_observation']['controls']) == ['e0']
    assert restored['untrusted_observation']['controls']['e0']['options'] == obs.elements[0].options
    assert restored['untrusted_memory']['pending_writes'] == content['untrusted_memory']['pending_writes']
    assert restored['current_visible_evidence'] == content['current_visible_evidence']
    assert restored['sourced_evidence_archive'] == content['sourced_evidence_archive']
    assert metrics['after_bytes'] == wire_bytes(projected)
    assert payload == saved


async def test_chat_overflow_never_dispatches_or_truncates_completion_evidence(monkeypatch):
    task, obs, memory = sample()
    content = {'trusted_goal': task.objective, 'hard_constraints': [],
               'untrusted_observation': obs.model_dump(), 'untrusted_memory': memory.context(),
               'sourced_evidence_archive': [{'url': 'about:blank', 'quote': 'Evidence ' * 10000}]}
    payload = chat_payload(content)
    saved = copy.deepcopy(payload)
    requests = []
    monkeypatch.setenv('BRAIN_FINISH_CONTEXT_MAX_BYTES', '24000')
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = ModelTransport('https://example.test', 'offline', 'offline', client=client)
        with pytest.raises(ContextBudgetExceeded) as exc:
            await transport.post(payload, 'dynamic_finish')
    assert not requests and payload == saved
    assert exc.value.metrics['sections']['sourced_evidence_archive'] > 24000


async def test_protected_overflow_stops_before_http():
    task, obs, memory = sample()
    memory.key_nodes["large"] = {"source": {"quote": "关键" * 10_000}}
    transport = Capture()
    with pytest.raises(ContextBudgetExceeded):
        await JevPolicy(transport).choose(task, obs, memory, None, generate_dynamic(obs, task))
    assert not transport.requests


def test_pressure_levels_pool_duplicate_context_and_preserve_capabilities():
    _, obs, memory = sample()
    obs.elements[1].context = obs.elements[0].context
    obs.elements[1].enabled = False
    payload = {"state": {"trusted_goal": "Original", "hard_constraints": ["No submission"],
                         "untrusted_observation": obs.model_dump(),
                         "untrusted_memory": memory.context()}, "questions": {}}
    first, metrics = project_request(payload)
    tighter, squeezed = project_request(payload, max_bytes=metrics["after_bytes"] - 100)
    assert squeezed["level"] > metrics["level"]
    controls = tighter["state"]["untrusted_observation"]["controls"]
    assert controls["e0"]["context_ref"] == controls["e1"]["context_ref"]
    assert controls["e1"]["enabled"] is False
    assert first["state"]["untrusted_memory"]["key_nodes"] == tighter["state"]["untrusted_memory"]["key_nodes"]


async def test_critical_nodes_are_grounded_and_survive_later_reviews():
    task, obs, _ = sample()
    obs.text = "Record HR-007"

    class Brain:
        async def review(self, *args, **kwargs):
            return Feedback(next_goal="Continue", notes=[
                EvidenceNote(quote="Record HR-007", critical=True),
                EvidenceNote(quote="Invented success", critical=True)])

    agent = DynamicController(task, None, None, feedback=Brain())
    await agent.review(obs)
    assert len(agent.memory.key_nodes) == 1
    node = next(iter(agent.memory.key_nodes.values()))
    assert node["verification"] == "quote_grounded_only"
    assert node["source"]["observation_id"] == "o1"
    assert node["source"]["quote"] == "Record HR-007"
    agent.memory.feedback = {"working_memory": "Later stage"}
    assert json.dumps(agent.memory.context()).count("HR-007") >= 1


def test_wait_batches_preserve_recent_meaningful_actions_without_changing_archive():
    events = []
    for i in range(6):
        events.append({'operation': 'fill', 'description': f'Field {i}',
                       'receipt': {'status': 'ok'}, 'action': {'bound_value': f'Value {i}'}})
        events.extend({'operation': 'wait', 'description': 'Waiting', 'receipt': {'status': 'ok'}}
                      for _ in range(20))
    saved = copy.deepcopy(events)
    view = history_view(events)
    assert view['total'] == 126 and len(view['recent']) == 12
    assert view['recent'][-1]['consecutive_waits'] == 20
    assert view['recent'][-2]['bound_value'] == 'Value 5'
    assert view['recent'][-6]['bound_value'] == 'Value 3'
    assert events == saved


def test_projection_preserves_runtime_errors_and_merges_exact_pins_with_provenance():
    task, obs, memory = sample()
    obs.errors = ['page_error:fields_dict failure'] + [f'blocked_request:{i}' for i in range(20)]
    node = next(iter(memory.key_nodes.values()))
    memory.key_nodes['duplicate'] = copy.deepcopy(node)
    memory.key_nodes['duplicate']['source']['observation_id'] = 'later'
    saved = copy.deepcopy(memory.export())
    payload = {'state': {'trusted_goal': task.objective, 'untrusted_observation': obs.model_dump(),
                         'untrusted_memory': memory.context()}, 'questions': {}}
    projected, _ = project_request(payload)
    state = projected['state']
    assert 'page_error:fields_dict failure' in state['untrusted_observation']['errors']
    pins = state['untrusted_memory']['key_nodes']
    assert len(pins) == 1 and pins[0]['source']['quote'] == 'Record HR-007'
    assert pins[0]['additional_source_refs'] == [{'observation_id': 'later'}]
    assert memory.export() == saved
