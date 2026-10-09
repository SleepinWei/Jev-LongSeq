import asyncio
import hashlib
import json
import threading

import pytest

from jev_browser.process_scores import ProcessScores, scoring_config
from jev_browser.saas_benchmark import grade_result


def verify(earned):
    return {"status": "PASS" if earned == 2 else "FAIL", "returncode": int(earned != 2),
            "all_pass": earned == 2, "earned": earned, "total": 2, "score": earned / 2,
            "checks": [{"label": str(i), "weight": 1, "passed": i < earned} for i in range(2)]}


@pytest.fixture
def scorer(tmp_path):
    config = {"min_interval_s": 30, "interval_s": 120, "action_interval": 20}
    instance = ProcessScores(tmp_path, "# hidden verifier", {"task_id": "business_031"},
                             lambda *args: verify(0), grade_result, 0, {}, config)
    yield instance
    instance.close()


def test_audit_requires_exact_task_and_hash(monkeypatch):
    monkeypatch.setattr("jev_browser.process_scores.BUSINESS_031_V11_UPSTREAM_SHA256",
                        hashlib.sha256(b"SELECT audited").hexdigest())
    assert scoring_config("business_031", "SELECT audited")["enabled"]
    assert not scoring_config("business_023", "SELECT audited")["enabled"]
    assert not scoring_config("business_031", "SELECT changed")["enabled"]
    assert not scoring_config("business_031", "SELECT audited", "off")["enabled"]


async def test_baseline_is_private_and_partial_rows_are_not_lost(scorer):
    row = await scorer.sample("baseline")
    scorer.publish()
    assert not list(scorer.output.iterdir())
    assert not scorer.directory.is_relative_to(scorer.output)
    log = scorer.output / "trajectory.jsonl"
    log.write_text('{"kind":"action","cycle":1')
    scorer.progress()
    assert scorer.offset == 0 and scorer.actions == 0
    with log.open("a") as stream:
        stream.write('}\nnull\n{"kind":"transition_confirmed","cycle":2,"confirmation_scope":"business_commit"}\n')
    scorer.progress()
    assert (scorer.actions, scorer.commits, scorer.cycle, scorer.invalid_events) == (1, 1, 2, 1)
    scorer.progress()
    assert scorer.actions == 1
    scorer.publish()
    assert json.loads((scorer.output / "process-scores.jsonl").read_text()) == row
    assert not list(scorer.output.glob("*verify*"))


async def test_progress_can_regress_and_invalid_scores_never_count(scorer):
    values = iter([verify(1), verify(2), {"status": "ERROR", "earned": 2}, verify(0)])
    scorer.verify = lambda *args: next(values)
    baseline = await scorer.sample("baseline")
    advance = await scorer.sample("intermediate")
    invalid = await scorer.sample("intermediate")
    regression = await scorer.sample("intermediate")
    assert baseline["delta_earned"] == 0
    assert advance["delta_earned"] == 1 and advance["newly_passed"] == ["1"]
    assert invalid["data_valid"] is False and invalid["earned"] is None
    assert invalid["delta_earned"] is None
    assert regression["delta_earned"] == -1 and regression["regressed"] == ["0", "1"]
    assert scorer.summary()["invalid_snapshots"] == 1
    assert scorer.summary()["agent_feedback"] is False


async def test_unavailable_baseline_is_not_replaced_with_later_scores(scorer):
    scorer.verify = lambda *args: {"error": "unavailable"}
    await scorer.sample("baseline")
    scorer.verify = lambda *args: verify(2)
    row = await scorer.sample("intermediate")
    assert row["data_valid"] and row["delta_earned"] is None


def test_rate_limit_applies_to_actions_and_confirmed_writes(scorer):
    scorer.actions = 20
    assert not scorer.due(29) and scorer.due(30)
    scorer.actions = 0
    assert not scorer.due(119) and scorer.due(120)
    scorer.commits = 1
    assert not scorer.due(29) and scorer.due(30)


async def test_stop_joins_inflight_grading_without_overlap(scorer):
    started, release = threading.Event(), threading.Event()
    active = []

    def slow(*args):
        active.append("start")
        started.set()
        assert release.wait(5)
        active.append("end")
        return verify(0)

    scorer.verify = slow
    scorer.last_sample = 0
    scorer.start()
    assert await asyncio.to_thread(started.wait, 5)
    stopping = asyncio.create_task(scorer.stop())
    await asyncio.sleep(0)
    assert not stopping.done() and active == ["start"]
    release.set()
    await stopping
    assert active == ["start", "end"] and scorer.worker is None


async def test_scoring_exception_is_a_visible_invalid_snapshot(scorer):
    def unavailable(*args):
        raise OSError("database offline")
    scorer.verify = unavailable
    row = await scorer.sample("baseline")
    assert not row["data_valid"] and row["error"] == "OSError: database offline"
