"""Read-only research projections and a bounded local study launcher."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .evaluation import efficiency_profile
from .observability import read_jsonl
from .protocol import digest
from .research_evidence import conclusions


def frozen_source(root):
    """Pin one source version for every child task in a study, despite workspace edits."""
    package = Path(__file__).parent
    source = {p.name: p.read_text() for p in sorted(package.glob("*.py"))}
    if source != {p.name: p.read_text() for p in sorted(package.glob("*.py"))}:
        raise RuntimeError("源码正在变化，请等待修改完成后启动")
    fingerprint = digest({name: digest(value) for name, value in source.items()})
    destination = root / ".code-snapshots" / fingerprint
    target = destination / "jev_browser"
    target.mkdir(parents=True, exist_ok=True)
    for name, value in source.items():
        path = target / name
        if path.exists() and path.read_text() != value:
            raise RuntimeError("研究源码快照校验失败")
        if not path.exists():
            path.write_text(value)
    return destination


def read(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def studies(store):
    rows = []
    for file in store.root.rglob("study.json"):
        if not file.resolve().is_relative_to(store.root):
            continue
        value = read(file, {})
        if "trials" not in value:
            continue
        rows.append(
            {
                "id": file.parent.relative_to(store.root).as_posix(),
                "status": value.get("status"),
                "phase": value.get("phase"),
                "started_at": value.get("started_at"),
                "trials": len(value["trials"]),
                "suite": value.get("config", {}).get("suite", "catalog"),
            }
        )
    return sorted(rows, key=lambda row: row.get("started_at") or "", reverse=True)


def study_path(store, study_id):
    if not isinstance(study_id, str) or study_id not in {r["id"] for r in studies(store)}:
        raise ValueError("研究记录不存在")
    path = (store.root / study_id).resolve()
    if not path.is_relative_to(store.root):
        raise ValueError("无效研究路径")
    return path


def study_data(store, study_id):
    root = study_path(store, study_id)
    state = read(store.file(root, "study.json"), {})
    result = {
        **state,
        "id": study_id,
        "budget": read(store.file(root, "budget.json"), {}),
        "trials": [],
    }
    result["process_alive"] = False
    if state.get("status") == "running" and type(state.get("pid")) is int and state["pid"] > 0:
        try:
            os.kill(state["pid"], 0)
            result["process_alive"] = True
        except (ProcessLookupError, PermissionError):
            pass
    for trial in state.get("trials", []):
        directory = store.file(root, trial["directory"])
        report = read(store.file(directory, "report.json"), {})
        progress = read(store.file(directory, "progress.json"), {})
        tasks = []
        for path in sorted(directory.glob("*/manifest.json")):
            if not path.resolve().is_relative_to(root):
                continue
            task_root = path.parent
            task_report = read(store.file(task_root, "report.json"), {})
            manifest = read(path, {})
            calls = task_report.get("model_calls") or read_jsonl(
                store.file(task_root, "model-calls.jsonl")
            )
            starts = read_jsonl(store.file(task_root, "model-request-starts.jsonl"))
            completed = {c.get("attempt_id") for c in calls}
            tasks.append(
                {
                    "id": manifest.get("task_id"),
                    "run_id": task_root.relative_to(store.root).as_posix(),
                    "result": task_report.get("result"),
                    "elapsed_s": task_report.get("end_to_end_s"),
                    "metrics": efficiency_profile(
                        calls,
                        actions=task_report.get("result", {}).get("actions", 0),
                        elapsed_s=task_report.get("end_to_end_s", 0),
                    )["total"],
                    "components": efficiency_profile(calls, actions=0, elapsed_s=0)["by_component"],
                    "grade": task_report.get("grade", {}),
                    "environment": task_report.get("environment", {}),
                    "inflight": len([r for r in starts if r.get("attempt_id") not in completed]),
                    "has_preview": store.file(task_root, "live.jpg").exists(),
                }
            )
        result["trials"].append(
            {
                **trial,
                "progress": progress,
                "tasks": tasks,
                "metrics": report.get("efficiency", {}).get("total"),
                "elapsed_s": report.get("end_to_end_s"),
                "findings": read(store.file(directory, "observability.json"), {}).get(
                    "findings", []
                ),
            }
        )
    calls = read_jsonl(store.file(root, "researcher/model-calls.jsonl"))
    result["research_usage"] = efficiency_profile(calls, actions=0, elapsed_s=0)["total"]
    result["preflight"] = read(store.file(root, "preflight.json"), state.get("preflight"))
    result["conclusions"] = read(store.file(root, "conclusions.json"))
    if not result["conclusions"]:
        analyses = {t["id"]: read(store.file(root, f"{t['directory']}/observability.json"), {})
                    for t in state.get("trials", []) if t["status"] == "complete"}
        result["conclusions"] = conclusions({**state, "budget": result["budget"]}, analyses)
    return result


def launch_research(store, body):
    if not isinstance(body, dict):
        raise ValueError("请求必须是 JSON 对象")
    mode = body.get("mode", "new")
    if mode not in {"new", "resume"}:
        raise ValueError("未知研究操作")
    max_trials = body.get("max_trials", 2)
    metric = body.get("metric", "tokens")
    if type(max_trials) is not int or not 2 <= max_trials <= 6:
        raise ValueError("研究轮数须为 2–6（包含基线）")
    if not isinstance(metric, str) or metric not in {"tokens", "latency"}:
        raise ValueError("请选择 tokens 或 latency 研究目标")
    suite = body.get("suite", "public-web")
    if not isinstance(suite, str) or suite not in {"public-web", "saas-bench"}:
        raise ValueError("请选择 public-web 或 saas-bench")
    with store.lock:
        if store.child and store.child.poll() is None:
            raise RuntimeError("已有任务正在运行，请等待完成")
        for entry in studies(store):
            if entry["status"] == "running" and study_data(store, entry["id"])["process_alive"]:
                raise RuntimeError("已有研究正在运行，请等待完成")
        if mode == "resume":
            output = study_path(store, body.get("id"))
            prior = read(output / "study.json", {})
            if prior.get("trials"):
                raise ValueError("有已执行轮次的研究请用原 CLI 参数恢复，避免改变对照条件")
            if prior.get("status") == "running" and study_data(store, body["id"])["process_alive"]:
                raise RuntimeError("该研究正在运行")
            study_id = body["id"]
        else:
            study_id = f"autoresearch-{suite}-{time.time_ns()}"
            output = store.root / study_id
        suite = prior.get("config", {}).get("suite", "webarena") if mode == "resume" else suite
        if mode == "resume":
            max_trials = prior.get("limits", {}).get("max_trials", 2)
            metric = prior.get("config", {}).get("metric", "tokens")
        study_seconds = (prior.get("limits", {}).get("study_seconds", 600 * max_trials + 300)
                         if mode == "resume" else 600 * max_trials + 300)
        if suite not in {"webarena", "public-web", "saas-bench"}:
            raise ValueError("该研究类型请通过 CLI 恢复")
        command = [
            sys.executable,
            "-m",
            "jev_browser",
            "autoresearch",
            "--suite",
            suite,
            "--task-ids",
            "50",
            "332",
            "--brain",
            "api",
            "--codex-model",
            "gpt-6-astra",
            "--codex-effort",
            "high",
            "--max-trials",
            str(max_trials),
            "--metric",
            metric,
            "--max-seconds",
            "300",
            "--study-seconds",
            str(study_seconds),
            "--max-actions",
            "150",
            "--max-model-attempts",
            "300",
            "--max-tokens",
            "600000",
            "--live-preview",
            "--output",
            str(output),
        ]
        if suite == "saas-bench":
            from .saas_benchmark import DEFAULT_TASKS

            ids = prior.get("config", {}).get("task_ids", DEFAULT_TASKS) if mode == "resume" else DEFAULT_TASKS
            command.extend(["--saas-task-ids", *ids])
            if mode == "resume" and prior.get("config", {}).get("saas_root"):
                command.extend(["--saas-root", prior["config"]["saas_root"],
                                "--saas-slot", str(prior["config"].get("saas_slot", 0))])
        if mode == "resume":
            command.append("--resume")
        store.root.mkdir(parents=True, exist_ok=True)
        source = frozen_source(store.root)
        environment = {**os.environ, "PYTHONPATH": str(source)}
        with (store.root / f"{output.name}.console.log").open("a") as stream:
            store.child = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT,
                                           env=environment)
        store.child_id = study_id
        return {"id": study_id}
