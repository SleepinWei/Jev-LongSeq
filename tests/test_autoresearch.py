import argparse
import copy
import json

import pytest
from pydantic import ValidationError

from jev_browser.autoresearch import StudyGate, compare, proposal, run_research
from jev_browser.cli import common
from jev_browser.observability import ResourceLimit, analyze, write_json
from jev_browser.protocol import AgentTuning, digest


@pytest.fixture(autouse=True)
def offline_researcher(monkeypatch):
    """Search-loop tests never start real Codex or consume model quota."""
    calls = []

    class OfflineResearcher:
        def __init__(self, args, observer):
            self.transport = self

        async def propose(self, profile, analysis, tried, history):
            calls.append(profile.model_dump())
            return proposal(profile, analysis, tried)

        async def aclose(self):
            pass

    monkeypatch.setattr("jev_browser.autoresearch.CodexResearcher", OfflineResearcher)
    return calls


def report(*, tokens=100, seconds=10, success=True, errors=False):
    return {
        "manifest": {
            "code_hash": "code",
            "fixture_hash": "fixture",
            "task_hash": "goal",
            "policy": "jev",
            "backend": "playwright",
            "live_preview": False,
            "budget": {
                "max_actions": 100,
                "max_cycles": 600,
                "max_feedback_calls": 24,
                "candidate_limit": 64,
                "max_seconds": 60,
            },
        },
        "result": {
            "status": "success" if success else "failed",
            "strict_success": success,
            "actions": 10,
            "violations": [],
        },
        "grade": {"strict_success": success, "duplicates": 0, "violations": 0},
        "model_calls": [
            {
                "kind": "jev",
                "status": 200,
                "resolved_model": "jev-test",
                "latency_s": 1,
                "input_tokens": tokens,
                "output_tokens": 10,
                "cost_usd": 0.01,
                **({"error": "ConnectError"} if errors else {}),
            }
        ],
        "end_to_end_s": seconds,
        "total_cost_usd": 0.02,
    }


def select(base, candidate, metric="tokens"):
    return compare(
        base,
        candidate,
        metric=metric,
        min_improvement=0.05,
        latency_regression=0.1,
        token_regression=0.1,
    )


def test_faster_failed_run_is_not_kept_and_unknown_usage_is_not_free():
    assert select(report(), report(tokens=1, seconds=1, success=False))["decision"] == "reject"
    unknown = report(tokens=None)
    assert select(report(), unknown)["decision"] == "inconclusive"
    unknown = report()
    unknown["total_cost_usd"] = None
    assert select(report(), unknown, "cost")["decision"] == "inconclusive"


def test_improvement_requires_comparable_quality_and_non_regressing_resources():
    assert select(report(), report(tokens=50))["decision"] == "keep"
    assert select(report(), report(tokens=50, seconds=20))["decision"] == "reject"
    assert select(report(), report(tokens=110, seconds=5), "latency")["decision"] == "keep"
    assert select(report(), report(tokens=200, seconds=5), "latency")["decision"] == "reject"
    assert select(report(errors=True), report(tokens=1))["decision"] == "inconclusive"
    changed_task = report(tokens=1)
    changed_task["manifest"]["fixture_hash"] = "different"
    assert select(report(), changed_task)["decision"] == "inconclusive"
    violations = report(tokens=1)
    violations["grade"]["duplicates"] = 1
    assert select(report(), violations)["decision"] == "reject"


def test_success_restoration_is_distinguished_from_efficiency_win():
    decision = select(report(success=False), report(tokens=1000))
    assert decision["decision"] == "keep"
    assert "relative_improvement" not in decision
    assert decision["validation"] == "single_task_provisional"


def test_success_restoration_does_not_hide_unknown_usage():
    assert select(report(success=False, tokens=None), report())["decision"] == "inconclusive"


@pytest.mark.parametrize("key", ["brain_backend", "codex_model", "codex_effort", "codex_timeout"])
def test_brain_settings_must_match_for_comparison(key):
    candidate = report(tokens=1)
    candidate["manifest"][key] = "changed"
    assert select(report(), candidate)["decision"] == "inconclusive"


def test_one_parameter_proposals_are_finite_and_do_not_include_grader_answers():
    initial = AgentTuning()
    analysis = analyze(report(tokens=4000), [])
    suggestion = proposal(initial, analysis, {digest(initial.model_dump())})
    candidate = AgentTuning.model_validate(suggestion["profile"])
    assert sum(getattr(initial, k) != getattr(candidate, k) for k in initial.model_dump()) == 1
    assert "item-" not in json.dumps(suggestion)
    assert proposal(initial, analyze(report(errors=True), []), set()) is None
    with pytest.raises(ValidationError):
        AgentTuning(verify_completion=False)


def test_gate_counts_retries_before_dispatch_and_persists_consumption(tmp_path):
    gate = StudyGate(tmp_path / "budget.json", seconds=60, attempts=2, tokens=100)
    gate.before_call()
    gate.record({"input_tokens": None, "output_tokens": None, "cost_usd": None})
    gate.before_call()
    gate.record({"input_tokens": 30, "output_tokens": 4, "cost_usd": 0.01})
    with pytest.raises(ResourceLimit, match="attempt"):
        gate.before_call()
    saved = json.loads((tmp_path / "budget.json").read_text())
    resumed = StudyGate(
        tmp_path / "budget.json", seconds=60, attempts=2, tokens=100, previous=saved
    )
    with pytest.raises(ResourceLimit):
        resumed.before_call()
    assert saved["known_tokens"] == 34 and saved["unknown_usage_attempts"] == 1


def test_cost_gate_stops_when_usage_becomes_unknown(tmp_path):
    gate = StudyGate(tmp_path / "budget.json", seconds=60, attempts=10, tokens=100, cost=1)
    gate.before_call()
    gate.record({"cost_usd": None})
    with pytest.raises(ResourceLimit, match="unknown"):
        gate.before_call()


def args_for(path):
    parser = argparse.ArgumentParser()
    common(parser, demo=True)
    args = parser.parse_args([])
    args.command, args.mode, args.policy, args.planner = "autoresearch", "dynamic", "jev", "llm"
    args.output, args.records, args.max_seconds = str(path), 2, 60
    args.max_trials, args.study_seconds, args.max_model_attempts, args.max_tokens = (
        2,
        300,
        10,
        10000,
    )
    args.max_cost_usd, args.metric, args.resume = None, "tokens", False
    args.min_improvement, args.max_latency_regression, args.max_token_regression = 0.05, 0.1, 0.1
    return args


def fake_runner(results):
    called = []

    async def run(args, *, count, output):
        index = len(called)
        value = copy.deepcopy(results[index])
        called.append(args._tuning.model_dump())
        output.mkdir(parents=True)
        for call in value["model_calls"]:
            args._research_gate.before_call()
            args._research_gate.record(call)
        write_json(output / "report.json", value)
        return value

    return run, called


async def test_full_loop_retains_measured_improvement_and_exports_reusable_profile(
    tmp_path, offline_researcher
):
    runner, called = fake_runner([report(), report(tokens=50)])
    assert await run_research(args_for(tmp_path), runner=runner) == 0
    state = json.loads((tmp_path / "study.json").read_text())
    assert len(called) == 2 and state["incumbent"] == 1
    assert state["trials"][1]["selection"]["decision"] == "keep"
    assert json.loads((tmp_path / "best-profile.json").read_text()) == called[1]
    assert (tmp_path / "trial-001" / "observability.json").exists()
    assert state["budget"]["attempts"] == 2
    assert len(offline_researcher) == 1  # no unused proposal after the last trial


async def test_regression_keeps_incumbent_and_resume_does_not_repeat_completed_trials(tmp_path):
    runner, called = fake_runner([report(), report(tokens=200)])
    args = args_for(tmp_path)
    await run_research(args, runner=runner)
    state = json.loads((tmp_path / "study.json").read_text())
    assert state["incumbent"] == 0
    assert json.loads((tmp_path / "best-profile.json").read_text()) == called[0]
    args.resume = True
    await run_research(args, runner=runner)
    assert len(called) == 2
    assert json.loads((tmp_path / "budget.json").read_text())["attempts"] == 2


async def test_transport_failure_breaks_loop_without_spending_another_trial(tmp_path):
    runner, called = fake_runner([report(errors=True, success=False)])
    assert await run_research(args_for(tmp_path), runner=runner) == 1
    assert len(called) == 1
    state = json.loads((tmp_path / "study.json").read_text())
    assert state["stop_reason"] == "transport_instability" and not state["best_validated"]
    assert not (tmp_path / "best-profile.json").exists()


async def test_interrupted_trial_is_never_replayed_on_resume(tmp_path):
    async def interrupted(args, *, count, output):
        raise RuntimeError("simulated process interruption")

    args = args_for(tmp_path)
    with pytest.raises(RuntimeError):
        await run_research(args, runner=interrupted)
    args.resume = True
    with pytest.raises(ValueError, match="interrupted trial"):
        await run_research(args, runner=interrupted)


async def test_no_trial_starts_without_a_full_comparable_time_budget(tmp_path):
    runner, called = fake_runner([])
    args = args_for(tmp_path)
    args.study_seconds = 1
    assert await run_research(args, runner=runner) == 1
    assert not called
    assert (
        json.loads((tmp_path / "study.json").read_text())["stop_reason"]
        == "insufficient_time_for_a_comparable_trial"
    )


async def test_codex_subscription_cost_is_not_invented(tmp_path):
    runner, called = fake_runner([])
    args = args_for(tmp_path)
    args.metric = "cost"
    with pytest.raises(ValueError, match="subscription"):
        await run_research(args, runner=runner)
    assert called == []


async def test_researcher_failure_stops_without_changing_the_incumbent(tmp_path, monkeypatch):
    from jev_browser.autoresearch import CodexResearcher

    async def fail(self, *args):
        raise RuntimeError("simulated Codex connection error")

    monkeypatch.setattr(CodexResearcher, "propose", fail)
    runner, called = fake_runner([report()])
    await run_research(args_for(tmp_path), runner=runner)
    state = json.loads((tmp_path / "study.json").read_text())
    assert state["stop_reason"] == "researcher_failed"
    assert state["incumbent"] == 0 and len(called) == 1
    assert json.loads((tmp_path / "best-profile.json").read_text()) == called[0]


async def test_rejected_experiment_is_visible_to_the_next_analysis(tmp_path, monkeypatch):
    seen = []

    class Analyst:
        def __init__(self, args, observer):
            self.transport = self

        async def propose(self, profile, analysis, tried, history):
            seen.append((analysis, history))
            return proposal(profile, analysis, tried)

        async def aclose(self):
            pass

    monkeypatch.setattr("jev_browser.autoresearch.CodexResearcher", Analyst)
    args = args_for(tmp_path)
    args.max_trials = 3
    runner, called = fake_runner([report(), report(tokens=200), report(tokens=50)])
    assert await run_research(args, runner=runner) == 0
    assert len(seen) == 2 and len(called) == 3
    analysis, history = seen[1]
    assert history[1]["selection"]["decision"] == "reject"
    assert history[1]["measurements"]["known_input_tokens"] == 200
    assert history[0]["measurements"]["end_to_end_s"] == 10
    assert any(e["trial_id"] == 1 for e in analysis["evidence"])
    record = json.loads((tmp_path / "researcher/analysis-002.json").read_text())
    assert record["status"] == "complete" and record["history"][1]["quality"]["strict_success"]
    conclusion = json.loads((tmp_path / "conclusions.json").read_text())
    assert [e["outcome"] for e in conclusion["experiments"]] == [
        "not_supported", "supported_in_this_comparison"]
    assert conclusion["incumbent"] == 2
    assert "未证明泛化" in conclusion["limitations"][0]
    assert "token regression" in (tmp_path / "research.md").read_text()


async def test_analyst_stop_explanation_survives_in_study_and_report(tmp_path, monkeypatch):
    class Analyst:
        def __init__(self, args, observer):
            self.transport = self
            self.last_advice = {"decision": "stop", "hypothesis": "Insufficient evidence for a new hypothesis."}

        async def propose(self, *args):
            return None

        async def aclose(self):
            pass

    monkeypatch.setattr("jev_browser.autoresearch.CodexResearcher", Analyst)
    runner, _ = fake_runner([report()])
    await run_research(args_for(tmp_path), runner=runner)
    study = json.loads((tmp_path / "study.json").read_text())
    assert study["last_advice"]["decision"] == "stop"
    assert "Insufficient evidence" in (tmp_path / "research.md").read_text()
