"""Host-side, read-only grading telemetry. Never projected into agent context."""
from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import time
from pathlib import Path

from .model_artifacts import redact
from .protocol import now
from .saas_verifier import BUSINESS_031_V11_UPSTREAM_SHA256


def scoring_config(task_id, source, mode="auto"):
    audited = (task_id == "business_031" and hashlib.sha256(source.encode()).hexdigest()
               == BUSINESS_031_V11_UPSTREAM_SHA256)
    return {"enabled": mode != "off" and audited, "mode": mode,
            "reason": "disabled" if mode == "off" else "audited_read_only" if audited else "not_audited",
            "interval_s": 120, "action_interval": 20, "min_interval_s": 30,
            "scope": "observer_only; non-atomic database checks; never model feedback"}


class ProcessScores:
    def __init__(self, output, source, task, verify, grade, slot, ports, config):
        self.output, self.verify, self.grade = Path(output), verify, grade
        self.slot, self.ports, self.config = slot, ports, config
        # The verifier and its raw output stay outside the agent's run directory.
        self.private = tempfile.TemporaryDirectory(prefix="jev-process-score-")
        self.directory = Path(self.private.name)
        oracle = self.directory / "verify.py"
        oracle.write_text(source)
        self.task = {**task, "verify_py_path": str(oracle)}
        self.rows = []
        self.baseline = self.previous = None
        self.offset = self.actions = self.commits = 0
        self.cycle = None
        self.invalid_events = 0
        self.last_sample = time.monotonic()
        self.last_actions = self.last_commits = 0
        self.stop_event = asyncio.Event()
        self.worker = None
        self.published = 0

    def progress(self):
        path = self.output / "trajectory.jsonl"
        if not path.exists():
            return
        with path.open("rb") as stream:
            stream.seek(self.offset)
            # Only read incremental records, including during slow model requests.
            for _ in range(512):
                position = stream.tell()
                line = stream.readline(1024 * 1024)
                if not line:
                    break
                if not line.endswith(b"\n"):
                    if len(line) < 1024 * 1024:
                        stream.seek(position)  # writer's unfinished row
                        break
                    while line and not line.endswith(b"\n"):
                        line = stream.readline(1024 * 1024)
                    self.invalid_events += 1
                    continue
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError("not an event")
                    self.cycle = row.get("cycle", self.cycle)
                    self.actions += int(row.get("kind") == "action")
                    self.commits += int(row.get("kind") == "transition_confirmed"
                                        and row.get("confirmation_scope") == "business_commit")
                except (ValueError, UnicodeError):
                    self.invalid_events += 1
            self.offset = stream.tell()

    def record(self, verification, phase, started_at, started_epoch, duration_s,
               cycle_start, actions_start):
        self.progress()
        grade = self.grade(verification)
        valid = grade["data_valid"] is True
        row = {"index": len(self.rows), "phase": phase, "is_final": phase == "final",
               "started_at": started_at, "finished_at": now(),
               "started_epoch": started_epoch, "finished_epoch": time.time(),
               "duration_s": round(duration_s, 4), "cycle_start": cycle_start,
               "cycle": self.cycle, "actions_start": actions_start, "actions": self.actions,
               "event_read_errors": self.invalid_events, "data_valid": valid,
               "source": grade["source"], "earned": grade.get("earned") if valid else None,
               "total": grade.get("total") if valid else None,
               "score": grade.get("score") if valid else None,
               "checks": grade["checks"], "verifier_errors": grade["verifier_errors"],
               "error": verification.get("error"), "baseline_earned": None,
               "delta_earned": None, "newly_passed": [], "regressed": [],
               "comparison_available": False}
        if phase == "baseline":
            self.baseline = row
        if valid and self.baseline and self.baseline["data_valid"] and row["total"] == self.baseline["total"]:
            row["baseline_earned"] = self.baseline["earned"]
            row["delta_earned"] = row["earned"] - self.baseline["earned"]
        if valid and self.previous and row["total"] == self.previous["total"]:
            before = self.previous["checks"]
            after = row["checks"]
            # Changed check identities/weights cannot establish a progression.
            def identity(checks):
                return [(c.get("label"), c.get("weight")) for c in checks]
            if identity(before) == identity(after):
                row["comparison_available"] = True
                row["newly_passed"] = [b.get("label") for a, b in zip(before, after)
                                       if a.get("passed") is not True and b.get("passed") is True]
                row["regressed"] = [b.get("label") for a, b in zip(before, after)
                                    if a.get("passed") is True and b.get("passed") is not True]
        if valid:
            self.previous = row
        self.rows.append(redact(row))
        self.last_sample = time.monotonic()
        self.last_actions, self.last_commits = self.actions, self.commits
        return row

    async def sample(self, phase):
        from .saas_benchmark import completed_thread

        self.progress()
        start, epoch, stamp = time.monotonic(), time.time(), now()
        cycle, actions = self.cycle, self.actions
        try:
            verification = await completed_thread(self.verify, self.task, self.slot, self.ports,
                                                  "localhost", str(self.directory))
        except Exception as exc:
            verification = {"error": f"{type(exc).__name__}: {exc}"}
        return self.record(verification, phase, stamp, epoch, time.monotonic() - start, cycle, actions)

    def publish(self, *, force=False):
        # run_trial requires an empty directory. Hold the initial snapshot until
        # the agent initializes its output; it is not passed through trial_args.
        if not force and not any(self.output.iterdir()):
            return
        if self.published == len(self.rows):
            return
        with (self.output / "process-scores.jsonl").open("a") as stream:
            for row in self.rows[self.published:]:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.published = len(self.rows)

    def due(self, elapsed):
        if elapsed < self.config["min_interval_s"]:
            return False
        return (elapsed >= self.config["interval_s"]
                or self.actions - self.last_actions >= self.config["action_interval"]
                or self.commits > self.last_commits)

    async def watch(self):
        while not self.stop_event.is_set():
            self.progress()
            self.publish()
            if self.due(time.monotonic() - self.last_sample):
                await self.sample("intermediate")  # single worker; never overlap graders
                self.publish()
            try:
                await asyncio.wait_for(self.stop_event.wait(), 1)
            except TimeoutError:
                pass

    def start(self):
        self.worker = asyncio.create_task(self.watch())

    async def stop(self):
        self.stop_event.set()
        if self.worker:
            # A grader in flight must finish before final grading / slot cleanup.
            try:
                await asyncio.shield(self.worker)
            except asyncio.CancelledError:
                await self.worker
                raise
            self.worker = None

    def summary(self):
        def compact(row):
            return {k: row.get(k) for k in ("index", "phase", "data_valid", "earned",
                    "total", "delta_earned", "finished_at", "cycle")} if row else None
        return {"config": self.config, "snapshots": len(self.rows),
                "invalid_snapshots": sum(r["data_valid"] is not True for r in self.rows),
                "grading_work_s": round(sum(r["duration_s"] for r in self.rows), 4),
                "additional_grading_work_s": round(sum(r["duration_s"] for r in self.rows
                                                      if not r["is_final"]), 4),
                "baseline": compact(self.baseline), "latest": compact(self.rows[-1] if self.rows else None),
                "artifact": "process-scores.jsonl", "agent_feedback": False}

    def close(self):
        self.private.cleanup()
