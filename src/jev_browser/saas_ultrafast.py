"""Supervise the original Ultrafast loop; instrumentation never chooses actions."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import signal
import sys
import time
from pathlib import Path

from . import ultrafast
from .evaluation import efficiency_profile
from .observability import read_jsonl, write_json
from .protocol import now


class MeteredClient:
    """One durable start/result per HTTP attempt, including original retry logic."""

    def __init__(self, client, output):
        self.client, self.output = client, output

    def post(self, url, **kwargs):
        body = kwargs.get("json", {})
        row = {"id": str(time.time_ns()), "started_at": now(), "transport": "http",
               "kind": "jev" if "questions" in body else "dynamic_input",
               "model": body.get("model"), "status": 0, "cost_usd": None,
               "input_tokens": None, "output_tokens": None}
        self.append("model-request-starts.jsonl", row)
        started = time.monotonic()
        try:
            response = self.client.post(url, **kwargs)
            row["status"] = response.status_code
            try:
                data = response.json()
                usage = data.get("usage", {}) if isinstance(data, dict) else {}
                row["input_tokens"] = usage.get("input_tokens", usage.get("prompt_tokens"))
                row["output_tokens"] = usage.get("output_tokens", usage.get("completion_tokens"))
            except (ValueError, TypeError, AttributeError):
                pass
            return response
        except BaseException as exc:
            row["error"] = type(exc).__name__  # never persist keys, headers or error bodies
            raise
        finally:
            row["latency_s"] = time.monotonic() - started
            self.append("model-calls.jsonl", row)

    def append(self, name, row):
        with (self.output / name).open("a") as stream:
            stream.write(json.dumps(row) + "\n")


def ledger(output):
    calls = read_jsonl(output / "model-calls.jsonl")
    finished = {c["id"] for c in calls}
    # A forcibly killed request still counts, with explicitly unknown usage.
    calls.extend({**r, "error": "InterruptedRequest", "latency_s": 0}
                 for r in read_jsonl(output / "model-request-starts.jsonl") if r["id"] not in finished)
    return calls


async def stop_child(child):
    if child.returncode is None:
        child.terminate()
        try:
            await asyncio.wait_for(child.wait(), timeout=10)
        except TimeoutError:
            child.kill()
            await child.wait()


async def run_original(args, task, manifest, output):
    from .saas_benchmark import empty_report

    root = ultrafast.source_root()
    config = ultrafast.configuration()
    source_hash = hashlib.sha256()
    for name in ("agent.py", "browser.py", "model.py", "questions.py", "snapshot.js"):
        source_hash.update((root / "jev_ultrafast" / name).read_bytes())
    run_id = f"saas-{task.id}-{time.time_ns()}"
    history_context = bool(getattr(args, "saas_history_context", False))
    trace = output.parent.parent / "ultrafast" / run_id
    trace.mkdir(parents=True, exist_ok=False)
    meta = {"id": run_id, "prompt": task.objective, "url": task.start_url,
            "started_at": now(), "source": str(root), "source_sha256": source_hash.hexdigest(),
            "model": config["model"], "text_model": config["text_model"],
            "max_seconds": args.max_seconds,
            "framework": "jev-ultrafast-history-context-v1" if history_context else "jev-ultrafast-original",
            "history_context": history_context,
            "runtime": ultrafast.runtime(root), "benchmark_output": str(output.resolve()),
            "capability_note": "Original single-tab agent; no URL-open or tab-switch action"}
    if history_context:
        meta["history_context_sha256"] = hashlib.sha256(
            Path(__file__).with_name("ultrafast_history.py").read_bytes()).hexdigest()
    write_json(trace / "meta.json", meta)
    write_json(trace / "state.json", {"status": "starting"})
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(root), str(Path(__file__).resolve().parents[1]),
                                        env.get("PYTHONPATH", "")])
    started = time.monotonic()
    timed_out = False
    with (trace / "console.log").open("w") as stream:
        child = await asyncio.create_subprocess_exec(
            ultrafast.runtime(root), "-m", "jev_browser.saas_ultrafast", "--output", str(trace.resolve()),
            env=env, stdout=stream, stderr=stream)
        try:
            await asyncio.wait_for(child.wait(), timeout=args.max_seconds)
        except TimeoutError:
            timed_out = True
        finally:
            await stop_child(child)  # child must stop before upstream grading/cleanup
    elapsed = time.monotonic() - started
    state = ultrafast.read_json(trace / "state.json")
    if timed_out or state.get("status") not in ultrafast.TERMINAL:
        state.update(status="timeout" if timed_out else "error")
        write_json(trace / "state.json", state)
    calls = ledger(trace)
    manifest = {**manifest, "framework": meta["framework"], "source_sha256": meta["source_sha256"],
                "model": meta["model"], "text_model": meta["text_model"], "original_trace_id": run_id,
                "original_trace_path": str(trace.resolve()), "max_seconds": args.max_seconds,
                "capability_note": meta["capability_note"],
                "native_limits": ultrafast.read_json(trace / "native-limits.json")}
    manifest.update(history_context=history_context,
                    history_context_sha256=meta.get("history_context_sha256"))
    report = empty_report(task, manifest, meta["framework"], f"Agent: {state.get('status')}")
    result = report["result"]
    done = state.get("status") == "done" and child.returncode == 0
    result.update(status="success" if done else "failed", actions=len(state.get("history", [])),
                  cycles=len(state.get("decisions", [])), elapsed_s=elapsed, finish_requests=int(done))
    report.update(model_calls=calls, end_to_end_s=elapsed, original_status=state.get("status"),
                  original_returncode=child.returncode,
                  efficiency=efficiency_profile(calls, actions=result["actions"], elapsed_s=elapsed))
    write_json(output / "original-state.json", state)
    write_json(output / "task.json", task.model_dump())
    write_json(output / "original-trace.json", {"id": run_id, "path": str(trace.resolve())})
    with (output / "model-calls.jsonl").open("w") as stream:
        for row in calls:
            stream.write(json.dumps(row) + "\n")
    return report


def execute(output):
    from jev_ultrafast import model, questions

    def interrupt(_signal, _frame):
        raise InterruptedError("Benchmark stopped this run")

    signal.signal(signal.SIGTERM, interrupt)
    write_json(output / "native-limits.json", {"max_actions": questions.MAX_STEPS})
    model.CLIENT = MeteredClient(model.CLIENT, output)
    if ultrafast.read_json(output / "meta.json").get("history_context"):
        from jev_ultrafast.agent import Agent

        from .ultrafast_history import HistoryClient, history_agent

        client = HistoryClient(model.CLIENT)
        model.CLIENT = client
        return ultrafast.execute(output, agent_class=history_agent(Agent, client))
    return ultrafast.execute(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    sys.exit(execute(parser.parse_args().output))
