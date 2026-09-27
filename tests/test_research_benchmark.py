import copy
import json

import pytest
from test_autoresearch import args_for, report, select

from jev_browser.autoresearch import run_research
from jev_browser.observability import save_analysis, write_json
from jev_browser.research_benchmark import aggregate


def task_report(task_id, *, tokens=100, seconds=10, success=True):
    value = report(tokens=tokens, seconds=seconds, success=success)
    value["manifest"].update(task_id=task_id, task_hash=f"task-{task_id}", suite="webarena")
    return value


def test_pair_comparison_rejects_a_regressing_task_even_when_total_improves():
    baseline = aggregate([task_report(50, tokens=1000), task_report(332, tokens=100)])
    candidate = aggregate([task_report(50, tokens=200), task_report(332, tokens=200)])
    assert select(baseline, candidate)["decision"] == "reject"
    assert "332" in select(baseline, candidate)["reason"]
    candidate = aggregate([task_report(50, tokens=500), task_report(332, tokens=50)])
    assert select(baseline, candidate)["validation"] == "two_task_provisional"
    candidate["result"]["strict_success"] = False
    assert select(baseline, candidate)["decision"] == "reject"


def test_paired_analysis_preserves_task_specific_feedback_diagnostics(tmp_path):
    child = tmp_path / "webarena-332"
    child.mkdir()
    (child / "trajectory.jsonl").write_text('{"kind":"invalid_feedback"}\n')
    analysis = save_analysis(tmp_path, aggregate([task_report(50), task_report(332)]))
    issues = [f for f in analysis["findings"] if f["code"] == "schema_repair"]
    assert len(issues) == 1 and issues[0]["evidence"]["task_id"] == 332
    assert len(analysis["task_observations"]) == 2


async def test_missing_webarena_is_a_visible_zero_spend_block(tmp_path, monkeypatch):
    async def unavailable(ids):
        return {
            "status": "blocked",
            "selected_tasks": [{"id": x} for x in ids],
            "missing_environment": ["WA_SHOPPING"],
        }

    monkeypatch.setattr("jev_browser.benchmark.check_webarena", unavailable)
    args = args_for(tmp_path)
    args.suite, args.task_ids, args.seed = "webarena", [50, 332], 0
    assert await run_research(args) == 2
    state = json.loads((tmp_path / "study.json").read_text())
    assert state["status"] == "blocked" and not state["trials"]
    assert not (tmp_path / "researcher").exists()
    assert json.loads((tmp_path / "budget.json").read_text()) == {}


async def test_public_loop_runs_same_pair_before_and_after_one_codex_proposal(
    tmp_path, monkeypatch
):
    order = []

    async def ready(ids):
        return {"status": "ready", "selected_tasks": [{"id": x} for x in ids]}

    async def run_task(args, selected, output):
        output.mkdir(parents=True)
        changed = args._tuning.brain_interval != 12
        order.append((selected["id"], args._tuning.brain_interval))
        value = task_report(selected["id"], tokens=50 if changed else 100)
        value["manifest"]["tuning"] = args._tuning.model_dump()
        for call in value["model_calls"]:
            args._research_gate.before_call()
            args._research_gate.record(call)
        write_json(output / "report.json", value)
        write_json(output / "manifest.json", value["manifest"])
        return value

    class Analyst:
        def __init__(self, args, observer):
            self.transport = self
            self.observer = observer

        async def propose(self, profile, analysis, tried, history):
            order.append("analyze")
            self.observer.before_call()
            self.observer.model_call(
                {
                    "kind": "research_analysis",
                    "transport": "codex_cli",
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cost_usd": None,
                }
            )
            candidate = {**profile.model_dump(), "brain_interval": 17}
            return {
                "profile": candidate,
                "hypothesis": "test-only proposed window",
                "change": {"field": "brain_interval", "before": 12, "after": 17},
                "evidence_codes": [],
                "source": "codex",
            }

        async def aclose(self):
            pass

    monkeypatch.setattr("jev_browser.benchmark.check_webarena", ready)
    monkeypatch.setattr("jev_browser.public_benchmark.run_webarena", run_task)
    monkeypatch.setattr("jev_browser.autoresearch.CodexResearcher", Analyst)
    args = args_for(tmp_path)
    args.suite, args.task_ids, args.seed = "webarena", [50, 332], 0
    args.study_seconds = 600
    assert await run_research(args) == 0
    assert order == [(50, 12), (332, 12), "analyze", (50, 17), (332, 17)]
    state = json.loads((tmp_path / "study.json").read_text())
    assert state["incumbent"] == 1 and state["budget"]["attempts"] == 5
    assert state["trials"][1]["result"]["passed_tasks"] == 2
    assert state["config"]["brain"] == "api"
    assert state["config"]["researcher"]["backend"] == "codex_cli"


@pytest.mark.parametrize("ids", [[50], [50, 50], [50, 332, 333], [1, 2]])
async def test_public_research_only_accepts_a_reviewed_distinct_pair(tmp_path, ids):
    args = args_for(tmp_path)
    args.suite, args.task_ids = "webarena", ids
    with pytest.raises(ValueError):
        await run_research(args)


def test_restoring_one_task_cannot_break_the_already_successful_task():
    baseline = aggregate([task_report(50), task_report(332, success=False)])
    candidate = aggregate([task_report(50, seconds=30), task_report(332)])
    assert select(baseline, candidate)["decision"] == "reject"
    mismatch = copy.deepcopy(baseline)
    mismatch["task_reports"] = mismatch["task_reports"][:1]
    assert select(baseline, mismatch)["decision"] == "inconclusive"
