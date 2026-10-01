"""Isolated SaaS-Bench episodes with upstream state verification after the policy stops.

The upstream checkout is trusted executable benchmark code, never model context.
Only description.md and application URLs are passed to the browser agent.
"""

from __future__ import annotations

import asyncio
import copy
import fcntl
import importlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

from .evaluation import efficiency_profile
from .observability import save_analysis, write_json
from .protocol import RunResult, Task, digest, now
from .saas_verifier import verifier_source

DEFAULT_TASKS = ["business_023", "business_031"]


def add_options(parser):
    parser.add_argument(
        "--saas-root",
        default=os.environ.get("SAAS_BENCH_ROOT", ""),
        help="Trusted SaaS-Bench checkout (install with the saas extra)",
    )
    parser.add_argument("--saas-task-ids", nargs="+", default=DEFAULT_TASKS)
    parser.add_argument("--saas-slot", type=int, default=0)
    parser.add_argument("--saas-agent", choices=["longseq", "jev-ultrafast"], default="longseq")
    parser.add_argument("--saas-history-context", action="store_true",
                        help="Experimental observed history context for the original Ultrafast agent")
    parser.add_argument("--saas-resume-from", help="Continue an unsaved LongSeq draft from a prior run directory")
    parser.add_argument("--saas-resume-ui-from",
                        help="Rewind UI to an earlier empty draft checkpoint, retaining latest memory")
    parser.add_argument("--saas-resume-brain-model",
                        help="Explicitly migrate the API brain to this model during Jev draft recovery")


def checkout(args):
    if not args.saas_root:
        raise ValueError("set --saas-root or SAAS_BENCH_ROOT")
    root = Path(args.saas_root).expanduser().resolve()
    if not (root / "saas_bench/apps.yaml").is_file():
        raise ValueError("SaaS-Bench checkout is missing saas_bench/apps.yaml")
    if not 0 <= args.saas_slot <= 99:
        raise ValueError("saas slot must be between 0 and 99")
    return root


def upstream(root):
    # Keep these slots separate from the reference harness's rollout_* containers.
    os.environ.setdefault("SAAS_SLOT_PREFIX", "jevsaas")
    os.environ.setdefault("SAAS_BASE_PORT", "31000")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", os.environ["SAAS_SLOT_PREFIX"]):
        raise ValueError("invalid SAAS_SLOT_PREFIX")
    existing = sys.modules.get("saas_bench")
    if existing and Path(existing.__file__).resolve().parent.parent != root:
        raise ValueError("a different SaaS-Bench checkout is already imported")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return tuple(
        importlib.import_module(f"saas_bench.{name}")
        for name in ("loader", "slot", "verify_runner")
    )


def configuration(root):
    import yaml

    return yaml.safe_load((root / "saas_bench/apps.yaml").read_text())["apps"]


def selected_tasks(args, root):
    loader, _, _ = upstream(root)
    ids = args.saas_task_ids
    if not 1 <= len(ids) <= 2 or len(set(ids)) != len(ids):
        raise ValueError("SaaS-Bench pilot requires one or two distinct task IDs")
    tasks = {t["task_id"]: t for t in loader.load_tasks(str(root / "tasks"))}
    if any(i not in tasks for i in ids):
        raise ValueError(f"unknown SaaS-Bench task IDs: {sorted(set(ids) - tasks.keys())}")
    return [tasks[i] for i in ids]


def images_for(root, apps, sites):
    import yaml

    images = set()
    for site in sites:
        cfg = apps[site]
        if cfg.get("start_type") == "compose":
            template = yaml.safe_load((root / cfg["compose_template_file"]).read_text())
            images.update(s["image"] for s in template["services"].values())
        else:
            images.add(shlex.split(cfg["start"])[-1])
    return sorted(images)


def command(*args):
    return subprocess.run(args, capture_output=True, text=True, timeout=30, check=False)


def _preflight(args):
    report = {
        "suite": "saas-bench",
        "checked_at": now(),
        "selected_tasks": [],
        "missing_environment": [],
        "missing_images": [],
        "errors": [],
        "scope": "SaaS-Bench upstream tasks; one/two-task pilot, not full-suite results",
    }
    try:
        root = checkout(args)
        if getattr(args, "saas_history_context", False) and getattr(args, "saas_agent", "longseq") != "jev-ultrafast":
            raise ValueError("--saas-history-context requires --saas-agent jev-ultrafast")
        tasks = selected_tasks(args, root)
        apps = configuration(root)
        _, slots, _ = upstream(root)
        slot = slots.SlotManager(apps, args.saas_slot)
        for task in tasks:
            sites = task["meta"].get("meta_data", {}).get("sites", [])
            if not sites or set(sites) - apps.keys():
                raise ValueError(f"invalid sites for {task['task_id']}")
            if not task["verify_py_path"]:
                report["errors"].append(f"{task['task_id']}: missing verify.py")
            else:
                verifier_source(task)  # Reject an unaudited patch before any model calls.
            if task["meta"].get("multimodal_input"):
                report["errors"].append(
                    f"{task['task_id']}: file/multimodal inputs unsupported by the DOM agent"
                )
            report["selected_tasks"].append(
                {
                    "id": task["task_id"],
                    "intent": task["description_md"],
                    "sites": sites,
                    "port_map": slot.get_port_map(sites),
                }
            )
        revision = command("git", "-C", str(root), "rev-parse", "HEAD")
        if revision.returncode:
            raise ValueError("SaaS-Bench must be a versioned Git checkout")
        report["upstream_revision"] = revision.stdout.strip()
        report["root"] = str(root)
        report["docker_context"] = os.environ.get("DOCKER_CONTEXT", "default")
        report["slot_prefix"] = slots._SLOT_PREFIX
        report["required_images"] = images_for(
            root, apps, sorted({s for t in report["selected_tasks"] for s in t["sites"]})
        )
        info = command("docker", "info", "--format", "{{json .}}")
        if info.returncode:
            report["errors"].append("Docker daemon unavailable in the configured context")
        else:
            daemon = json.loads(info.stdout)
            report["docker"] = {
                k: daemon.get(k) for k in ("Architecture", "MemTotal", "NCPU", "ServerVersion")
            }
            if command("docker", "compose", "version").returncode:
                report["errors"].append("Docker Compose plugin unavailable")
            for image in report["required_images"]:
                if command("docker", "image", "inspect", image).returncode:
                    report["missing_images"].append(image)
        if getattr(args, "saas_agent", "longseq") == "jev-ultrafast":
            from .ultrafast import configuration as original_configuration

            report["agent"] = original_configuration()
            if getattr(args, "command", "benchmark") != "benchmark":
                report["errors"].append("Original Ultrafast supports benchmark, not LongSeq autoresearch tuning")
            if not getattr(args, "environment_only", False):
                if not report["agent"]["available"]:
                    report["errors"].append("Original Ultrafast source unavailable; set JEV_ULTRAFAST_ROOT")
                report["missing_environment"] = [k for k in ["TYPESAFE_API_KEY", "TEXT_MODEL_API_KEY"]
                                                   if not os.environ.get(k)]
        elif not getattr(args, "environment_only", False):
            if args.policy == "rule":
                report["errors"].append("SaaS-Bench requires a model policy")
            keys = (
                ["TYPESAFE_API_KEY"]
                if args.policy == "jev"
                else ["POLICY_ENDPOINT", "POLICY_MODEL", "POLICY_API_KEY"]
            )
            if args.brain == "api":
                keys += ["PLANNER_ENDPOINT", "PLANNER_MODEL", "PLANNER_API_KEY"]
            report["missing_environment"] = [k for k in keys if not os.environ.get(k)]
    except (ValueError, OSError, ImportError, subprocess.SubprocessError) as exc:
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    report["status"] = (
        "blocked"
        if any(report[k] for k in ("errors", "missing_images", "missing_environment"))
        else "ready"
    )
    return report


async def check_saas(args):
    return await asyncio.to_thread(_preflight, args)


async def completed_thread(fn, *args, **kwargs):
    """Do not let cancellation race Docker startup with the finally cleanup."""
    task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def grade_result(verification):
    # Upstream verifiers catch SQL/runtime errors and sometimes emit ordinary
    # FAIL + rc=1. These are unavailable checks, not evidence of agent failure.
    verifier_errors = [c for c in verification.get("checks", [])
                       if re.match(r"\s*(exception|error)\s*:", str(c.get("detail", "")), re.I)]
    valid = (
        verification.get("status") in {"PASS", "FAIL"}
        and verification.get("returncode") in {0, 1}
        and verification.get("total", 0) > 0
        and bool(verification.get("checks"))
        and not verifier_errors
    )
    passed = (
        valid
        and verification["status"] == "PASS"
        and verification.get("returncode") == 0
        and verification.get("all_pass") is True
        and verification.get("earned") == verification.get("total")
        and all(c.get("passed") is True for c in verification["checks"])
    )
    return {
        "strict_success": passed if valid else None,
        "data_valid": valid,
        "source": "SaaS-Bench official verify.py",
        "score": verification.get("score"),
        "earned": verification.get("earned"),
        "total": verification.get("total"),
        "checks": verification.get("checks", []),
        "verifier_errors": verifier_errors,
        "verification": verification,
    }


async def run_saas(args, selected, output):
    from .cli import run_trial

    root = checkout(args)
    loader, slots, verifier = upstream(root)
    task = next(t for t in selected_tasks(args, root) if t["task_id"] == selected["id"])
    apps = configuration(root)
    sites = task["meta"]["meta_data"]["sites"]
    slot = slots.SlotManager(apps, args.saas_slot)
    ports = slot.get_port_map(sites)
    prompt, _, _ = loader.build_prompt(task, ports, "localhost")
    urls = [f"http://localhost:{ports[site]}" for site in sites]
    browser_task = Task(
        id=f"saas-bench-{task['task_id']}",
        control_mode="dynamic",
        objective=prompt,
        start_url=urls[0],
        allowed_origins=urls,
        constraints=[
            "Use the isolated benchmark applications through their UI.",
            "Only perform writes requested by the task.",
        ],
    )
    image_ids = {
        image: command("docker", "image", "inspect", "--format", "{{.Id}}", image).stdout.strip()
        for image in images_for(root, apps, sites)
    }
    revision = command("git", "-C", str(root), "rev-parse", "HEAD").stdout.strip()
    original_verifier, effective_verifier, verifier_patch = verifier_source(task)
    verifier_hash = digest(effective_verifier)
    manifest = {
        "upstream_revision": revision,
        "suite": "saas-bench",
        "benchmark": "SaaS-Bench",
        "task_id": task["task_id"],
        "task_hash": digest(prompt),
        "fixture_hash": digest(
            {
                "apps": apps,
                "meta": task["meta"],
                "images": image_ids,
                "revision": revision,
                "verifier_hash": verifier_hash,
            }
        ),
        "verifier_hash": verifier_hash,
        "upstream_verifier_hash": digest(original_verifier),
        "verifier_patch": verifier_patch,
        "image_ids": image_ids,
        "port_map": ports,
        "slot_id": args.saas_slot,
        "slot_prefix": slots._SLOT_PREFIX,
        "code_hash": digest(
            {p.name: digest(p.read_text()) for p in Path(__file__).parent.glob("*.py")}
        ),
    }
    checkpoint = None
    if getattr(args, "saas_resume_brain_model", None) and not getattr(args, "saas_resume_from", None):
        raise ValueError("--saas-resume-brain-model requires --saas-resume-from")
    if getattr(args, "saas_resume_ui_from", None) and not getattr(args, "saas_resume_from", None):
        raise ValueError("--saas-resume-ui-from requires --saas-resume-from")
    if getattr(args, "saas_resume_from", None):
        from .continuation import load_continuation

        if getattr(args, "saas_agent", "longseq") != "longseq":
            raise ValueError("draft continuation requires LongSeq")
        checkpoint = load_continuation(args.saas_resume_from, browser_task, manifest,
                                       ui_directory=getattr(args, "saas_resume_ui_from", None))
        manifest["continuation"] = checkpoint["memory"].resume_context
    if output.exists() and any(output.iterdir()):
        raise ValueError("SaaS-Bench task output must be empty")
    output.mkdir(parents=True, exist_ok=True)
    verification_task = task
    if verifier_patch:
        verifier_path = output / "verifier.compat.py"
        verification_task = {**task, "verify_py_path": str(verifier_path.resolve())}
    report = None
    started = time.monotonic()
    setup_s, verify_s, cleanup_error = 0.0, 0.0, None
    # One process owns each slot across setup, agent execution, verification and cleanup.
    lock = (root / f".jev-{slots._SLOT_PREFIX}-{args.saas_slot}.lock").open("a")
    owned = False
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        owned = True
        await completed_thread(slot.start_apps, sites, hostname="localhost")
        setup_s = time.monotonic() - started
        if not getattr(args, "environment_only", False) and getattr(args, "saas_agent", "longseq") == "jev-ultrafast":
            from .saas_ultrafast import run_original

            report = await run_original(args, browser_task, manifest, output)
        elif not getattr(args, "environment_only", False):
            trial_args = copy.copy(args)
            trial_args.command, trial_args.suite = "benchmark", "saas-bench"
            trial_args.backend, trial_args.mode = "playwright", "dynamic"
            trial_args._benchmark_task = browser_task
            trial_args._benchmark_manifest = manifest
            trial_args._benchmark_urls = urls[1:]
            if checkpoint:
                from .protocol import AgentTuning

                trial_args._resume_checkpoint = checkpoint
                trial_args._tuning = AgentTuning.model_validate(checkpoint["manifest"]["tuning"])
            report = await run_trial(trial_args, output=output)
        # LongSeq requires an empty output directory when it starts. Keep the
        # hidden oracle out of that directory until the agent has stopped.
        if verifier_patch:
            verifier_path.write_text(effective_verifier)
        verify_started = time.monotonic()
        verification = await completed_thread(
            verifier.run_verify, verification_task, args.saas_slot, ports, "localhost", str(output)
        )
        verify_s = time.monotonic() - verify_started
        grade = grade_result(verification)
        if verifier_patch:
            grade.update(source="SaaS-Bench verify.py with BigCapital schema compatibility",
                         verifier_patch=verifier_patch)
        if report is None:
            report = empty_report(
                browser_task,
                manifest,
                "environment_check",
                "Environment smoke only; no agent or model calls",
            )
            grade["strict_success"] = None
            report["result"]["status"] = "success" if grade["data_valid"] else "failed"
        else:
            strict = grade["strict_success"]
            if strict is not None:
                strict = (
                    strict
                    and report["result"]["status"] == "success"
                    and not report["result"]["violations"]
                )
            report["result"]["strict_success"] = strict
            if report["result"].get("finish_requests") and strict is False:
                report["result"]["false_completions"] = 1
            if report["result"]["status"] == "success" and strict is not True:
                report["result"].update(
                    status="failed", reason="Official verifier rejected completion"
                )
        report["grade"] = grade
    except Exception as exc:
        report = report or empty_report(
            browser_task,
            manifest,
            "failed",
            f"SaaS-Bench setup/verification: {type(exc).__name__}: {exc}",
        )
        report["result"].update(status="failed", strict_success=None)
        report["grade"] = {
            "strict_success": None,
            "data_valid": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if owned:
            try:
                await completed_thread(slot.stop_apps, sites)
            except Exception as exc:
                cleanup_error = f"{type(exc).__name__}: {exc}"
        lock.close()
    report["environment"] = {
        "setup_s": setup_s,
        "verification_s": verify_s,
        "total_s": time.monotonic() - started,
        "cleanup_error": cleanup_error,
    }
    if cleanup_error:
        report["result"].update(status="failed", strict_success=None)
        report["grade"]["data_valid"] = False
    report["evidence_scope"] = (
        "Environment smoke; no agent performance measured"
        if getattr(args, "environment_only", False)
        else "SaaS-Bench official state verifier; bounded pilot only"
    )
    if checkpoint:
        report["evidence_scope"] = "Continuation after UI draft reconstruction; official end-state " \
                                   "verification, not a fresh episode or exact environment checkpoint."
    for name in ("manifest", "result"):
        write_json(output / f"{name}.json", report[name])
    write_json(output / "report.json", report)
    save_analysis(output, report)
    return report


def empty_report(task, manifest, status, reason):
    result = RunResult(
        task_id=task.id,
        status="failed",
        strict_success=None,
        reason=reason,
        actions=0,
        cycles=0,
        planner_calls=0,
        elapsed_s=0,
    ).model_dump()
    return {
        "manifest": {**manifest, "execution_kind": status},
        "result": result,
        "grade": {},
        "model_calls": [],
        "end_to_end_s": 0,
        "total_cost_usd": None,
        "efficiency": efficiency_profile([], actions=0, elapsed_s=0),
    }
