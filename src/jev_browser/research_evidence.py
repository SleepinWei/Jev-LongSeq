"""Bounded experiment memory and traceable conclusions; never expose oracle answers."""
from __future__ import annotations

import json
from pathlib import Path

from .observability import write_json


def measurements(analysis):
    total = analysis.get("efficiency", {}).get("total", {})
    return {**{k: total.get(k) for k in (
        "attempts", "known_input_tokens", "known_output_tokens", "unknown_usage_attempts",
        "request_time_s", "cost_usd")},
        "end_to_end_s": analysis.get("efficiency", {}).get("end_to_end_s")}


def trial_evidence(trial, analysis):
    """Allowlisted summaries reference the saved analysis, not raw pages/graders."""
    artifact = f"{trial['directory']}/observability.json"
    evidence = []
    scopes = [("study", "", analysis)] + [
        (f"task-{task['task_id']}", f"/task_observations/{i}", task)
        for i, task in enumerate(analysis.get("task_observations", []))
    ]
    for scope, pointer, observation in scopes:
        for code, value, suffix in (
            ("quality", observation.get("quality", {}), "/quality"),
            ("efficiency", observation.get("efficiency", {}).get("total", {}), "/efficiency/total"),
            ("latency", observation.get("efficiency", {}).get("end_to_end_s"), "/efficiency/end_to_end_s"),
        ):
            evidence.append({"id": f"t{trial['id']}/{scope}/{code}", "code": code,
                             "trial_id": trial["id"], "scope": scope, "value": value,
                             "artifact": artifact, "pointer": pointer + suffix})
        for index, finding in enumerate(observation.get("findings", [])):
            # Per-task findings are also copied into the aggregate; avoid duplicates.
            if scope == "study" and "task_id" in finding.get("evidence", {}):
                continue
            evidence.append({"id": f"t{trial['id']}/{scope}/finding-{index}",
                             "code": finding["code"], "trial_id": trial["id"], "scope": scope,
                             "value": finding.get("evidence", {}),
                             "hypothesis": finding.get("hypothesis"),
                             "artifact": artifact, "pointer": pointer + f"/findings/{index}"})
    return evidence


def experiment_memory(trials, analyses, incumbent):
    complete = [t for t in trials if t["status"] == "complete" and t["id"] in analyses]
    # Full compact outcome history, bounded detailed evidence from baseline/incumbent/recent trials.
    detailed = {t["id"] for t in complete[-3:]} | {0, incumbent}
    history, evidence = [], []
    for trial in complete:
        analysis = analyses[trial["id"]]
        refs = trial_evidence(trial, analysis)
        history.append({"id": trial["id"], "profile": trial["profile"],
                        "proposal": trial.get("proposal"), "selection": trial.get("selection"),
                        "quality": analysis.get("quality", {}),
                        "measurements": measurements(analysis),
                        "findings": sorted({f["code"] for f in analysis.get("findings", [])}),
                        "evidence_ids": [r["id"] for r in refs] if trial["id"] in detailed else []})
        if trial["id"] in detailed:
            evidence.extend(refs)
    return history, evidence


def conclusions(state, analyses):
    trials = state.get("trials", [])
    history, evidence = experiment_memory(trials, analyses, state.get("incumbent"))
    evidence = [ref for t in trials if t["id"] in analyses
                for ref in trial_evidence(t, analyses[t["id"]])]
    by_id = {r["id"]: r for r in history}
    experiments = []
    for trial in trials:
        if not trial.get("proposal"):
            continue
        selection = trial.get("selection", {})
        decision = selection.get("decision")
        outcome = ("supported_in_this_comparison" if decision == "keep" else
                   "not_supported" if decision == "reject" else "unresolved")
        experiments.append({"trial_id": trial["id"], "hypothesis": trial["proposal"]["hypothesis"],
                            "change": trial["proposal"]["change"], "outcome": outcome,
                            "selection": selection,
                            "evidence_refs": trial["proposal"].get("evidence_refs", []),
                            "result_evidence": [ref["id"] for ref in evidence
                                                if ref["trial_id"] == trial["id"]]})
    best = state.get("incumbent")
    reason = state.get("stop_reason")
    if state.get("status") == "blocked":
        next_step = "环境预检尚未通过，本次未派发新任务或模型请求；先处理 preflight.json 中的阻塞项。"
    elif reason in {"transport_instability", "public_data_unavailable", "researcher_failed"}:
        next_step = "先修复服务或数据可用性，再在相同条件下另做对照；当前失败不能支持调参收益。"
    elif not state.get("best_validated"):
        next_step = "优先恢复严格完成能力，按失败任务与证据定位问题，再测试单项变更。"
    elif any(e["selection"].get("decision") == "keep" for e in experiments):
        next_step = "在相同任务上重复成对验证，再增加未参与调参的任务；当前保留结果仍是小样本观察。"
    else:
        next_step = "保留已验证基线，利用未支持的假设与逐任务诊断设计下一项不同的实验。"
    config = state.get("config", {})
    protocol = {k: config.get(k) for k in (
        "suite", "task_ids", "metric", "min_improvement", "max_latency_regression",
        "max_token_regression", "code_hash", "models", "researcher")}
    task_results = [{"trial_id": trial["id"], "task_id": task.get("task_id", "study"),
                     "quality": task.get("quality", {}), "measurements": measurements(task)}
                    for trial in trials if trial["id"] in analyses
                    for task in (analyses[trial["id"]].get("task_observations")
                                 or [analyses[trial["id"]]])]
    return {"version": 1, "stop_reason": reason, "best_validated": state.get("best_validated", False),
            "incumbent": best, "completed_trials": len(history),
            "protocol": protocol, "task_results": task_results,
            "incumbent_observation": by_id.get(best), "experiments": experiments,
            "evidence": evidence, "budget": state.get("budget", {}),
            "analyst_conclusion": state.get("last_advice"), "next_step": next_step,
            "limitations": ["单次、小规模配置对照；未证明泛化能力或统计显著性。",
                            "诊断与提案是因果假设，保留与拒绝只依据实际判分和对照指标。",
                            "未知 usage 不记作零；研究分析开销与任务执行指标分开。"]}


def save_conclusions(root: Path, state, analyses):
    value = conclusions(state, analyses)
    write_json(root / "conclusions.json", value)

    def cell(text):
        return str(text).replace("|", "\\|").replace("\n", " ")

    lines = ["# Autoresearch 研究结论", "",
             f"停止原因：`{value['stop_reason']}`。严格验证的保留配置：`{value['best_validated']}`。",
             f"已完成 {value['completed_trials']} 轮；当前保留 trial：`{value['incumbent']}`。", "",
             "## 假设与实验结果", "",
             "| Trial | 单项变更 | 假设 | 选择 | 实际依据 |",
             "|---|---|---|---|---|"]
    for experiment in value["experiments"]:
        selection = experiment["selection"]
        change = experiment["change"]
        lines.append(f"| {experiment['trial_id']} | {cell(change['field'])}: "
                     f"{cell(change['before'])} → {cell(change['after'])} | "
                     f"{cell(experiment['hypothesis'])} | {cell(selection.get('decision', '未完成'))} | "
                     f"{cell(selection.get('reason', '尚无有效对照'))} |")
    if not value["experiments"]:
        lines.extend(["", "尚未完成候选实验，不能推导优化收益。"])
    lines.extend(["", "## 逐任务测量", "",
                  "| Trial | 任务 | 严格完成 | 已知 tokens | 未知用量次数 | 端到端秒数 |",
                  "|---|---|---|---:|---:|---:|"])
    for task in value["task_results"]:
        m = task["measurements"]
        known = (m.get("known_input_tokens") or 0) + (m.get("known_output_tokens") or 0)
        lines.append(f"| {task['trial_id']} | {cell(task['task_id'])} | "
                     f"{task['quality'].get('strict_success')} | {known} | "
                     f"{m.get('unknown_usage_attempts')} | {m.get('end_to_end_s')} |")
    lines.extend(["", "## 对照条件", "", "```json",
                  json.dumps(value["protocol"], ensure_ascii=False, indent=2), "```"])
    advice = value["analyst_conclusion"]
    if advice:
        lines.extend(["", "## 分析器最后结论", "", advice.get("hypothesis", "")])
    lines.extend(["", "## 可追溯证据", ""])
    for ref in value["evidence"]:
        lines.append(f"- `{ref['id']}` — [{ref['artifact']}]({ref['artifact']}) "
                     f"`{ref['pointer']}`：`{json.dumps(ref['value'], ensure_ascii=False)}`")
    budget = value["budget"]
    lines.extend(["", "## 全研究预算", "",
                  f"派发 {budget.get('attempts', 0)} 次；已知 tokens {budget.get('known_tokens', 0)}；"
                  f"未知用量 {budget.get('unknown_usage_attempts', 0)} 次。",
                  "", "## 下一步", "", value["next_step"], "", "## 结论边界", "",
                  *[f"- {line}" for line in value["limitations"]], ""])
    (root / "research.md").write_text("\n".join(lines))
    return value
