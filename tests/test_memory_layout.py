"""Maximum-pressure wire views must restore all checkpoint evidence exactly."""

import copy
import json

from jev_browser.context_budget import (
    _pool,
    compact_memory_layout,
    evidence_delta_view,
    memory_view,
    project_chat_request,
    project_request,
    wire_bytes,
)
from jev_browser.protocol import Element, Observation

URL = "https://example.test/desk/employee-separation/HR-EMP-SEP-2026-00001"


def notebook():
    return {
        "working_memory": "Original unfinished journal and payment work",
        "key_nodes": [], "blockers": ["Unresolved report"],
        "pending_writes": [{"url": URL, "bound_value": "Exact pending input", "status": "unknown"}],
        "current_environment_readbacks": {"environment_id": "env", "actions": [
            {"action_key": str(i), "operation": "click", "target": "Save",
             "confirmation_scope": "business_commit", "business_commit_confirmed": True,
             "source": {"url": URL, "observation_id": str(i), "visible_excerpt": "Saved record"},
             "proof": {"exact_value": "Record " + str(i)}} for i in range(12)]},
        "write_checkpoints": [
            {"environment_id": "env", "action_key": str(i), "stage_goal": "Save requested record",
             "status": "business_commit_confirmed", "fields": [{"name": "Name", "value": str(i)}],
             "visible_excerpt": "Saved record", "proof": {"exact_value": str(i)},
             "source": {"url": URL, "observation_id": str(i)}} for i in range(6)],
        "opened_pages": [{"url": URL, "title": "Record " + str(i), "observed": True,
                          "open_tab_ids": ["tab"], "excerpt": "Record page"} for i in range(12)],
    }


def restore_layout(memory):
    memory = copy.deepcopy(memory)
    for owner, key in ((memory.get("current_environment_readbacks", {}), "actions"),
                       (memory, "write_checkpoints"), (memory, "opened_pages")):
        if columns := owner.pop(key + "_columns", None):
            owner[key] = [dict(zip(columns[row[0]], row[1:], strict=True)) for row in owner[key]]
    urls = memory.pop("memory_urls", {})

    def restore(value):
        if isinstance(value, dict):
            if "memory_url_ref" in value:
                value["url"] = urls[value.pop("memory_url_ref")]
            for child in value.values():
                restore(child)
        elif isinstance(value, list):
            for child in value:
                restore(child)

    restore(memory)
    return memory


def test_wire_schema_and_url_sharing_is_reversible_with_exact_pending_and_recent_evidence():
    raw = notebook()
    saved = copy.deepcopy(raw)
    compacted = compact_memory_layout(raw)
    assert wire_bytes(compacted) < wire_bytes(raw)
    assert compacted["pending_writes"] == raw["pending_writes"]
    assert "actions_columns" in compacted["current_environment_readbacks"]
    assert restore_layout(compacted) == raw
    assert raw == saved


def test_maximum_pressure_representation_fits_without_changing_task_or_observation_controls():
    raw = notebook()
    obs = Observation(observation_id="fresh", document_version="v1", tab_id="tab", url=URL,
        title="Record", text="Saved record", elements=[Element(id="next", role="button", name="Quick new")])
    payload = {"state": {"trusted_goal": "Exact original task", "hard_constraints": ["No duplicates"],
        "untrusted_observation": obs.model_dump(), "untrusted_memory": raw},
        "questions": {"action": {"criteria": {"a1": {"operation": "click", "target": "next"}}}}}
    saved = copy.deepcopy(payload)
    ordinary = _pool(payload, 3)
    compacted = _pool(payload, 4)
    assert restore_layout(compacted["state"]["untrusted_memory"]) == memory_view(raw, 2)
    assert compacted["state"]["untrusted_observation"] == ordinary["state"]["untrusted_observation"]
    assert compacted["state"]["trusted_goal"] == payload["state"]["trusted_goal"]
    assert compacted["state"]["hard_constraints"] == payload["state"]["hard_constraints"]
    assert compacted["questions"] == payload["questions"]
    assert wire_bytes(compacted) < wire_bytes(ordinary)
    boundary = min(wire_bytes(_pool(payload, level)) for level in range(4)) - 1
    _, metrics = project_request(payload, max_bytes=boundary)
    assert metrics["level"] == 4 and metrics["after_bytes"] <= metrics["max_bytes"]
    assert payload == saved


def test_small_memory_stays_a_regular_dictionary_without_schema_overhead():
    obs = Observation(observation_id="fresh", document_version="v1", tab_id="tab", url="about:blank",
                      title="Page", text="Page")
    payload = {"state": {"trusted_goal": "Read page", "untrusted_observation": obs.model_dump(),
                         "untrusted_memory": {"working_memory": "Recent fact", "key_nodes": []}}}
    assert "memory_lookup" not in _pool(payload, 4)["state"]["context_view"]


def decode_delta(table):
    return [{"url": table["urls"][row[0]],
             "quote": "\n".join(table["lines"][ref] for ref in row[1])} for row in table["rows"]]


def test_historical_evidence_line_catalog_preserves_every_quote_and_order_exactly():
    records = [{"url": URL, "quote": '\n共同标题 "Name"\r\n' +
                "\n".join(["Shared report field" + str(j) for j in range(50)]) +
                f"\nQuery attempt {i}\n\n"} for i in range(45)]
    original = copy.deepcopy(records)
    table = evidence_delta_view(records)
    assert isinstance(table, dict) and wire_bytes(table) < wire_bytes(records) / 2
    assert decode_delta(table) == records
    assert records == original


def test_brain_pressure_pools_delta_but_keeps_exact_current_evidence_task_schema_and_pending():
    raw = notebook()
    obs = Observation(observation_id="fresh", document_version="v1", tab_id="tab", url=URL,
                      title="Report", text="Nothing to show")
    records = [{"url": URL, "quote": "\n".join(["Repeated observation field " + str(j)
               for j in range(50)]) + f"\nRefresh {i}"} for i in range(45)]
    content = {"trusted_goal": "Original goal", "hard_constraints": ["No resubmission"],
        "untrusted_observation": obs.model_dump(), "untrusted_memory": raw,
        "new_evidence_since_last_brain_call": records,
        "current_visible_evidence": "Exact current outcome evidence",
        "schema": {"type": "object"}, "schema_error": None,
        "retrieved_historical_evidence": {"ref": "Exact retrieved quote"}}
    payload = {"messages": [{"role": "system", "content": "Plan from observed controls"},
                            {"role": "user", "content": json.dumps(content)}]}
    original = copy.deepcopy(payload)
    projected, metrics = project_chat_request(payload, max_bytes=35000, purpose="dynamic_feedback")
    result = json.loads(projected["messages"][-1]["content"])
    assert metrics["level"] >= 3
    assert decode_delta(result["new_evidence_since_last_brain_call"]) == records
    for key in ["trusted_goal", "hard_constraints", "current_visible_evidence", "schema",
                "schema_error", "retrieved_historical_evidence"]:
        assert result[key] == content[key]
    assert result["untrusted_memory"]["pending_writes"] == raw["pending_writes"]
    assert payload == original


def test_small_delta_or_records_with_extra_provenance_stay_unchanged():
    records = [{"url": "about:blank", "quote": "Current quote"}]
    assert evidence_delta_view(records) == records
    records[0]["observation_id"] = "exact-source"
    assert evidence_delta_view(records) == records
