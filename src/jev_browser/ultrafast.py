"""Studio adapter for the original, unmodified jev_ultrafast.Agent loop.

Only process supervision and artifact recording live here; no LongSeq planner or
controller participates in baseline decisions. The upstream checkout is optional.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from .start_page import prompt_url, validate_url

TERMINAL = {"done", "blocked", "needs_input", "noop", "error", "timeout", "interrupted"}


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False))
    temporary.replace(path)


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def source_root():
    configured = os.environ.get("JEV_ULTRAFAST_ROOT")
    return (Path(configured).expanduser() if configured else
            Path(__file__).resolve().parents[3] / "browseruse" / "jev-ultrafast").resolve()


def configuration():
    root = source_root()
    available = all((root / "jev_ultrafast" / name).is_file()
                    for name in ("agent.py", "browser.py", "model.py", "questions.py", "snapshot.js"))
    configured = bool(os.environ.get("TYPESAFE_API_KEY") and os.environ.get("TEXT_MODEL_API_KEY"))
    return {"available": available, "configured": configured,
            "source": str(root), "model": os.environ.get("TYPESAFE_MODEL", "jev-latest"),
            "text_model": os.environ.get("TEXT_MODEL", "deepseek-chat"),
            "message": ("" if available else "请设置 JEV_ULTRAFAST_ROOT 指向原始源码目录。") +
                       ("" if configured else "请通过 --env-file 配置 TYPESAFE_API_KEY 和 TEXT_MODEL_API_KEY。")}


def runtime(root):
    configured = os.environ.get("JEV_ULTRAFAST_PYTHON")
    if configured:
        return str(Path(configured).expanduser())
    upstream = root / ".venv" / "bin" / "python"
    return str(upstream) if upstream.is_file() else sys.executable


def run_path(store, run_id):
    root = (store.root / "ultrafast").resolve()
    path = (root / run_id).resolve()
    if root.parent != store.root or path.parent != root or not (path / "meta.json").is_file():
        raise ValueError("Ultrafast 运行不存在")
    return path


def listing(store):
    root = store.root / "ultrafast"
    if root.resolve().parent != store.root:
        return []
    rows = []
    for p in root.glob("*/meta.json"):
        if p.parent.is_symlink():
            continue
        path = run_path(store, p.parent.name)
        meta = read_json(store.file(path, "meta.json"))
        benchmark = benchmark_data(store, meta, p.parent.name)
        rows.append({**meta, **({"benchmark": {k: benchmark[k] for k in
                      ("suite", "task_id", "status", "strict_success", "earned", "total")}}
                      if benchmark else {})})
    return sorted(rows,
                  key=lambda m: m.get("started_at", ""), reverse=True)


def benchmark_data(store, meta, run_id):
    """Project a bounded SaaS score; never follow a trace's arbitrary path."""
    raw = meta.get("benchmark_output")
    if not isinstance(raw, str):
        return None
    output = Path(raw).resolve()
    if (not output.is_relative_to(store.root)
            or not re.fullmatch(r"saas-bench-([a-z]+_[0-9]+)", output.name)):
        return None
    task_id = output.name.removeprefix("saas-bench-")
    pending = {"suite": "saas-bench", "task_id": task_id, "status": "pending",
               "strict_success": None, "score": None, "earned": None, "total": None,
               "checks": [], "verifier_errors": [], "agent_status": None, "reason": ""}
    report = read_json(store.file(output, "report.json"))
    if not report:
        return pending
    manifest = report.get("manifest", {})
    if (manifest.get("suite") != "saas-bench" or manifest.get("task_id") != task_id
            or manifest.get("original_trace_id") != run_id):
        return None
    result, grade = report.get("result", {}), report.get("grade", {})
    valid = grade.get("data_valid") is True
    return {**pending, "status": "graded" if valid else "invalid",
            "strict_success": result.get("strict_success") if valid else None,
            "score": grade.get("score"), "earned": grade.get("earned"),
            "total": grade.get("total"), "checks": grade.get("checks", []),
            "verifier_errors": grade.get("verifier_errors", []),
            "agent_status": result.get("status"), "reason": result.get("reason", "")}


def data(store, run_id):
    path = run_path(store, run_id)
    meta = read_json(store.file(path, "meta.json"))
    state = read_json(store.file(path, "state.json"))
    launcher = store.launch_status()
    active = launcher["running"] and launcher["id"] == "ultrafast/" + run_id
    connection_message = ""
    if active and state.get("status") == "starting":
        log = store.file(path, "console.log")
        if log.exists() and 'Allow remote debugging?' in log.read_text():
            connection_message = "等待 Chrome 授权：请在浏览器的“Allow remote debugging?”弹窗中选择是否允许。"
    if not active and state.get("status") not in TERMINAL:
        state.update(status="interrupted", error="运行进程已结束，保留最后记录。")
    benchmark = benchmark_data(store, meta, run_id)
    if benchmark:
        benchmark["agent_status"] = state.get("status")
    return {"meta": meta, "state": state, "active": active,
            "timeline": timeline_data(store, path, state),
            "benchmark": benchmark,
            "connection_message": connection_message}


def timeline_data(store, path, state):
    """Small replay index; screenshots remain tied to saved decision observations."""
    decisions, history = state.get("decisions", []), state.get("history", [])
    entries = []
    for index, decision in enumerate(decisions):
        saved = store.file(path, f"decisions/{index}.json")
        context = read_json(saved) if saved.exists() else {}
        turn = decision.get("turn", 1)
        start = decision.get("elapsed_ms")
        following = next((d for d in decisions[index + 1:] if d.get("turn", 1) == turn), {})
        end = following.get("elapsed_ms", float("inf"))
        # A prediction may never execute. Do not zip decisions and actions: retries,
        # NONE and DONE would shift every subsequent action onto the wrong frame.
        matches = [h for h in history if start is not None
                   and h.get("turn", 1) == turn and h.get("choice") == decision.get("choice")
                   and start <= h.get("executed_ms", -1) < end]
        action = matches[0] if len(matches) == 1 else None
        page = context.get("page", {})
        entries.append({"index": index, "frame": context.get("frame"),
                        "title": page.get("title"), "url": page.get("url"),
                        "action": action})
    return entries


def stop(store, body):
    if not isinstance(body, dict) or not isinstance(body.get("id"), str):
        raise ValueError("请指定要停止的 Ultrafast 运行")
    with store.lock:
        if store.child_id != "ultrafast/" + body["id"]:
            raise ValueError("该任务不是当前运行的 Ultrafast 任务")
        if store.child and store.child.poll() is None:
            store.child.terminate()
    return {"id": body["id"], "stopping": True}


def decision_data(store, run_id, index):
    """Pair a decision with its observed DOM, never the subsequent page."""
    path = run_path(store, run_id)
    if not re.fullmatch(r"[0-9]{1,6}", str(index)):
        raise ValueError("无效 decision 编号")
    index = int(index)
    saved = store.file(path, f"decisions/{index}.json")
    if saved.exists():
        return read_json(saved)
    latest = read_json(store.file(path, "state.json"))
    decisions = latest.get("decisions", [])
    if index >= len(decisions):
        raise ValueError("Decision 暂不可用")
    decision = decisions[index]
    fingerprint = decision.get("fingerprint")
    # Legacy runs have DOM snapshots, but only their final screenshot survived.
    match = None
    snapshots = store.file(path, "snapshots.jsonl")
    if fingerprint and snapshots.exists():
        with snapshots.open() as stream:
            for line in stream:
                try:
                    snapshot = json.loads(line)
                except ValueError:
                    continue
                if snapshot.get("page", {}).get("fingerprint") == fingerprint:
                    match = snapshot
    if match:
        same_page = latest.get("page", {}).get("fingerprint") == fingerprint
        return {"decision": decision, "page": match["page"],
                "elements": match.get("elements", []), "overlays": match.get("overlays", []),
                "frame": match.get("frame") or ("latest" if same_page else None),
                "context_available": True, "legacy": True}
    request_state = decision.get("request", {}).get("state", {})
    if request_state.get("elements"):
        return {"decision": decision, "page": request_state.get("page", {}),
                "elements": request_state["elements"], "overlays": [], "frame": None,
                "context_available": True, "legacy": True}
    return {"decision": decision, "context_available": False, "legacy": True}


def image_path(store, run_id, frame):
    path = run_path(store, run_id)
    if frame == "latest":
        return store.file(path, "latest.jpg")
    if not re.fullmatch(r"frames/[0-9]+\.jpg", frame):
        raise ValueError("无效截图编号")
    return store.file(path, frame)


def launch(store, body):
    if not isinstance(body, dict):
        raise ValueError("请求必须是 JSON 对象")
    goal = body.get("prompt")
    if not isinstance(goal, str) or not 1 <= len(goal.strip()) <= 8000:
        raise ValueError("请输入 1–8000 字的 prompt")
    supplied = body.get("url", "")
    if not isinstance(supplied, str):
        raise ValueError("请输入有效的起始网址")
    url = validate_url(supplied.strip()) if supplied.strip() else (
        prompt_url(goal) or "https://www.google.com/")
    config = configuration()
    if not config["available"] or not config["configured"]:
        raise ValueError(config["message"])
    root = source_root()
    digest = hashlib.sha256()
    for name in ("agent.py", "browser.py", "model.py", "questions.py", "snapshot.js"):
        digest.update((root / "jev_ultrafast" / name).read_bytes())
    with store.lock:
        if store.child and store.child.poll() is None:
            raise RuntimeError("已有任务正在运行，请等待完成后再对比")
        run_id = f"original-{time.time_ns()}"
        output = store.root / "ultrafast" / run_id
        if output.parent.resolve().parent != store.root:
            raise ValueError("无效运行目录")
        output.mkdir(parents=True)
        meta = {"id": run_id, "prompt": goal.strip(), "url": url,
                "started_at": datetime.now(UTC).isoformat(), "source": str(root),
                "source_sha256": digest.hexdigest(), "model": config["model"],
                "text_model": config["text_model"], "max_seconds": 300,
                "framework": "jev-ultrafast-original", "screenshots": "每轮观察截图",
                "runtime": runtime(root)}
        write_json(output / "meta.json", meta)
        write_json(output / "state.json", {"status": "starting"})
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(root), str(Path(__file__).resolve().parents[1]),
                                             env.get("PYTHONPATH", "")])
        with (output / "console.log").open("w") as stream:
            child = subprocess.Popen([runtime(root), "-m", "jev_browser.ultrafast",
                                      "--output", str(output)], env=env, stdout=stream,
                                     stderr=subprocess.STDOUT)
        store.child, store.child_id = child, "ultrafast/" + run_id
        threading.Thread(target=supervise, args=(child, output), daemon=True).start()
        return {"id": run_id}


def supervise(child, output):
    try:
        child.wait(timeout=300)
    except subprocess.TimeoutExpired:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        state = read_json(output / "state.json")
        state.update(status="timeout", error="达到 300 秒总时限；保留最后记录。")
        write_json(output / "state.json", state)


def record(output, snapshot, started):
    state = {**snapshot, "page": dict(snapshot.get("page") or {})}
    screenshot = state["page"].pop("screenshot", None)
    if screenshot:
        frames = output / "frames"
        frames.mkdir(exist_ok=True)
        state["frame"] = f"frames/{time.time_ns()}.jpg"
        (output / state["frame"]).write_bytes(base64.b64decode(screenshot))
        image = output / "latest.tmp"
        image.write_bytes(base64.b64decode(screenshot))
        image.replace(output / "latest.jpg")
    state.update(updated_at=time.time(), wall_elapsed_s=round(time.monotonic() - started, 3))
    decision = state.get("decision")
    decisions = state.get("decisions", [])
    if (decision and decisions and state["page"].get("fingerprint")
            and decisions[-1].get("fingerprint") == state["page"]["fingerprint"]):
        folder = output / "decisions"
        folder.mkdir(exist_ok=True)
        index = len(state.get("decisions", [])) - 1
        write_json(folder / f"{index}.json", {
            "decision": state["decisions"][index], "page": state["page"],
            "elements": state.get("elements", []), "overlays": state.get("overlays", []),
            "frame": state.get("frame"), "context_available": True,
        })
    with (output / "snapshots.jsonl").open("a") as stream:
        stream.write(json.dumps(state, ensure_ascii=False) + "\n")
    write_json(output / "state.json", state)


def execute(output, agent_class=None):
    meta = read_json(output / "meta.json")
    started, agent = time.monotonic(), None
    try:
        if agent_class is None:
            from jev_ultrafast.agent import Agent

            agent_class = Agent
        class RecordingAgent(agent_class):
            def command(self, name, body=None):
                snapshot = super().command(name, body)
                if name == "predict":
                    record(output, snapshot, started)
                return snapshot

        agent = RecordingAgent(meta["url"], meta["prompt"], screenshots=True)
        record(output, agent.snapshot(), started)
        for snapshot in agent.run():
            record(output, snapshot, started)
        return 0
    except Exception as exc:
        state = agent.snapshot() if agent else {}
        # API exception text may contain response bodies; keep credentials server-side.
        state.update(status="interrupted" if isinstance(exc, InterruptedError) else "error",
                     error="任务已停止。" if isinstance(exc, InterruptedError) else
                     f"{type(exc).__name__}：原始 Agent 运行失败，请检查模型配置或 Chrome 连接。")
        record(output, state, started)
        return 1
    finally:
        if agent:
            agent.close()


if __name__ == "__main__":
    def interrupt(_signal, _frame):
        raise InterruptedError("Studio stopped this run")

    signal.signal(signal.SIGTERM, interrupt)
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    sys.exit(execute(parser.parse_args().output))
