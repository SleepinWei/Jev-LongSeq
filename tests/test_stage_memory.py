"""Role-specific memory keeps unresolved work and never promotes local evidence."""

import copy
import json

import pytest

from jev_browser.context_budget import (
    archive_ref,
    project_chat_request,
    project_request,
    stage_memory_view,
    wire_bytes,
)


def notebook():
    actions, checkpoints = [], []
    for i in range(12):
        source = {"url": f"https://test.example/record/{i}", "observation_id": f"o{i}",
                  "document_version": f"v{i}", "tab_id": "tab", "visible_excerpt": "Saved form " * 100}
        actions.append({"environment_id": "fresh", "action_key": f"a{i}", "operation": "click",
                        "target": "Save", "confirmation_scope": "action_effect",
                        "business_commit_confirmed": False, "source": source,
                        "proof": {"exact_quotes": ["Saved record " * 100]}})
        checkpoints.append({"environment_id": "fresh", "action_key": f"a{i}", "target": "Save",
                            "stage_goal": f"Save vendor V-{i}", "status": "write_effect_confirmed",
                            "source": copy.deepcopy(source), "visible_excerpt": "Saved form " * 100,
                            "fields": [{"name": "Vendor", "value": f"V-{i}"}],
                            "proof": {"exact_quotes": ["Saved record " * 100]}})
    return {"current_environment_readbacks": {"environment_id": "fresh", "actions": actions},
            "write_checkpoints": checkpoints, "key_nodes": [{"source": {"url": "old", "quote": "ID-42"}}],
            "working_memory": "Payment depends on posting Journal J-42; neither is verified.",
            "brain_guidance": "Post the journal before paying.",
            "pending_writes": [{"action": {"bound_value": "0.00", "description": "Debit row 2"},
                                "before_excerpt": "Exact old row", "waits": 2}],
            "unresolved_verifications": [{"goal": "Verify J-42 posted", "status": "unknown"}],
            "execution_scope": {"bindings": {"row2": {"value": "0.00"}}},
            "planned_inputs": [{"name": "Debit row 2", "value": "0.00"}],
            "resume": {"environment_recreated": True},
            "interrupted_operations": [{"description": "Save J-42", "disposition": "not confirmed"}]}


def content(raw):
    return {"trusted_goal": "Original task including exact amounts", "hard_constraints": ["No duplicates"],
            "untrusted_memory": raw,
            "untrusted_observation": {"url": "https://test.example/current", "text": "Fresh current values",
                                       "elements": [], "errors": []}}


def test_distant_proof_index_retains_distinct_confirmation_levels_and_real_archive_refs():
    raw = notebook()
    saved = copy.deepcopy(raw)
    view = stage_memory_view(raw, {"url": "https://test.example/current"})
    assert wire_bytes(view) < wire_bytes(raw) / 2
    assert len(view["historical_operations"]) == 8
    original_refs = {archive_ref(r): r for r in raw["current_environment_readbacks"]["actions"]
                     + raw["write_checkpoints"]}
    for entry in view["historical_operations"]:
        action, checkpoint = entry["readbacks"][0], entry["checkpoints"][0]
        assert not action["business_commit_confirmed"]
        assert action["confirmation_scope"] == "action_effect"
        assert checkpoint["status"] == "write_effect_confirmed"
        assert original_refs[action["archive_ref"]]["action_key"] == entry["action_key"]
        assert original_refs[checkpoint["archive_ref"]]["stage_goal"] == checkpoint["stage_goal"]
    for key in set(raw) - {"current_environment_readbacks", "write_checkpoints"}:
        assert view[key] == raw[key]
    assert raw == saved


def test_current_page_recent_records_and_handoff_keep_exact_evidence():
    raw = notebook()
    raw["planning_handoff"] = {"action_key": "a1", "mode": "navigation_only_until_fresh_planning"}
    view = stage_memory_view(raw, {"url": "https://test.example/record/0"})
    expected = [0, 1, 8, 9, 10, 11]
    assert view["current_environment_readbacks"]["actions"] == [
        raw["current_environment_readbacks"]["actions"][i] for i in expected]
    assert view["write_checkpoints"] == [raw["write_checkpoints"][i] for i in expected]
    assert not any(e["action_key"] in {"a0", "a1"} for e in view["historical_operations"])


def test_environment_identity_and_unknown_records_never_merge_or_disappear():
    raw = notebook()
    inherited = copy.deepcopy(raw["write_checkpoints"][0])
    inherited["environment_id"] = "inherited"
    unknown = {"action_key": "a0", "custom_proof": "Unknown legacy schema"}
    raw["write_checkpoints"] = [inherited, unknown] + raw["write_checkpoints"]
    view = stage_memory_view(raw, {})
    a0 = [e for e in view["historical_operations"] if e["action_key"] == "a0"]
    assert {e["environment_id"] for e in a0} == {"inherited", "fresh"}
    assert "readbacks" not in next(e for e in a0 if e["environment_id"] == "inherited")
    assert unknown in view["write_checkpoints"]
    extended = {**raw["write_checkpoints"][0], "custom_proof": "Extension cannot be discarded"}
    raw["write_checkpoints"].insert(0, extended)
    assert extended in stage_memory_view(raw, {})["write_checkpoints"]


@pytest.mark.parametrize("purpose", ["dynamic_feedback", "dynamic_input", "dynamic_finish"])
def test_non_policy_requests_unchanged_by_memory_experiment(monkeypatch, purpose):
    payload = {"messages": [{"role": "user", "content": json.dumps(content(notebook()))}]}
    monkeypatch.setenv("POLICY_MEMORY_MODE", "legacy")
    legacy, _ = project_chat_request(payload, max_bytes=200000, purpose=purpose)
    monkeypatch.setenv("POLICY_MEMORY_MODE", "stage_index_v1")
    experimental, metrics = project_chat_request(payload, max_bytes=200000, purpose=purpose)
    assert experimental == legacy
    assert "policy_memory_mode" not in metrics


@pytest.mark.parametrize("chat", [True, False])
def test_policy_wire_projection_uses_index_preserves_task_choices_and_archive(monkeypatch, chat):
    raw = notebook()
    state = content(raw)
    choices = {"a1": {"operation": "fill", "target": "row2", "value": "0.00"}}
    state["candidates"] = choices
    payload = ({"messages": [{"role": "user", "content": json.dumps(state)}]} if chat
               else {"state": state, "questions": {"action": {"criteria": choices}}})
    saved = copy.deepcopy(payload)
    monkeypatch.setenv("POLICY_MEMORY_MODE", "legacy")
    legacy, _ = (project_chat_request(payload, max_bytes=200000, purpose="llm_policy") if chat
                 else project_request(payload, max_bytes=200000))
    legacy_state = json.loads(legacy["messages"][-1]["content"]) if chat else legacy["state"]
    monkeypatch.setenv("POLICY_MEMORY_MODE", "stage_index_v1")
    projected, metrics = (project_chat_request(payload, max_bytes=200000, purpose="llm_policy") if chat
                          else project_request(payload, max_bytes=200000))
    actual = json.loads(projected["messages"][-1]["content"]) if chat else projected["state"]
    assert actual["trusted_goal"] == state["trusted_goal"]
    assert actual["hard_constraints"] == state["hard_constraints"]
    assert actual["candidates"] == choices
    assert actual["untrusted_memory"]["pending_writes"] == raw["pending_writes"]
    assert actual["untrusted_memory"]["unresolved_verifications"] == (
        legacy_state["untrusted_memory"]["unresolved_verifications"])
    assert metrics["stage_memory_applied"]
    assert metrics["memory_after_role_bytes"] < metrics["memory_before_role_bytes"]
    assert payload == saved


def test_no_evidence_reduction_keeps_original_representation(monkeypatch):
    state = content({"pending_writes": [], "working_memory": "Recent only"})
    monkeypatch.setenv("POLICY_MEMORY_MODE", "stage_index_v1")
    _, metrics = project_request({"state": state}, max_bytes=200000)
    assert not metrics["stage_memory_applied"]
    assert metrics["memory_after_role_bytes"] == metrics["memory_before_role_bytes"]


def test_invalid_policy_memory_mode_fails_before_request_projection(monkeypatch):
    monkeypatch.setenv("POLICY_MEMORY_MODE", "invalid")
    with pytest.raises(ValueError, match="POLICY_MEMORY_MODE"):
        project_request({"state": content(notebook())}, max_bytes=200000)
