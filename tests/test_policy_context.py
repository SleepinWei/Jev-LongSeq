"""Generative choices must retain executable identity without the full action log."""

import copy
import json

import httpx
import pytest

from jev_browser.context_budget import wire_bytes
from jev_browser.dynamic import generate_dynamic
from jev_browser.memory import Memory
from jev_browser.models import (
    JsonPolicy,
    ModelTransport,
    dynamic_policy_options,
    policy_page_observation,
    state,
)
from jev_browser.protocol import Action, Element, GridCell, GridRow, Observation, Task, VisibleGrid


def page():
    return Observation(observation_id="fresh", document_version="v2", tab_id="tab",
        url="https://example.test/vendors", title="Vendors", text="All current page text",
        elements=[
            Element(id="name", role="textbox", name="Name", value="Ada", editable=True,
                    grid_ref="people", row_ref="r1"),
            Element(id="user", role="combobox", name="User", value="Ada", editable=True,
                    grid_ref="people", row_ref="r1"),
            Element(id="option", role="option", name="Ada", option_owner="user"),
            Element(id="header", role="button", name="Name", grid_ref="people"),
            Element(id="other", role="textbox", name="Name", value="Bob", editable=True,
                    grid_ref="people", row_ref="r2"),
            Element(id="menu", role="button", name="Quick new"),
            Element(id="item", role="menuitem", name="Vendor", menu_owner="menu"),
        ],
        grids=[VisibleGrid(id="people", name="People", rows=[
            GridRow(key="r1", cells=[GridCell(column="Name", value="Ada")], control_refs=["name", "user"]),
            GridRow(key="r2", cells=[GridCell(column="Name", value="Bob")], control_refs=["other"]),
        ])])


@pytest.mark.parametrize("target,expected", [
    ("option", {"name", "user", "option"}),
    ("item", {"menu", "item"}),
    ("header", {"header"}),
])
def test_page_view_keeps_exact_targets_owner_closure_rows_and_all_grid_evidence(target, expected):
    obs = page()
    memory = Memory()
    memory.dynamic_mode = True
    task = Task(id="scope", control_mode="dynamic", objective="Original task")
    content = state(task, obs, memory, None)
    original = copy.deepcopy(content)
    candidates = [a for a in generate_dynamic(obs, task) if a.element_ref == target]
    saved_obs, saved_memory, saved_actions = obs.model_dump(), memory.export(), copy.deepcopy(candidates)
    policy_page_observation(content, obs, memory, candidates)
    shown = content["untrusted_observation"]
    assert {e["id"] for e in shown["elements"]} == expected
    assert shown["elements"] == [e for e in original["untrusted_observation"]["elements"] if e["id"] in expected]
    assert shown["grids"] == original["untrusted_observation"]["grids"]
    assert shown["text"] == original["untrusted_observation"]["text"]
    assert content["untrusted_memory"] == original["untrusted_memory"]
    assert content["trusted_goal"] == task.objective
    assert obs.model_dump() == saved_obs and memory.export() == saved_memory and candidates == saved_actions


@pytest.mark.parametrize("pending,target", [(True, "name"), (False, "missing")])
def test_pending_or_unknown_target_never_filters_observation(pending, target):
    obs, memory = page(), Memory()
    memory.dynamic_mode = True
    if pending:
        memory.pending_writes["save"] = {"action": {"operation": "click", "description": "Save"},
            "expected_goal": "Persist exact vendor", "before_excerpt": "Original form", "waits": 1}
    task = Task(id="pending", control_mode="dynamic", objective="Original task")
    candidates = [Action(id="a9", operation="click", observation_id="fresh", document_version="v2",
                         tab_id="tab", element_ref=target, description="Target")]
    content = state(task, obs, memory, None)
    original = copy.deepcopy(content)
    policy_page_observation(content, obs, memory, candidates)
    assert content == original


def test_compact_choices_preserve_bound_empty_string_row_target_and_header_warning():
    obs = page()
    task = Task(id="options", control_mode="dynamic", objective='Enter "Ada".')
    candidates = generate_dynamic(obs, task)
    candidates.append(Action(id="empty", operation="fill", observation_id="fresh", document_version="v2",
                             tab_id="tab", element_ref="name", bound_value="", description="Clear Name"))
    saved = copy.deepcopy(candidates)
    options = dynamic_policy_options(obs, candidates)
    assert list(options) == [a.id for a in candidates]
    for action in candidates:
        option = options[action.id]
        assert option["operation"] == action.operation
        if action.element_ref:
            assert option["target"] == action.element_ref
        else:
            assert option["description"] == action.description
        assert ("value" in option) is (action.bound_value is not None)
        if action.bound_value is not None:
            assert option["value"] == action.bound_value
        if action.element_ref == "header":
            assert "not a row field input" in option["description"]
    assert candidates == saved


async def test_json_policy_projects_dense_candidate_page_before_http_with_same_execution_actions(monkeypatch):
    obs, memory = page(), Memory()
    memory.dynamic_mode = True
    memory.feedback = {"next_goal": "Select Ada", "execution_scope": {"bindings": {"option": {"operations": ["click"]}}}}
    # Dense controls outside the current stage should not consume the choice budget.
    obs.elements += [Element(id=f"dense{i}", role="textbox", name=f"Vendor {i}", value="Unique value " + str(i),
                             context="Form " + str(i) + " large repeated context " * 80, editable=True)
                     for i in range(120)]
    task = Task(id="dense", control_mode="dynamic", objective="Original vendor task")
    candidates = [a for a in generate_dynamic(obs, task) if a.element_ref == "option"]
    original = copy.deepcopy(candidates)
    snapshot = memory.export()
    received = []
    monkeypatch.setenv("POLICY_CONTEXT_MAX_BYTES", "12000")

    def respond(request):
        payload = json.loads(request.content)
        assert len(request.content) <= 12000
        content = json.loads(payload["messages"][-1]["content"])
        received.append(content)
        assert set(content["untrusted_observation"]["controls"]) == {"option", "user", "name"}
        assert content["untrusted_memory"]["execution_scope"] == memory.feedback["execution_scope"]
        assert content["trusted_goal"] == task.objective
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"choice": candidates[0].id, "outcome": "none"})}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = ModelTransport("https://example.test", "offline", "offline", client=client)
        decision = await JsonPolicy(transport).choose(task, obs, memory, None, candidates)
    assert decision.choice == candidates[0].id and len(received) == 1
    assert received[0]["candidates"] == [{"id": key, **value} for key, value in
                                           dynamic_policy_options(obs, candidates).items()]
    assert wire_bytes(received[0]["candidates"]) < wire_bytes([a.model_dump() for a in candidates])
    assert candidates == original and memory.export() == snapshot
