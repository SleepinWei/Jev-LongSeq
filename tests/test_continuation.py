import json

import httpx
import pytest

from jev_browser.continuation import (
    load_continuation,
    reconstruct_ui,
    replay_draft_step,
    restore_controller,
    validate_continuation_models,
)
from jev_browser.dynamic import DynamicController, JsonFeedback
from jev_browser.memory import Memory
from jev_browser.models import ModelTransport, state
from jev_browser.observability import Observer
from jev_browser.protocol import Action, Element, Observation, Operation, Receipt, Task


async def test_unknown_recovery_before_run_reports_original_error_without_replaying(tmp_path, monkeypatch):
    definition = Task(id="recovery", control_mode="dynamic", objective="Finish the draft", sandbox=True)
    option = Element(id="employee", role="option", name="HR-EMP-00007 Ananya Reddy")
    obs = Observation(observation_id="o", document_version="v", tab_id="tab", url="about:blank",
                      title="Draft", text="Not Saved", elements=[option])

    class Browser:
        calls = 0

        async def observe(self):
            return obs

        async def execute(self, action):
            self.calls += 1
            return Receipt(action_id=action.id, status="unknown", detail="Element is not attached to the DOM")

    browser = Browser()
    observer = Observer(tmp_path)
    monkeypatch.setattr("jev_browser.controller.time.monotonic", lambda: 100.0)
    controller = DynamicController(definition, browser, None, feedback=None)
    try:
        await replay_draft_step(browser, definition, option.model_dump(), Operation.CLICK,
                               None, observer, "derived-reselect")
    except ValueError as exc:
        monkeypatch.setattr("jev_browser.controller.time.monotonic", lambda: 102.0)
        result = controller.result("failed", f"{type(exc).__name__}: {exc}")
    else:
        pytest.fail("Unknown recovery must stop without replay")
    assert result.reason == "ValueError: recovery UI action was not confirmed; no replay: unknown"
    assert result.elapsed_s == 2.0 and result.actions == result.cycles == 0
    assert browser.calls == 1
    assert not controller.memory.confirmed_actions and not controller.memory.confirmed_writes
    event = json.loads((tmp_path / "resume-bootstrap.jsonl").read_text())
    assert event["receipt"]["status"] == "unknown"


@pytest.fixture
def checkpoint_files(tmp_path):
    task = Task(id="draft", control_mode="dynamic", objective="Finish the original requested form",
                sandbox=True)
    manifest = {"run_id": "parent", "fixture_hash": "fixture", "image_ids": {"app": "sha256:old"},
                "upstream_revision": "revision", "port_map": {"app": 31000}, "slot_prefix": "jevsaas"}
    login = Observation(observation_id="login", document_version="v1", tab_id="tab-0",
                        url="about:blank", title="Login", text="Login",
                        elements=[Element(id="login", role="button", name="Login")])
    draft = Observation(observation_id="draft", document_version="v2", tab_id="tab-0",
                        url="about:blank", title="Draft", text="Not Saved\nNo rows",
                        elements=[Element(id="employee", role="textbox", name="Employee",
                                          value="HR-EMP-00007", editable=True),
                                  Element(id="unknown", role="button", name="")])

    def click(obs, element):
        return Action(id=element, operation=Operation.CLICK, observation_id=obs.observation_id,
                      document_version=obs.document_version, tab_id=obs.tab_id,
                      element_ref=element, description=element, effect="write").model_dump()

    first, unknown = click(login, "login"), click(draft, "unknown")
    rows = [{"kind": "observation", "observation": login.model_dump()},
            {"kind": "action", "action": first, "receipt": {"status": "ok"}},
            {"kind": "observation", "observation": draft.model_dump()},
            {"kind": "action", "action": unknown, "receipt": {"status": "ok"}},
            {"kind": "observation", "observation": draft.model_dump()}]
    memory = Memory()
    memory.dynamic_mode = True
    memory.observe(draft)
    memory.feedback = {"next_goal": "Finish draft", "working_memory": "Recent exact ledger\nHR-EMP-00007",
                       "evidence_cursor": 1, "action_cursor": 2, "last_outcome": "unknown"}
    memory.evidence["old"] = {"source": {"url": "about:blank", "quote": "Older sourced fact"}}
    memory.pending_writes["unknown-key"] = {"action": unknown, "waits": 1}
    memory.confirmed_writes.add("login-key")
    for name, value in [("task", task.model_dump()), ("manifest", manifest), ("memory", memory.export())]:
        (tmp_path / f"{name}.json").write_text(json.dumps(value))
    (tmp_path / "trajectory.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    (tmp_path / "model-calls.jsonl").write_text(json.dumps({"model": "test"}))
    return tmp_path, task, manifest, memory, draft


@pytest.mark.parametrize("change", ["brain", "policy", "declaration", "provider", "legacy", "mixed"])
def test_brain_migration_rejects_undeclared_or_ambiguous_changes(checkpoint_files, change):
    path, task, manifest, _, _ = checkpoint_files
    calls = [{"kind": "jev", "model": "jev-latest", "endpoint_host": "api.typesafe.ai"},
             {"kind": "dynamic_feedback", "model": "old-brain", "endpoint_host": "old.example"}]
    (path / "model-calls.jsonl").write_text("\n".join(json.dumps(r) for r in calls))
    checkpoint = load_continuation(path, task, manifest)
    policy = ModelTransport("https://api.typesafe.ai/v1/systemone", "test-key", "jev-latest")
    brain = ModelTransport("https://api.arc-bench.com/v1/chat/completions", "test-key", "deepseek-v4-flash")
    declared = "deepseek-v4-flash"
    if change == "brain":
        declared = None
    elif change == "policy":
        policy.model = "other-policy"
    elif change == "declaration":
        declared = "different-brain"
    elif change == "provider":
        policy.endpoint = "https://other.example/v1/systemone"
    elif change == "legacy":
        checkpoint["model_calls"] = [{"model": "old-brain"}]
    elif change == "mixed":
        checkpoint["model_calls"].append({"kind": "policy", "model": "other"})
    with pytest.raises(ValueError):
        validate_continuation_models(checkpoint, policy, brain, brain_model=declared)
    assert "brain_migration" not in checkpoint["memory"].resume_context


def test_brain_migration_records_provenance_without_changing_goal_or_memory(checkpoint_files):
    path, task, manifest, _, _ = checkpoint_files
    calls = [{"kind": "jev", "model": "jev-latest", "endpoint_host": "api.typesafe.ai"},
             {"kind": "dynamic_feedback", "model": "deepseek-flash", "endpoint_host": "api.deepseek.com"},
             {"kind": "dynamic_input", "model": "deepseek-flash", "endpoint_host": "api.deepseek.com"}]
    (path / "model-calls.jsonl").write_text("\n".join(json.dumps(r) for r in calls))
    checkpoint = load_continuation(path, task, manifest)
    before = checkpoint["memory"].export()
    policy = ModelTransport("https://api.typesafe.ai/v1/systemone", "test-key", "jev-latest")
    brain = ModelTransport("https://api.arc-bench.com/v1/chat/completions", "test-key", "deepseek-v4-flash")
    validate_continuation_models(checkpoint, policy, brain, brain_model=brain.model)
    after = checkpoint["memory"].export()
    migration = after["resume_context"].pop("brain_migration")
    assert after == before
    assert migration["previous_models"] == ["deepseek-flash"]
    assert migration["previous_endpoint_hosts"] == ["api.deepseek.com"]
    assert migration["endpoint_host"] == "api.arc-bench.com"
    assert migration["model"] == brain.model
    assert migration["policy_model"] == policy.model


@pytest.mark.parametrize("old_kind", ["jev", "llm_policy"])
def test_explicit_policy_migration_preserves_task_memory_and_audits_provider(checkpoint_files, old_kind):
    path, task, manifest, _, _ = checkpoint_files
    calls = [{"kind": old_kind, "model": "jev-latest", "endpoint_host": "api.typesafe.ai"},
             {"kind": "dynamic_feedback", "model": "deepseek-flash", "endpoint_host": "api.deepseek.com"}]
    (path / "model-calls.jsonl").write_text("\n".join(json.dumps(r) for r in calls))
    checkpoint = load_continuation(path, task, manifest)
    before = checkpoint["memory"].export()
    policy = ModelTransport("https://api.deepseek.com/v1/chat/completions", "test", "deepseek-flash")
    brain = ModelTransport("https://api.deepseek.com/v1/chat/completions", "test", "deepseek-flash")
    validate_continuation_models(checkpoint, policy, brain, policy_model="deepseek-flash")
    after = checkpoint["memory"].export()
    migration = after["resume_context"].pop("policy_migration")
    assert after == before
    assert migration["previous_models"] == ["jev-latest"]
    assert migration["previous_endpoint_hosts"] == ["api.typesafe.ai"]
    assert migration["previous_call_kinds"] == [old_kind]
    assert migration["model"] == migration["brain_model"] == "deepseek-flash"
    assert migration["endpoint_host"] == migration["brain_endpoint_host"] == "api.deepseek.com"


@pytest.mark.parametrize("change", ["undeclared", "declaration", "brain", "brain_provider", "ledger"])
def test_policy_migration_does_not_silently_change_other_components(checkpoint_files, change):
    path, task, manifest, _, _ = checkpoint_files
    calls = [{"kind": "jev", "model": "jev-latest", "endpoint_host": "api.typesafe.ai"},
             {"kind": "dynamic_feedback", "model": "deepseek-flash", "endpoint_host": "api.deepseek.com"}]
    (path / "model-calls.jsonl").write_text("\n".join(json.dumps(r) for r in calls))
    checkpoint = load_continuation(path, task, manifest)
    policy = ModelTransport("https://api.deepseek.com/v1/chat/completions", "test", "deepseek-flash")
    brain = ModelTransport("https://api.deepseek.com/v1/chat/completions", "test", "deepseek-flash")
    declaration = "deepseek-flash"
    if change == "undeclared":
        declaration = None
    elif change == "declaration":
        declaration = "other-policy"
    elif change == "brain":
        brain.model = "other-brain"
    elif change == "brain_provider":
        brain.endpoint = "https://other.example/v1/chat/completions"
    else:
        checkpoint["model_calls"] = [{"model": "legacy"}]
    with pytest.raises(ValueError):
        validate_continuation_models(checkpoint, policy, brain, policy_model=declaration)
    assert "policy_migration" not in checkpoint["memory"].resume_context


def test_legacy_checkpoint_keeps_strict_default_model_check(checkpoint_files):
    path, task, manifest, _, _ = checkpoint_files
    checkpoint = load_continuation(path, task, manifest)
    client = ModelTransport("https://example.com", "test-key", "test")
    validate_continuation_models(checkpoint, client, client)
    client.model = "other"
    with pytest.raises(ValueError, match="model configuration differs"):
        validate_continuation_models(checkpoint, client, client)


async def test_resume_preserves_prompt_ledger_archive_and_history_without_replaying_unknown(checkpoint_files):
    path, task, manifest, original, draft = checkpoint_files
    checkpoint = load_continuation(path, task, manifest)
    assert len(checkpoint["recipe"]) == 1  # The unknown click is not replayed.
    assert checkpoint["memory"].feedback == original.feedback
    assert checkpoint["memory"].evidence == original.evidence
    assert len(checkpoint["memory"].events) == 2
    assert not checkpoint["memory"].pending_writes
    assert checkpoint["memory"].interrupted_writes == original.pending_writes
    assert "unknown-key" not in checkpoint["memory"].confirmed_writes

    def respond(request):
        payload = json.loads(request.content)
        content = json.loads(payload["messages"][1]["content"])
        assert content["trusted_goal"] == task.objective
        assert content["phase"] == "resume"
        assert content["untrusted_memory"]["working_memory"] == original.feedback["working_memory"]
        assert content["untrusted_memory"]["interrupted_operations"][0]["operation"] == "click"
        assert content.get("last_transition") is None
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"next_goal": "Continue the draft", "working_memory": original.feedback["working_memory"],
             "stage_entry": {"intent": "locate", "operation": "request_replan"}}
        )}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        brain = JsonFeedback(ModelTransport("https://test.example", "test", "test", client=client))
        controller = DynamicController(task, None, None, feedback=brain)
        restore_controller(controller, checkpoint, {"draft_fields_match": True})
        assert controller.initial_phase == "resume"
        await controller.review(draft, phase=controller.initial_phase)
        assert state(task, draft, controller.memory, None)["trusted_goal"] == task.objective
        assert controller.memory.evidence == original.evidence


def test_resume_restores_pinned_nodes_and_full_notebook_versions(checkpoint_files):
    path, task, manifest, _, _ = checkpoint_files
    saved = json.loads((path / "memory.json").read_text())
    pins = {"record": {"source": {"url": "about:blank", "quote": "HR-EMP-00007"},
                       "verification": "quote_grounded_only"}}
    versions = {"older": "Earlier exact notebook"}
    saved.update(key_nodes_archive=pins, working_memory_archive=versions)
    (path / "memory.json").write_text(json.dumps(saved))
    restored = load_continuation(path, task, manifest)["memory"]
    assert restored.key_nodes == pins
    assert restored.working_memory_archive == versions
    assert restored.context()["key_nodes"] == list(pins.values())


@pytest.mark.parametrize("difference", ["prompt", "permissions", "image"])
def test_resume_rejects_prompt_permission_or_environment_drift(checkpoint_files, difference):
    path, task, manifest, _, _ = checkpoint_files
    if difference == "prompt":
        task.objective += " Changed goal"
    elif difference == "permissions":
        task.allowed_operations = [Operation.WAIT]
    else:
        manifest = {**manifest, "image_ids": {"app": "sha256:changed"}}
    with pytest.raises(ValueError, match="differ"):
        load_continuation(path, task, manifest)


def test_resume_refuses_to_replay_a_save_into_a_recreated_environment(checkpoint_files):
    path, task, manifest, _, _ = checkpoint_files
    rows = [json.loads(row) for row in (path / "trajectory.jsonl").read_text().splitlines()]
    rows[0]["observation"]["elements"][0]["name"] = "Save"
    (path / "trajectory.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    with pytest.raises(ValueError, match="committed data"):
        load_continuation(path, task, manifest)


async def test_reconstructed_draft_fields_are_checked_before_model_continuation(checkpoint_files):
    path, task, manifest, _, draft = checkpoint_files
    checkpoint = load_continuation(path, task, manifest)

    class Browser:
        dispatched = False

        async def observe(self):
            if self.dispatched:
                return draft
            return Observation(observation_id="new", document_version="new", tab_id="tab-0",
                               url="about:blank", title="Login", text="Login",
                               elements=[Element(id="new-login", role="button", name="Login")])

        async def execute(self, action):
            assert action.element_ref == "new-login"
            self.dispatched = True
            return Receipt(action_id=action.id, status="ok")

    result = await reconstruct_ui(Browser(), task, checkpoint, Observer(path / "new"))
    assert result["draft_fields_match"] and result["steps"] == 1
    draft.elements[0].value = "wrong-employee"
    with pytest.raises(ValueError, match="draft differs"):
        await reconstruct_ui(Browser(), task, checkpoint, Observer())


def test_nested_resume_keeps_parent_history_and_latest_working_memory(checkpoint_files):
    path, task, manifest, original, _ = checkpoint_files
    child = path / "child"
    child.mkdir()
    saved = original.export()
    saved["brain_feedback"]["working_memory"] = "Latest child memory after another unknown click"
    saved["resume_context"] = {"source_directory": str(path)}
    saved["interrupted_writes"] = original.pending_writes
    rows = [json.loads(row) for row in (path / "trajectory.jsonl").read_text().splitlines()][-3:]
    for name, value in [("task", task.model_dump()), ("manifest", {**manifest, "run_id": "child"}),
                        ("memory", saved)]:
        (child / f"{name}.json").write_text(json.dumps(value))
    (child / "trajectory.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    (child / "model-calls.jsonl").write_text(json.dumps({"model": "test"}))
    checkpoint = load_continuation(child, task, manifest)
    assert len(checkpoint["recipe"]) == 1
    assert len(checkpoint["memory"].events) == 3
    assert checkpoint["memory"].feedback["working_memory"] == saved["brain_feedback"]["working_memory"]
    assert checkpoint["memory"].resume_context["source_run_id"] == "child"
    assert len(checkpoint["memory"].resume_context["lineage_directories"]) == 2


@pytest.fixture
def populated_child_checkpoint(checkpoint_files):
    path, task, manifest, original, draft = checkpoint_files
    child = path / "populated-child"
    child.mkdir()
    populated = draft.model_copy(deep=True)
    populated.text = "Not Saved\nActivities\n1\nReturn company laptop"
    populated.elements.append(Element(id="add", role="button", name="Add row"))
    action = Action(id="add", operation=Operation.CLICK, observation_id=populated.observation_id,
                    document_version=populated.document_version, tab_id=populated.tab_id,
                    element_ref="add", description="Add row", effect="write").model_dump()
    rows = [{"kind": "observation", "observation": populated.model_dump()},
            {"kind": "action", "action": action, "receipt": {"status": "ok"}},
            {"kind": "observation", "observation": populated.model_dump()}]
    saved = original.export()
    saved["resume_context"] = {"source_directory": str(path)}
    saved["brain_feedback"]["working_memory"] = "Latest ledger: an unsaved activity row was added"
    saved["evidence_archive"]["new"] = {"source": {"quote": "Return company laptop"}}
    for name, value in [("task", task.model_dump()), ("manifest", {**manifest, "run_id": "child"}),
                        ("memory", saved)]:
        (child / f"{name}.json").write_text(json.dumps(value))
    (child / "trajectory.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    (child / "model-calls.jsonl").write_text(json.dumps({"model": "test"}))
    return path, child, task, manifest, saved


def test_ui_rewind_preserves_latest_memory_and_history(populated_child_checkpoint):
    path, child, task, manifest, saved = populated_child_checkpoint
    checkpoint = load_continuation(child, task, manifest, ui_directory=path)
    assert checkpoint["memory"].feedback == saved["brain_feedback"]
    assert checkpoint["memory"].evidence == saved["evidence_archive"]
    assert len(checkpoint["memory"].events) == 3
    assert len(checkpoint["recipe"]) == 1
    assert "No rows" in checkpoint["last_observation"].text
    assert checkpoint["memory"].resume_context["ui_rewound"]
    assert checkpoint["memory"].resume_context["source_run_id"] == "child"
    assert checkpoint["memory"].resume_context["ui_checkpoint_run_id"] == "parent"
    with pytest.raises(ValueError, match="unsaved empty"):
        load_continuation(child, task, manifest)
    with pytest.raises(ValueError, match="lineage"):
        load_continuation(child, task, manifest, ui_directory=path / "unrelated")


@pytest.mark.parametrize("status", ["ok", "unknown"])
def test_ui_rewind_rejects_dispatched_save_even_if_pending(populated_child_checkpoint, status):
    path, child, task, manifest, saved = populated_child_checkpoint
    rows = [json.loads(r) for r in (child / "trajectory.jsonl").read_text().splitlines()]
    rows[0]["observation"]["elements"][-1]["name"] = "S a ve"
    rows[1]["receipt"]["status"] = status
    saved["pending_writes"] = {"save": {"action": rows[1]["action"], "waits": 1}}
    (child / "memory.json").write_text(json.dumps(saved))
    (child / "trajectory.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    with pytest.raises(ValueError, match="committed data"):
        load_continuation(child, task, manifest, ui_directory=path)


@pytest.mark.parametrize("wire_format", ["jev", "brain"])
async def test_original_goal_guard_blocks_changed_prompt_before_model_request(wire_format):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = ModelTransport("https://test.example", "test", "test", client=client)
        transport.required_goal = "Original exact user prompt"
        context = {"trusted_goal": transport.required_goal}

        def payload():
            return ({"state": context} if wire_format == "jev" else
                    {"messages": [{"role": "user", "content": json.dumps(context)}]})

        await transport.post(payload(), wire_format)
        context["trusted_goal"] = "Accidentally replaced with the stage guidance"
        with pytest.raises(ValueError, match="changed the original"):
            await transport.post(payload(), wire_format)
        assert len(requests) == len(transport.ledger) == 1


async def test_recovery_rejects_swapped_named_fields_and_unresolved_runtime_errors(checkpoint_files):
    path, task, manifest, _, draft = checkpoint_files
    checkpoint = load_continuation(path, task, manifest)
    checkpoint['recipe'] = []
    draft.elements.append(Element(id='second', role='textbox', name='Other', value='Second', editable=True))
    checkpoint['last_observation'] = draft.model_copy(deep=True)
    actual = draft.model_copy(deep=True)
    actual.elements[0].name = 'Other'
    actual.elements[-1].name = 'Employee'

    class Browser:
        async def observe(self):
            return actual

    with pytest.raises(ValueError, match='draft differs'):
        await reconstruct_ui(Browser(), task, checkpoint, Observer())
    actual = draft.model_copy(deep=True)
    actual.errors = ['page_error:Unhandled UI error']
    with pytest.raises(ValueError, match='unresolved UI runtime'):
        await reconstruct_ui(Browser(), task, checkpoint, Observer())


@pytest.mark.parametrize('result', ['populated', 'still_blank', 'unknown'])
async def test_one_bounded_full_form_reselect_requires_company_readback(result, monkeypatch):
    from jev_browser.continuation import repair_derived_draft

    async def immediate(_):
        pass

    monkeypatch.setattr('jev_browser.continuation.asyncio.sleep', immediate)
    task = Task(id='recovery', objective='Restore the original draft', control_mode='dynamic', sandbox=True)
    obs = Observation(observation_id='1', document_version='1', tab_id='tab-0', url='about:blank',
                      title='Draft', text='Not Saved\nNo rows',
                      errors=["page_error:Cannot read properties of undefined (reading 'fields_dict')"],
                      elements=[Element(id='employee', role='combobox', name='Employee',
                                        value='HR-007', editable=True),
                                Element(id='company', role='status', name='Company',
                                        read_only=True, required=True, enabled=False),
                                Element(id='option', role='option', name='HR-007 Ada'),
                                Element(id='search', role='combobox', name='Search', value='Ada', editable=True)])
    checkpoint = {'recipe': [{'element': obs.elements[-2].model_dump()},
                             {'element': {'role': 'button', 'name': 'Edit Full Form'}}]}
    original = obs.model_copy(deep=True)

    class Browser:
        calls = []

        async def observe(self):
            return obs.model_copy(deep=True)

        async def execute(self, action):
            self.calls.append((action.operation, action.bound_value))
            if result == 'unknown':
                return Receipt(action_id=action.id, status='unknown')
            if action.operation == Operation.FILL:
                obs.elements[0].value = action.bound_value
            elif result == 'populated':
                obs.elements[1].value = 'TechVista'
            return Receipt(action_id=action.id, status='ok')

        async def blur_input(self, obs, ref):
            self.calls.append(('blur', ref))
            return Receipt(action_id='blur', status='ok')

    browser = Browser()
    if result == 'populated':
        fresh, repaired = await repair_derived_draft(browser, task, checkpoint, original, Observer())
        assert repaired and fresh.elements[1].value == 'TechVista'
        assert fresh.elements[0].value == original.elements[0].value
        assert len(browser.calls) == 4
    else:
        with pytest.raises(ValueError, match='no replay|no retry'):
            await repair_derived_draft(browser, task, checkpoint, original, Observer())
        assert len(browser.calls) == (1 if result == 'unknown' else 4)


@pytest.mark.parametrize('duplicate_labels', [False, True])
async def test_recovery_allows_derived_sibling_expansion_but_keeps_duplicate_row_identity(duplicate_labels):
    task = Task(id='scope', objective='Restore draft', control_mode='dynamic', sandbox=True)
    original = Observation(observation_id='old', document_version='v1', tab_id='tab-0',
                           url='about:blank', title='Draft', text='Not Saved\nNo rows',
                           elements=[Element(id='first', role='textbox', name='Employee',
                                             editable=True, value='Ada', context='Row 1')])
    actual = original.model_copy(deep=True)
    if duplicate_labels:
        original.elements.append(Element(id='second', role='textbox', name='Employee',
                                         editable=True, value='Bob', context='Row 2'))
        actual.elements.append(original.elements[-1].model_copy(deep=True))
        actual.elements[0].context = 'Row 2'
        actual.elements[1].context = 'Row 1'
    else:
        actual.elements[0].context = 'Employee Ada Company TechVista Department HR'
        actual.elements.append(Element(id='company', role='status', name='Company',
                                       read_only=True, value='TechVista'))

    class Browser:
        async def observe(self):
            return actual

    checkpoint = {'recipe': [], 'last_observation': original}
    if duplicate_labels:
        with pytest.raises(ValueError, match='draft differs'):
            await reconstruct_ui(Browser(), task, checkpoint, Observer())
    else:
        result = await reconstruct_ui(Browser(), task, checkpoint, Observer())
        assert result['draft_fields_match']
