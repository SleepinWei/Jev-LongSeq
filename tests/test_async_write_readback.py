"""Asynchronous Save navigation is new evidence, not automatic confirmation."""
from unittest.mock import AsyncMock

import pytest

from jev_browser.dynamic import DynamicController, EvidenceNote, Feedback, semantic_key
from jev_browser.protocol import Budget, Decision, Element, Observation, Operation, Receipt, Task


def definition():
    return Task(id="async-save", sandbox=True, control_mode="dynamic",
                objective="Save the requested record", allowed_origins=["https://example.test"])


def page(saved=False):
    return Observation(observation_id="saved" if saved else "draft", document_version="v2" if saved else "v1",
        url="https://example.test/records/record-1" if saved else "https://example.test/records/new",
        tab_id="tab", title="Record", text="Record-1 Draft" if saved else "Not Saved",
        elements=[Element(id="submit" if saved else "save", role="button", name="Submit" if saved else "Save")])


@pytest.mark.parametrize("change,expected", [("route", True), ("same", False), ("origin", False),
                                           ("scheme", False), ("tab", False), ("unknown", False)])
async def test_refresh_reassesses_only_authorized_same_origin_tab_and_is_bounded(change, expected):
    old, fresh = page(), page(True)
    backend = AsyncMock()
    if change == "same":
        fresh = old.model_copy(update={"observation_id": "new-handle"})
    elif change == "origin":
        fresh.url = "https://other.test/records/record-1"
    elif change == "scheme":
        fresh.url = "http://example.test/records/record-1"
    elif change == "tab":
        fresh.tab_id = "other-tab"
    backend.observe.return_value = fresh
    agent = DynamicController(definition(), backend, None, feedback=None)
    pending = {"key": "save", "dispatch_status": "unknown" if change == "unknown" else "ok",
               "before_semantics": semantic_key(old)}
    agent.pending = pending
    agent.memory.pending_writes["save"] = pending
    agent.consumed.add("save")
    assert await agent.refresh_unknown_readback(old) is expected
    assert not await agent.refresh_unknown_readback(old)
    assert agent.pending is pending and agent.memory.pending_writes["save"] is pending
    assert not agent.memory.confirmed_writes and not agent.memory.write_checkpoints
    assert agent.consumed == {"save"}
    backend.execute.assert_not_awaited()


async def test_slow_save_refresh_route_is_reviewed_before_confirmation_without_resubmission():
    class Backend:
        dispatched = []
        post_save_observations = 0

        async def observe(self):
            if not self.dispatched:
                return page()
            self.post_save_observations += 1
            return page(self.post_save_observations > 1)

        async def execute(self, action):
            self.dispatched.append(action.element_ref)
            return Receipt(action_id=action.id, status="ok")

    backend = Backend()

    class Policy:
        async def choose(self, task, obs, memory, contract, candidates):
            op = Operation.WAIT if memory.pending_writes else Operation.CLICK
            return Decision(choice=next(a.id for a in candidates if a.operation == op),
                            outcome="unknown" if memory.pending_writes else "none")

    class Brain:
        frames = []

        async def review(self, task, obs, memory, *, phase, **kwargs):
            if phase == "initial":
                return Feedback(next_goal="Save the record")
            self.frames.append((phase, obs.url))
            if phase in {"write_checkpoint", "finish"}:
                return Feedback(next_goal="Done", complete=True, answer="Saved",
                                notes=[EvidenceNote(quote="Record-1 Draft")])
            if obs.url.endswith("/record-1"):
                return Feedback(next_goal="Review remaining work", last_outcome="confirmed",
                                readback_quote="Record-1 Draft", notes=[EvidenceNote(quote="Record-1 Draft")])
            return Feedback(next_goal="Inspect the save result", last_outcome="unknown")

    brain = Brain()
    agent = DynamicController(definition(), backend, Policy(), feedback=brain,
                              budget=Budget(max_cycles=8))
    result = await agent.run()
    assert result.status == "success", result.reason
    assert backend.dispatched == ["save"]
    assert brain.frames[:2] == [("uncertain_outcome", page().url), ("uncertain_outcome", page(True).url)]
    assert not agent.pending and not agent.memory.pending_writes
    assert len(agent.memory.write_checkpoints) == 1
    refresh = next(e for e in agent.events if e["kind"] == "unknown_readback_refreshed")
    assert refresh["changed"] and refresh["location_changed"] and not refresh["action_confirmed"]
