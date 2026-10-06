"""Paged list receipts preserve original evidence and pending-write boundaries."""
import copy
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from jev_browser.dynamic import (
    DynamicController,
    JsonFeedback,
    ReadbackUnresolved,
    evidence_text,
    static_list_readback,
)
from jev_browser.models import ModelTransport
from jev_browser.protocol import Element, GridCell, GridRow, Observation, Task, VisibleGrid
from jev_browser.readback_packet import list_packet


def setup(rows=18):
    task = Task(id="packet", objective="Create the requested record and preserve other obligations",
                control_mode="dynamic", allowed_origins=["https://example.test"])
    obs = Observation(observation_id="current", document_version="v2", tab_id="tab",
        url="https://example.test/records", title="Records", text="Records\nImportant current warning",
        elements=[Element(id="sort", role="menuitem", name="Sort ascending")],
        grids=[VisibleGrid(id="g", rows=[GridRow(key=str(i), cells=[
            GridCell(column="Name", value=f"Other person {i}"),
            GridCell(column="Email", value=f"other{i}@example.test")]) for i in range(rows)])])
    pending = {"key": "save", "dispatch_status": "ok", "resolved": False,
        "action": {"operation": "click", "observation_id": "old"},
        "before": {"url": "https://example.test/records/new", "tab_id": "tab"},
        "before_semantics": "old", "before_controls": [], "before_dialogs": [], "before_errors": [],
        "click_target": {"role": "button", "name": "Save"},
        "field_snapshot": [{"name": "Name", "value": "Requested Person"}]}
    return task, obs, pending


def test_packets_cover_current_rows_once_and_quotes_are_exact_original_substrings():
    _, obs, pending = setup()
    snapshot = copy.deepcopy(pending)
    rows = []
    cursor = 0
    while True:
        packet = list_packet(obs, pending, cursor=cursor)
        view = packet["list_evidence"]
        assert view["rows_in_packet"] == 6 and view["rows_outside_packet"] == 12
        assert view["literal_matching_rows"] == 0
        assert "Important current warning" in packet["readback_evidence"].values()
        assert "menuitem Sort ascending = " in packet["readback_evidence"].values()
        assert all(q in evidence_text(obs) for q in packet["readback_evidence"].values())
        rows.extend(r["row_ref"] for r in view["rows"])
        cursor = view["next_cursor"]
        if cursor is None:
            break
    assert rows == [str(i) for i in range(18)]
    assert pending == snapshot


def test_late_literal_match_is_prioritized_without_becoming_confirmation():
    _, obs, pending = setup(25)
    obs.grids[0].rows[-1].cells[0].value = "Requested Person Jr"
    packet = list_packet(obs, pending)
    assert packet["list_evidence"]["rows"][0]["row_ref"] == "24"
    assert packet["list_evidence"]["rows"][0]["literal_match_fields"] == ["Name"]
    assert packet["list_evidence"]["rows_outside_packet"] == 19
    assert "retrieval hints only" in packet["list_evidence"]["scope"]


def test_oversized_cell_omission_does_not_join_quotes_across_missing_text():
    _, obs, pending = setup(1)
    obs.grids[0].rows[0].cells.insert(1, GridCell(column="Long", value="X" * 1300))
    packet = list_packet(obs, pending)
    row = packet["list_evidence"]["rows"][0]
    assert row["oversized_cells_omitted"] == 1
    assert len(row["evidence_ids"]) == 2
    assert all(q in evidence_text(obs) for q in packet["readback_evidence"].values())


@pytest.mark.parametrize("case", ["dialog", "editable", "unknown", "cross_origin", "form", "error"])
def test_scoped_packet_requires_successful_write_and_static_same_origin_list(case):
    task, obs, pending = setup()
    assert static_list_readback(task, obs, pending)
    if case == "dialog":
        obs.dialogs = ["Save failed"]
    elif case == "editable":
        obs.elements.append(Element(id="edit", role="textbox", name="Editable field", editable=True))
    elif case == "unknown":
        pending["dispatch_status"] = "unknown"
    elif case == "cross_origin":
        obs.url = "https://other.test/records"
    elif case == "form":
        obs.url = pending["before"]["url"]
    else:
        obs.errors = ["page_error:new_failure"]
    assert not static_list_readback(task, obs, pending)


@pytest.mark.parametrize("too_many", [False, True])
async def test_model_pages_same_frame_with_charged_budget_and_never_confirms_from_retrieval(too_many):
    task, obs, pending = setup(24 if too_many else 18)
    seen = []

    def respond(request):
        payload = json.loads(request.content)
        content = json.loads(payload["messages"][1]["content"])
        assert content["trusted_goal"] == task.objective
        assert content["last_transition"]["field_snapshot"] == pending["field_snapshot"]
        assert content["current_page"]["observation_id"] == obs.observation_id
        assert content["visible_control_delta"]["omitted_for_list_receipt"]
        seen.append(content["list_evidence"]["cursor"])
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
            "last_outcome": "unknown", "evidence_ids": [],
            "next_cursor": content["list_evidence"]["next_cursor"]})}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = ModelTransport("https://test.example", "test", "test", client=client)
        transport.required_goal = task.objective
        agent = DynamicController(task, AsyncMock(), None, feedback=JsonFeedback(transport))
        agent.pending = pending
        agent.memory.pending_writes["save"] = pending
        agent.consumed.add("save")
        if too_many:
            with pytest.raises(ReadbackUnresolved):
                await agent.review(obs, phase="action_readback")
        else:
            result = await agent.review(obs, phase="action_readback")
            assert result.last_outcome == "unknown"
        assert seen == [0, 6, 12] and agent.feedback_calls == 3
        assert agent.pending is pending and agent.memory.pending_writes["save"] is pending
        assert agent.consumed == {"save"} and not agent.memory.confirmed_writes
        agent.backend.execute.assert_not_awaited()


@pytest.mark.parametrize("case", ["confirm_and_next", "invented_cursor", "unseen_quote"])
async def test_model_cannot_confirm_while_requesting_or_quote_an_unseen_page(case):
    task, obs, pending = setup()
    packet = list_packet(obs, pending, cursor=6)
    unseen = packet["list_evidence"]["rows"][0]["evidence_ids"][0]

    def respond(request):
        data = {"last_outcome": "confirmed" if case == "confirm_and_next" else "unknown",
                "evidence_ids": [unseen] if case == "unseen_quote" else [],
                "next_cursor": 42 if case == "invented_cursor" else 6}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(data)}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = ModelTransport("https://test.example", "test", "test", client=client)
        with pytest.raises(ValueError):
            await JsonFeedback(transport).readback(task, obs, DynamicController(task, None, None, feedback=None).memory,
                                                   pending)


async def test_format_repair_does_not_reset_three_page_limit():
    task, obs, pending = setup(24)
    seen = []

    def respond(request):
        content = json.loads(json.loads(request.content)["messages"][1]["content"])
        cursor = content["list_evidence"]["cursor"]
        seen.append(cursor)
        answer = "{" if seen == [0, 6] else json.dumps({
            "last_outcome": "unknown", "evidence_ids": [],
            "next_cursor": content["list_evidence"]["next_cursor"]})
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"content": answer}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        agent = DynamicController(task, AsyncMock(), None,
            feedback=JsonFeedback(ModelTransport("https://test.example", "test", "test", client=client)))
        agent.pending = pending
        agent.memory.pending_writes["save"] = pending
        with pytest.raises(ReadbackUnresolved):
            await agent.review(obs, phase="action_readback")
        assert seen == [0, 6, 6, 12] and agent.feedback_calls == 4
        assert agent.pending is pending and not agent.memory.confirmed_writes
        agent.backend.execute.assert_not_awaited()


async def test_confirmed_compound_row_quote_uses_original_transition_guard():
    task, obs, pending = setup()
    obs.grids[0].rows[-1].cells[0].value = "Requested Person"

    def respond(request):
        content = json.loads(json.loads(request.content)["messages"][1]["content"])
        row = content["list_evidence"]["rows"][0]
        assert row["row_ref"] == "17"
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"last_outcome": "confirmed",
                "evidence_ids": row["evidence_ids"], "next_cursor": None})}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        agent = DynamicController(task, AsyncMock(), None,
            feedback=JsonFeedback(ModelTransport("https://test.example", "test", "test", client=client)))
        agent.pending = pending
        agent.memory.pending_writes["save"] = pending
        result = await agent.review(obs, phase="action_readback")
        assert "\n" in result.readback_quote and result.readback_quote in evidence_text(obs)
        assert agent.pending is pending and not agent.memory.confirmed_writes
        pending["dispatch_status"] = "unknown"
        assert not agent.confirm_transition(result.last_outcome, obs, "test")
        pending["dispatch_status"] = "ok"
        assert agent.confirm_transition(result.last_outcome, obs, "test")
        assert agent.pending is None and agent.memory.confirmed_writes == {"save"}
        agent.backend.execute.assert_not_awaited()
