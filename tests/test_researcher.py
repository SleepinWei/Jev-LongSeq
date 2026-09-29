import json
from types import SimpleNamespace

import pytest

from jev_browser.autoresearch import StudyGate
from jev_browser.observability import Observer
from jev_browser.protocol import AgentTuning, digest
from jev_browser.researcher import CodexResearcher


class FakeTransport:
    def __init__(self, advice):
        self.advice = advice
        self.calls = 0

    async def post(self, payload, kind):
        self.calls += 1
        self.observer.before_call()
        self.observer.model_call(
            {
                "kind": kind,
                "transport": "codex_cli",
                "input_tokens": 30,
                "output_tokens": 10,
                "cost_usd": None,
            }
        )
        self.payload = json.loads(payload["messages"][1]["content"])
        return {"choices": [{"message": {"content": json.dumps(self.advice)}}]}


def analyst(tmp_path, *, profile=None, decision="try", codes=None):
    args = SimpleNamespace(metric="tokens")
    transport = FakeTransport(
        {
            "decision": decision,
            "profile": profile,
            "hypothesis": "Test a longer autonomous window on the same task.",
            "evidence_codes": ["brain_latency"] if codes is None else codes,
        }
    )
    gate = StudyGate(tmp_path / "budget.json", seconds=60, attempts=3, tokens=1000)
    observer = Observer(tmp_path / "researcher", gate=gate)
    return CodexResearcher(args, observer, transport=transport), gate


ANALYSIS = {"findings": [{"code": "brain_latency"}]}


async def test_codex_proposal_is_single_field_and_research_overhead_is_budgeted(tmp_path):
    initial = AgentTuning()
    candidate = {**initial.model_dump(), "brain_interval": 17}
    researcher, gate = analyst(tmp_path, profile=candidate)
    change = await researcher.propose(initial, ANALYSIS, {digest(initial.model_dump())}, [])
    assert change["change"] == {"field": "brain_interval", "before": 12, "after": 17}
    assert change["source"] == "codex"
    assert gate.used["attempts"] == 1 and gate.used["known_tokens"] == 40
    assert gate.used["unknown_cost_attempts"] == 1
    assert researcher.transport.payload["incumbent_profile"] == initial.model_dump()


@pytest.mark.parametrize(
    "candidate",
    [
        {"brain_interval": 24, "recent_evidence": 1},
        {"brain_interval": 10000},
        {"verify_completion": False},
        {},
    ],
)
async def test_researcher_cannot_change_multiple_fields_or_control_boundaries(tmp_path, candidate):
    initial = AgentTuning()
    researcher, _ = analyst(tmp_path, profile={**initial.model_dump(), **candidate})
    with pytest.raises(ValueError):
        await researcher.propose(initial, ANALYSIS, set(), [])


async def test_researcher_rejects_repeats_and_invented_evidence(tmp_path):
    initial = AgentTuning()
    candidate = {**initial.model_dump(), "brain_interval": 17}
    researcher, _ = analyst(tmp_path, profile=candidate)
    with pytest.raises(ValueError, match="previously tested"):
        await researcher.propose(initial, ANALYSIS, {digest(candidate)}, [])
    researcher, _ = analyst(tmp_path, profile=candidate, codes=["invented"])
    with pytest.raises(ValueError, match="unobserved"):
        await researcher.propose(initial, ANALYSIS, set(), [])


async def test_researcher_can_stop_and_does_not_spend_on_transport_failure(tmp_path):
    researcher, _ = analyst(tmp_path, decision="stop")
    assert await researcher.propose(AgentTuning(), ANALYSIS, set(), []) is None
    assert researcher.transport.calls == 1
    failed = {"findings": [{"code": "transport_instability"}]}
    assert await researcher.propose(AgentTuning(), failed, set(), []) is None
    assert researcher.transport.calls == 1


def grounded_analysis():
    return {"findings": [{"code": "brain_latency"}], "evidence": [
        {"id": "t0/task-50/brain", "code": "brain_latency", "value": {"share": .8}},
        {"id": "t1/task-332/quality", "code": "task_incomplete", "value": {"status": "failed"}}]}


async def test_proposal_uses_rejected_task_evidence_and_preserves_prediction(tmp_path):
    initial = AgentTuning()
    researcher, _ = analyst(tmp_path, profile={**initial.model_dump(), "brain_interval": 17},
                            codes=["task_incomplete"])
    researcher.transport.advice.update(evidence_refs=["t1/task-332/quality"],
                                      expected_effect="Restore strict completion on task 332.")
    history = [{"id": 1, "selection": {"decision": "reject"}, "quality": {"strict_success": False}}]
    change = await researcher.propose(initial, grounded_analysis(), set(), history)
    assert change["evidence_refs"] == ["t1/task-332/quality"]
    assert change["expected_effect"] == "Restore strict completion on task 332."
    assert researcher.last_advice["decision"] == "try"
    assert researcher.transport.payload["trial_history"] == history


@pytest.mark.parametrize("refs,effect,code", [
    (["invented"], "Fewer calls", "brain_latency"),
    (["t1/task-332/quality"], "Fewer calls", "brain_latency"),
    ([], "Fewer calls", "brain_latency"),
    (["t0/task-50/brain"], " ", "brain_latency"),
])
async def test_fabricated_or_unbacked_evidence_cannot_start_candidate(tmp_path, refs, effect, code):
    initial = AgentTuning()
    researcher, _ = analyst(tmp_path, profile={**initial.model_dump(), "brain_interval": 17}, codes=[code])
    researcher.transport.advice.update(evidence_refs=refs, expected_effect=effect)
    with pytest.raises(ValueError):
        await researcher.propose(initial, grounded_analysis(), set(), [])
    assert researcher.last_advice is None


async def test_stop_reason_is_preserved_not_just_discarded(tmp_path):
    researcher, _ = analyst(tmp_path, decision="stop")
    researcher.transport.advice["hypothesis"] = "No change is supported by the recorded tasks."
    assert await researcher.propose(AgentTuning(), grounded_analysis(), set(), []) is None
    assert researcher.last_advice["hypothesis"] == "No change is supported by the recorded tasks."
