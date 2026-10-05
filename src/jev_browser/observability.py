"""Low-overhead, local telemetry and evidence-based run diagnostics."""

from __future__ import annotations

import json
import re
import time
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path

from .context_budget import wire_bytes
from .evaluation import efficiency_profile, quantile
from .protocol import now


class ResourceLimit(RuntimeError):
    """A local experiment budget was reached before dispatching another model request."""


class ModelCallTimeout(RuntimeError):
    """One model request exceeded its allowance, independent of the run deadline."""


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # an interrupted writer can leave a partial final line
    return rows


def write_json(path: Path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def payload_sizes(payload: dict) -> dict:
    """Byte counts only, not token estimates or copies of potentially sensitive prompts."""

    size = wire_bytes

    content = payload.get("state")
    if content is None:
        messages = payload.get("messages", [])
        try:
            content = json.loads(messages[-1]["content"]) if messages else {}
        except (TypeError, KeyError, json.JSONDecodeError):
            content = {}
    return (
        {
            "total_bytes": size(payload),
            "state_sections_bytes": {key: size(value) for key, value in content.items()},
        }
        if isinstance(content, dict)
        else {"total_bytes": size(payload)}
    )


class Observer:
    def __init__(self, output: Path | None = None, *, gate=None):
        self.output, self.gate = output, gate
        self.run_id = uuid.uuid4().hex
        self.context: dict = {}
        self.spans: list[dict] = []
        self.calls: list[dict] = []
        if output:
            output.mkdir(parents=True, exist_ok=True)

    def append(self, filename, row):
        if self.output:
            with (self.output / filename).open("a") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    @contextmanager
    def span(self, name, **attributes):
        parent = self.context.get("span_id")
        row = {
            **self.context,
            "span_id": uuid.uuid4().hex,
            "parent_id": parent,
            "run_id": self.run_id,
            "name": name,
            "started_at": now(),
            **attributes,
        }
        self.context["span_id"] = row["span_id"]
        started = time.monotonic()
        try:
            yield row
            row["status"] = "ok"
        except BaseException as exc:
            row.update(status="error", error=type(exc).__name__)
            raise
        finally:
            if parent is None:
                self.context.pop("span_id", None)
            else:
                self.context["span_id"] = parent
            row["duration_s"] = time.monotonic() - started
            self.spans.append(row)
            self.append("spans.jsonl", row)

    async def measure(self, name, function, *args, **kwargs):
        with self.span(name):
            return await function(*args, **kwargs)

    def before_call(self):
        if self.gate:
            self.gate.before_call()

    def request_started(self, record):
        self.append("model-request-starts.jsonl", record)

    def model_artifact(self, attempt_id, direction, value, *, secrets=()):
        """Capture separately from metadata; telemetry must never break model execution."""
        import os

        from .model_artifacts import capture

        if not self.output or os.environ.get("JEV_CAPTURE_MODEL_ARTIFACTS") == "0":
            return {"available": False, "reason": "capture_disabled"}
        try:
            return {"available": True, **capture(self.output, attempt_id, direction, value,
                                                 secrets=secrets)}
        except Exception as exc:
            return {"available": False, "reason": "capture_failed", "error": type(exc).__name__}

    def model_call(self, record):
        self.calls.append(dict(record))
        self.append("model-calls.jsonl", record)
        if self.gate:
            self.gate.record(record)


def analyze(report: dict, events: list[dict], spans: list[dict] | None = None) -> dict:
    """Diagnostics point to observable symptoms; they do not claim causal proof."""
    spans = spans or []
    calls = report.get("model_calls", [])
    result, grade = report.get("result", {}), report.get("grade", {})
    efficiency = efficiency_profile(
        calls,
        actions=result.get("actions", 0),
        elapsed_s=report.get("end_to_end_s", result.get("elapsed_s", 0)),
    )
    failures = Counter(
        r.get("error") or str(r.get("status"))
        for r in calls
        if r.get("error") or r.get("status", 200) >= 400
    )
    kinds = Counter(e.get("kind") for e in events)
    triggers = Counter(e.get("reason") for e in events if e.get("kind") == "brain_requested")
    receipts = Counter(
        e.get("receipt", {}).get("status") for e in events if e.get("kind") == "action"
    )
    durations = defaultdict(list)
    for span in spans:
        durations[span["name"]].append(span["duration_s"])
    contexts = defaultdict(list)
    for call in calls:
        if call.get("payload_sizes"):
            contexts[call["kind"]].append(call["payload_sizes"]["total_bytes"])
    findings = []

    def finding(code, severity, evidence, hypothesis, recommendation):
        findings.append(
            dict(
                code=code,
                severity=severity,
                evidence=evidence,
                hypothesis=hypothesis,
                recommendation=recommendation,
            )
        )

    if failures:
        finding(
            "transport_instability",
            "high",
            dict(failures),
            "Model-service failures confound controller efficiency and may prevent completion.",
            "Keep failed attempts in accounting; do not tune retries or promote a cheaper failed run.",
        )
    if result.get("status") == "failed" and not calls and not result.get("actions"):
        finding(
            "setup_failure",
            "high",
            {"reason": result.get("reason")},
            "The run failed before any model request or browser action.",
            "Repair setup before evaluating an agent strategy.",
        )
    if grade.get("strict_success") and result.get("strict_success") is not True:
        finding(
            "incomplete_handoff",
            "high",
            {
                "environment_success": True,
                "agent_status": result.get("status"),
                "finish_requests": result.get("finish_requests", 0),
            },
            "The environment is correct but the agent did not complete its final handoff.",
            "Test explicit coverage summaries and completion guidance without weakening verification.",
        )
    invalid = kinds["invalid_feedback"] + kinds["invalid_plan"]
    if invalid:
        finding(
            "schema_repair",
            "medium",
            {"invalid_outputs": invalid},
            "Invalid structured output consumes extra LLM calls.",
            "Test concise schema-focused guidance; retain strict output validation.",
        )
    brain = efficiency["by_component"].get("brain", {})
    model_time = efficiency["total"]["request_time_s"]
    share = brain.get("request_time_s", 0) / model_time if model_time else 0
    if share > 0.5:
        finding(
            "brain_latency",
            "medium",
            {
                "brain_share_of_request_time": share,
                "brain_successful_responses": brain.get("successful_responses", 0),
            },
            "LLM requests account for most measured model-request time (including retries).",
            "Test a longer guidance window or a compact response profile on the same task.",
        )
    jev = efficiency["by_component"].get("jev", {})
    per_call = jev.get("known_input_tokens", 0) / max(1, jev.get("successful_responses", 0))
    if per_call > 2500:
        finding(
            "context_volume",
            "medium",
            {"known_jev_input_tokens_per_successful_response": per_call},
            "Repeated policy context is a material input-token cost.",
            "Test a smaller recent-evidence window while preserving the evidence archive and checks.",
        )
    if result.get("false_completions", 0):
        finding(
            "false_completion",
            "high",
            {"rejected_finishes": result["false_completions"]},
            "The local policy requests completion before the verifier agrees.",
            "Test explicit coverage and unresolved-work summaries; keep the completion verifier.",
        )
    if receipts["stale"]:
        finding(
            "stale_actions",
            "medium",
            {"stale_receipts": receipts["stale"]},
            "The page changed between observation and execution; this alone is not a grounding bug.",
            "Inspect correlated observation/action spans before changing browser synchronization.",
        )
    if (result.get("status") and result.get("strict_success") is not True
            and not failures and (calls or result.get("actions"))):
        finding(
            "task_incomplete", "high",
            {"status": result["status"], "reason": str(result.get("reason", ""))[:500],
             "actions": result.get("actions", 0),
             "finish_requests": result.get("finish_requests", 0)},
            "Task completion was not independently confirmed; low cost is not a quality gain.",
            "Inspect this task's stopping condition and evidence coverage before optimizing efficiency.",
        )
    return {
        "version": 1,
        "quality": {
            "agent_status": result.get("status"),
            "strict_success": result.get("strict_success"),
            "environment_success": grade.get("strict_success"),
            "violations": len(result.get("violations", [])) + grade.get("violations", 0),
            "duplicate_writes": grade.get("duplicates", 0),
        },
        "efficiency": efficiency,
        "failures": dict(failures),
        "brain_triggers": dict(triggers),
        "receipts": dict(receipts),
        "event_counts": dict(kinds),
        "spans": {
            k: {
                "count": len(v),
                "total_s": sum(v),
                "p50_s": quantile(v, 0.5),
                "p95_s": quantile(v, 0.95),
            }
            for k, v in durations.items()
        },
        "context_bytes": {k: {"mean": sum(v) / len(v), "max": max(v)} for k, v in contexts.items()},
        "findings": findings,
        "measurement_notes": [
            "Nested spans overlap; do not sum them as end-to-end time.",
            "Payload bytes are not tokens. Only provider usage counts as tokens.",
            "Findings are hypotheses, not causal attribution.",
        ],
    }


def save_analysis(output: Path, report: dict | None = None):
    report_path = output / "report.json"
    if report is None:
        report = (
            json.loads(report_path.read_text())
            if report_path.exists()
            else {
                "model_calls": read_jsonl(output / "model-calls.jsonl"),
                "result": {},
            }
        )
    result = analyze(
        report, read_jsonl(output / "trajectory.jsonl"), read_jsonl(output / "spans.jsonl")
    )
    if report.get("task_reports"):
        result["task_observations"] = []
        for task_report in report["task_reports"]:
            task_id = task_report["manifest"]["task_id"]
            suite = task_report["manifest"].get("suite", "webarena")
            if suite == "saas-bench":
                if not isinstance(task_id, str) or not re.fullmatch(r"[a-z]+_[0-9]+", task_id):
                    raise ValueError("invalid SaaS-Bench task ID")
            elif suite in {"webarena", "public-web"}:
                if type(task_id) is not int:
                    raise ValueError("public task IDs must be integers")
            else:
                raise ValueError("unrecognized public task suite")
            child = output / f"{suite}-{task_id}"
            observation = analyze(
                task_report,
                read_jsonl(child / "trajectory.jsonl"),
                read_jsonl(child / "spans.jsonl"),
            )
            result["task_observations"].append({"task_id": task_id, **observation})
            for finding in observation["findings"]:
                result["findings"].append(
                    {**finding, "evidence": {"task_id": task_id, "details": finding["evidence"]}}
                )
    starts = read_jsonl(output / "model-request-starts.jsonl")
    completed = {c.get("attempt_id") for c in report.get("model_calls", [])}
    result["inflight_attempts"] = [
        {k: r.get(k) for k in ("attempt_id", "call_id", "kind", "transport", "cycle", "started_at")}
        for r in starts
        if r.get("attempt_id") not in completed
    ]
    write_json(output / "observability.json", result)
    rows = [
        "# Run observability",
        "",
        f"Quality: `{json.dumps(result['quality'])}`",
        "",
        "| Component | Attempts | HTTP | Codex CLI | Input tokens (known) | Output tokens (known) | Request time (s) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, value in result["efficiency"]["by_component"].items():
        rows.append(
            f"| {name} | {value['attempts']} | {value['http_attempts']} | {value['codex_invocations']} | {value['known_input_tokens']} | "
            f"{value['known_output_tokens']} | {value['request_time_s']:.3f} |"
        )
    rows.extend(["", "## Findings", ""])
    for finding in result["findings"]:
        rows.append(
            f"- **{finding['code']}**: {finding['hypothesis']} "
            f"Evidence: `{json.dumps(finding['evidence'])}`. {finding['recommendation']}"
        )
    rows.extend(
        [
            "",
            "Missing usage and prices remain unknown. A faster failed run is not an efficiency win.",
            "",
        ]
    )
    (output / "observability.md").write_text("\n".join(rows))
    return result
