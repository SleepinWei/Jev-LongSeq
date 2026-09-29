import json

from jev_browser.research_evidence import conclusions, experiment_memory, trial_evidence


def test_task_evidence_has_exact_artifact_locations_without_oracle_answers():
    trial = {"id": 0, "directory": "trial-000", "status": "complete", "profile": {}}
    task = {"task_id": 50, "quality": {"strict_success": False},
            "efficiency": {"total": {"unknown_usage_attempts": 1}, "end_to_end_s": 12},
            "findings": [{"code": "task_incomplete", "evidence": {"actions": 12}}],
            "oracle": "PRIVATE_ORACLE_ANSWER", "page_text": "RAW_PAGE"}
    analysis = {"quality": {"strict_success": False}, "task_observations": [task]}
    refs = trial_evidence(trial, analysis)
    ref = next(r for r in refs if r["code"] == "task_incomplete")
    assert ref["artifact"] == "trial-000/observability.json"
    assert ref["pointer"] == "/task_observations/0/findings/0"
    assert ref["scope"] == "task-50"
    assert "PRIVATE_ORACLE_ANSWER" not in json.dumps(refs) and "RAW_PAGE" not in json.dumps(refs)
    latency = next(r for r in refs if r["scope"] == "task-50" and r["code"] == "latency")
    assert latency["value"] == 12


def test_prompt_evidence_is_bounded_but_final_report_keeps_all_trials():
    trials = [{"id": i, "directory": f"trial-{i:03}", "status": "complete", "profile": {}}
              for i in range(20)]
    analyses = {i: {"quality": {}, "findings": []} for i in range(20)}
    history, evidence = experiment_memory(trials, analyses, 4)
    assert len(history) == 20
    assert {r["trial_id"] for r in evidence} == {0, 4, 17, 18, 19}
    report = conclusions({"trials": trials, "incumbent": 4}, analyses)
    assert {r["trial_id"] for r in report["evidence"]} == set(range(20))
