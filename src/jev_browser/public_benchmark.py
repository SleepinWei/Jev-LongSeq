"""Run registered, read-only WebArena pilot tasks without exposing evaluation answers."""

from __future__ import annotations

import asyncio
import importlib.metadata
import os
import time
from pathlib import Path

from .candidates import origin
from .controller import Controller
from .dynamic import DynamicController
from .evaluation import efficiency_profile
from .gym_backend import BrowserGymBackend
from .observability import Observer, save_analysis
from .protocol import Budget, InteractionRule, Operation, Predicate, RunResult, Task, digest


async def run_webarena(args, selected, output: Path):
    from .cli import adapters, resolve_tuning, write_json

    if selected["id"] not in {47, 48, 49, 50, 51, 332, 333}:
        raise ValueError("this pilot is limited to reviewed read-only shopping/order tasks")
    output.mkdir(parents=True, exist_ok=False)
    start = os.environ["WA_SHOPPING"]
    task = Task(
        id=f"webarena-{selected['id']}",
        objective=selected["intent"],
        start_url=start,
        allowed_origins=list({origin(os.environ[f"WA_{s.upper()}"]) for s in selected["sites"]}),
        constraints=[
            "Read-only order inspection. Do not buy, cancel, edit or delete anything.",
            "Use only observed page data. Compute totals exactly. Bind final_answer in the plan when supported by evidence.",
        ],
        success_predicates=[Predicate(kind="no_pending_writes")],
        allow_generated_bindings=True,
        requires_final_answer=True,
        rules=[
            InteractionRule(id=n.lower(), name=n, operation=Operation.CLICK)
            for n in ["Next", "Previous", "Back", "Close"]
        ],
    )
    if args.mode == "dynamic":
        task = Task(
            id=task.id,
            control_mode="dynamic",
            objective=task.objective,
            start_url=task.start_url,
            allowed_origins=task.allowed_origins,
            constraints=[task.constraints[0], "Use only visible evidence; compute totals exactly."],
            requires_final_answer=True,
        )
    write_json(output / "task.json", task.model_dump(mode="json"))
    started = time.monotonic()
    observer = Observer(output, gate=getattr(args, "_research_gate", None))
    tuning = resolve_tuning(args)
    budget = Budget(
        max_actions=args.max_actions,
        max_seconds=args.max_seconds,
        max_planner_calls=args.max_planner_calls,
        max_feedback_calls=args.max_feedback_calls,
        brain_interval=tuning.brain_interval,
        candidate_limit=args.candidates,
    )
    model_transports = []
    grade = {}
    controller = None
    manifest = {
        "suite": "webarena",
        "backend": "browsergym",
        "live_preview": getattr(args, "live_preview", False),
        "budget": budget.model_dump(),
        "code_hash": digest(
            {p.name: digest(p.read_text()) for p in Path(__file__).parent.glob("*.py")}
        ),
        "fixture_hash": digest(
            [selected, args.seed, {s: os.environ.get(f"WA_{s.upper()}") for s in selected["sites"]}]
        ),
        "run_id": observer.run_id,
        "tuning": tuning.model_dump() if args.mode == "dynamic" else None,
        "task_id": selected["id"],
        "seed": args.seed,
        "browsergym": importlib.metadata.version("browsergym-core"),
        "libwebarena": importlib.metadata.version("libwebarena"),
        "policy": args.policy,
        "planner": args.planner,
        "mode": args.mode,
        "brain_backend": args.brain,
        "codex_model": args.codex_model if args.brain == "codex" else None,
        "codex_effort": args.codex_effort if args.brain == "codex" else None,
        "codex_timeout": args.codex_timeout if args.brain == "codex" else None,
        "task_hash": digest(task.model_dump(mode="json")),
        "evidence_scope": "One/two read-only public task pilot; horizon has not been measured.",
    }
    write_json(output / "manifest.json", manifest)
    try:
        policy, planner, model_transports = adapters(args)
        for client in model_transports:
            client.observer = observer
        async with (
            asyncio.timeout(args.max_seconds),
            BrowserGymBackend(
                task,
                gym_id=f"browsergym/webarena.{selected['id']}",
                output=output,
                headless=not args.headed,
                seed=args.seed,
                max_env_steps=args.max_actions * 2,
                live_preview=getattr(args, "live_preview", False),
            ) as backend,
        ):
            manifest["chromium"] = backend.browser_version
            controller_type = DynamicController if task.control_mode == "dynamic" else Controller
            controller_options = (
                {"feedback": planner, "tuning": tuning}
                if task.control_mode == "dynamic"
                else {"planner": planner, "mode": args.mode}
            )
            controller = controller_type(
                task,
                backend,
                policy,
                **controller_options,
                budget=budget,
                output=output,
                observer=observer,
            )
            result = await controller.run()
            terminal_before_finish = backend.terminated
            evaluation = await backend.finish(controller.final_answer or "N/A")
            strict = bool(
                evaluation["reward"] == 1
                and not result.violations
                and (result.status == "success" or terminal_before_finish)
            )
            result.strict_success = strict
            if result.status == "success" or strict:
                result.status = "success" if strict else "failed"
                result.reason = (
                    "official WebArena evaluator accepted answer"
                    if strict
                    else "official WebArena evaluator rejected answer"
                )
            grade = {"strict_success": strict, **evaluation}
            write_json(output / "gym-actions.json", backend.events)
    except Exception as exc:
        result = RunResult(
            task_id=task.id,
            status="budget_exhausted" if isinstance(exc, TimeoutError) else "failed",
            reason=f"{type(exc).__name__}: {str(exc)[:200]}",
            actions=controller.actions if controller else 0,
            cycles=controller.cycles if controller else 0,
            planner_calls=controller.planner_calls if controller else 0,
            elapsed_s=time.monotonic() - started,
            strict_success=False,
        )
    ledger = [record for transport in model_transports for record in transport.ledger]
    for transport in model_transports:
        await transport.aclose()
    elapsed = time.monotonic() - started
    costs = [r["cost_usd"] for r in ledger]
    total = (
        sum(costs) + elapsed / 3600 * args.browser_hourly_cost
        if args.browser_hourly_cost is not None and all(c is not None for c in costs)
        else None
    )
    report = {
        "manifest": manifest,
        "result": result.model_dump(),
        "final_answer": controller.final_answer if controller else "",
        "grade": grade,
        "model_calls": ledger,
        "efficiency": efficiency_profile(ledger, actions=result.actions, elapsed_s=elapsed),
        "total_cost_usd": total,
        "end_to_end_s": elapsed,
    }
    write_json(output / "manifest.json", manifest)
    write_json(output / "result.json", result.model_dump())
    write_json(output / "report.json", report)
    save_analysis(output, report)
    return report
