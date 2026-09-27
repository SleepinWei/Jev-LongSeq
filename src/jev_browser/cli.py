from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
import platform
import sys
import time
from pathlib import Path

from .browser import PlaywrightBackend
from .codex_transport import CodexTransport
from .config import load_env_file, use_text_model_for_planner
from .controller import Controller
from .dynamic import DynamicController, JsonFeedback
from .evaluation import calibration, efficiency_profile, summarize
from .fixture import RulePlanner, RulePolicy, catalog_html, demo_task
from .models import JevPolicy, JsonPlanner, JsonPolicy, ModelTransport, Pricing, instructions
from .observability import Observer, save_analysis
from .protocol import AgentTuning, Budget, RunResult, Task, digest, now


def resolve_tuning(args):
    if not hasattr(args, "_tuning"):
        args._tuning = (
            AgentTuning.model_validate_json(Path(args.tuning).read_text())
            if getattr(args, "tuning", None)
            else AgentTuning(brain_interval=args.brain_interval)
        )
    return args._tuning


def transport(prefix: str, default_endpoint: str = "", default_model: str = ""):
    endpoint = os.environ.get(f"{prefix}_ENDPOINT", default_endpoint)
    model = os.environ.get(f"{prefix}_MODEL", default_model)
    if not endpoint or not model:
        raise ValueError(f"set {prefix}_ENDPOINT and {prefix}_MODEL")

    def price(key):
        return float(os.environ[key]) if key in os.environ else None

    return ModelTransport(
        endpoint,
        os.environ.get(f"{prefix}_API_KEY", ""),
        model,
        timeout_s=float(
            os.environ.get(f"{prefix}_TIMEOUT_SECONDS", "90" if prefix == "PLANNER" else "30")
        ),
        pricing=Pricing(
            price(f"{prefix}_INPUT_PER_MILLION"), price(f"{prefix}_OUTPUT_PER_MILLION")
        ),
    )


def adapters(args):
    transports = []
    if args.mode == "dynamic" and args.policy == "rule":
        raise ValueError(
            "dynamic mode requires a model policy; rule policy is a structured fixture"
        )
    if args.policy == "rule":
        policy = RulePolicy()
    elif args.policy == "jev":
        client = transport("TYPESAFE", "https://api.typesafe.ai/v1/systemone", "jev-latest")
        policy = JevPolicy(client)
        transports.append(client)
    else:
        client = transport("POLICY")
        policy = JsonPolicy(client)
        transports.append(client)
    planner = None

    def brain_transport():
        if args.brain == "codex":
            return CodexTransport(
                model=args.codex_model, effort=args.codex_effort, timeout_s=args.codex_timeout
            )
        use_text_model_for_planner()
        return transport("PLANNER")

    if args.mode == "dynamic":
        client = brain_transport()
        planner = JsonFeedback(client, resolve_tuning(args))
        transports.append(client)
    elif args.mode != "flat":
        if args.planner == "rule":
            planner = RulePlanner()
        else:
            client = brain_transport()
            planner = JsonPlanner(client)
            transports.append(client)
    return policy, planner, transports


def write_json(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


async def run_trial(args, *, count=None, output=None):
    started = time.monotonic()
    output = Path(output or args.output)
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"output directory is not empty: {output}; choose a new run directory")
    output.mkdir(parents=True, exist_ok=True)
    is_demo = (
        args.command in {"demo", "matrix", "benchmark"}
        and getattr(args, "suite", "catalog") == "catalog"
    )
    custom_goal = getattr(args, "goal", None) if is_demo else None
    if custom_goal and (args.mode != "dynamic" or args.policy == "rule"):
        raise ValueError("a demo --goal requires --mode dynamic and a model policy")
    if hasattr(args, "_benchmark_task"):
        task = args._benchmark_task
    elif args.command == "browse":
        from .candidates import origin

        task = Task(
            id="browse",
            control_mode="dynamic",
            objective=args.goal,
            start_url=args.url or "about:blank",
            allowed_origins=list(dict.fromkeys(
                ([origin(args.url)] if args.url else []) + args.allow_origin)),
        )
    elif is_demo and args.mode == "dynamic":
        task = Task(
            id=f"catalog-{count or args.records}",
            control_mode="dynamic",
            sandbox=True,
            objective=custom_goal
            or "Visit every record in the catalog. Save exactly those with Rating >= 4 "
            "and Price <= 60; read back each saved state. Never delete or save twice.",
        )
    else:
        task = (
            demo_task(count or args.records)
            if is_demo
            else Task.model_validate_json(Path(args.task).read_text())
        )
    if task.control_mode == "dynamic":
        args.mode = "dynamic"
    elif args.mode == "dynamic":
        raise ValueError("use a dynamic task without predefined rules/predicates")
    tuning = resolve_tuning(args)
    observer = Observer(output, gate=getattr(args, "_research_gate", None))
    budget = Budget(
        max_actions=args.max_actions,
        max_seconds=args.max_seconds,
        candidate_limit=args.candidates,
        max_planner_calls=args.max_planner_calls,
        max_feedback_calls=args.max_feedback_calls,
        brain_interval=tuning.brain_interval,
    )
    manifest = {
        "task_id": task.id,
        "run_id": observer.run_id,
        "started_at": now(),
        "policy": args.policy,
        "backend": args.backend,
        "planner": args.planner if args.mode not in {"flat", "dynamic"} else None,
        "feedback_model": args.brain if args.mode == "dynamic" else None,
        "brain_backend": args.brain,
        "codex_model": args.codex_model if args.brain == "codex" else None,
        "codex_effort": args.codex_effort if args.brain == "codex" else None,
        "codex_timeout": args.codex_timeout if args.brain == "codex" else None,
        "mode": args.mode,
        "live_preview": getattr(args, "live_preview", False),
        "tuning": tuning.model_dump() if args.mode == "dynamic" else None,
        "budget": budget.model_dump(),
        "task_hash": digest(task.model_dump(mode="json")),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "playwright": importlib.metadata.version("playwright"),
        "viewport": {"width": 1280, "height": 900},
        "system_prompt_hash": digest(instructions(task)),
        "benchmark": "offline-catalog-v1" if is_demo and not custom_goal else "user-task-ungraded",
        "fixture": {
            "records": count or args.records,
            "popup": args.popup,
            "reorder": args.reorder,
            "injection": args.injection,
            "delayed_save_ms": args.delayed_save_ms,
            "lost_ack": args.lost_ack,
        }
        if is_demo
        else None,
        "browser_hourly_cost_usd": args.browser_hourly_cost,
    }
    if args.command == "browse":
        manifest["start_selection"] = {"status": "resolving"}
    manifest["code_hash"] = digest(
        {p.name: digest(p.read_text()) for p in Path(__file__).parent.glob("*.py")}
    )
    manifest.update(getattr(args, "_benchmark_manifest", {}))
    write_json(output / "manifest.json", manifest)
    write_json(output / "task.json", task.model_dump(mode="json"))
    transports, grade, controller = [], {}, None
    try:
        policy, planner, transports = adapters(args)
        for client in transports:
            client.observer = observer
        if args.command == "browse":
            from .start_page import resolve_start_page

            async with asyncio.timeout(min(60, args.max_seconds)):
                selection = await resolve_start_page(args.goal, args.url, planner.transport)
            task.start_url = selection["url"]
            task.allowed_origins = list(dict.fromkeys([origin(task.start_url), *args.allow_origin]))
            manifest["start_selection"] = {"status": "selected", **selection}
            manifest["task_hash"] = digest(task.model_dump(mode="json"))
            write_json(output / "task.json", task.model_dump(mode="json"))
            write_json(output / "manifest.json", manifest)
        if args.backend == "browsergym":
            from .gym_backend import BrowserGymBackend

            backend = BrowserGymBackend(
                task,
                output=output,
                headless=not args.headed,
                records=count or args.records,
                seed=getattr(args, "seed", 0),
                popup=args.popup,
                injection=args.injection,
                max_env_steps=args.max_actions * 2,
                live_preview=getattr(args, "live_preview", False),
            )
            manifest["browsergym"] = importlib.metadata.version("browsergym-core")
            manifest["benchmark"] = (
                "user-task-ungraded" if custom_goal else "browsergym-custom-catalog-v1"
            )
            if args.reorder or args.delayed_save_ms or args.lost_ack:
                raise ValueError(
                    "these fault flags are currently supported only by the Playwright backend"
                )
        elif args.backend == "chrome":
            from .chrome import ChromeBackend

            backend = ChromeBackend(task, output=output,
                                    live_preview=getattr(args, "live_preview", False))
        else:
            backend = PlaywrightBackend(
                task,
                headless=not args.headed,
                output=output,
                live_preview=getattr(args, "live_preview", False),
            )
        backend.observer = observer
        async with (
            asyncio.timeout(max(0.001, args.max_seconds - (time.monotonic() - started))),
            backend as browser,
        ):
            manifest["chromium"] = browser.browser_version
            if is_demo:
                html = catalog_html(
                    count or args.records,
                    popup=args.popup,
                    reorder=args.reorder,
                    injection=args.injection,
                    delayed_save_ms=args.delayed_save_ms,
                    lost_ack=args.lost_ack,
                )
                manifest["fixture_hash"] = digest(html)
                if args.backend == "playwright":
                    await browser.load_html(html)
            controller_type = DynamicController if task.control_mode == "dynamic" else Controller
            controller_options = (
                {"feedback": planner, "tuning": tuning}
                if task.control_mode == "dynamic"
                else {"planner": planner, "mode": args.mode}
            )
            controller = controller_type(
                task,
                browser,
                policy,
                **controller_options,
                budget=budget.model_copy(
                    update={
                        "max_seconds": max(0.001, args.max_seconds - (time.monotonic() - started))
                    }
                ),
                output=output,
                observer=observer,
            )
            result = await controller.run()
            if is_demo and not custom_goal:
                # Hidden truth is never passed to the controller, planner, or local policy.
                if args.backend == "browsergym":
                    gym_grade = await browser.finish(
                        controller.final_answer
                        or ("Finished" if result.status == "success" else "Stopped")
                    )
                    manifest["gym_env_steps"] = gym_grade["env_steps"]
                    manifest["gym_refresh_steps"] = gym_grade["refresh_steps"]
                    grade = gym_grade["info"].get("grade", {})
                    write_json(output / "gym-actions.json", browser.events)
                else:
                    grade = await browser.page.evaluate("window.__grade()")
                result.strict_success = bool(grade["strict_success"] and result.status == "success")
                records = count or args.records
                manifest["reference_actions"] = (
                    3 * records
                    - 1
                    + 2 * len(grade["expected"])
                    + (records - 1) // 5
                    + int(args.popup)
                )
                manifest["reference_scope"] = (
                    "fault-free script, includes explicit extraction/readback; excludes waiting and recovery"
                )
    except Exception as exc:
        status = "budget_exhausted" if isinstance(exc, TimeoutError) else "failed"
        result = (
            controller.result(status, f"{type(exc).__name__}: {str(exc)[:200]}")
            if controller
            else RunResult(
                task_id=task.id,
                status=status,
                reason=f"{type(exc).__name__}: {str(exc)[:200]}",
                actions=0,
                cycles=0,
                planner_calls=0,
                elapsed_s=time.monotonic() - started,
            )
        )
        if is_demo and not custom_goal:
            result.strict_success = False
    ledger = [record for client in transports for record in client.ledger]
    for client in transports:
        await client.aclose()
    end_to_end = time.monotonic() - started
    model_costs = [record["cost_usd"] for record in ledger]
    browser_cost = (
        end_to_end / 3600 * args.browser_hourly_cost
        if args.browser_hourly_cost is not None
        else None
    )
    # Missing prices or uncertain failed-call usage must not be silently treated as zero.
    cost = (
        sum(model_costs) + browser_cost
        if all(c is not None for c in model_costs) and browser_cost is not None
        else None
    )
    manifest["model_calls"] = len(ledger)
    report = {
        "manifest": manifest,
        "result": result.model_dump(),
        "final_answer": controller.final_answer if controller else "",
        "grade": grade,
        "model_calls": ledger,
        "efficiency": efficiency_profile(ledger, actions=result.actions, elapsed_s=end_to_end),
        "end_to_end_s": end_to_end,
        "total_cost_usd": cost,
        "browser_cost_usd": browser_cost,
        "evidence_scope": "Custom catalog task, not a public benchmark score. Real model run only if policy is jev/llm; small pilot cannot establish comparative capability.",
    }
    write_json(output / "manifest.json", manifest)
    write_json(output / "result.json", result.model_dump())
    write_json(output / "report.json", report)
    save_analysis(output, report)
    return report


def common(parser, *, demo):
    parser.add_argument(
        "--env-file", help="Explicit dotenv path; secrets are never copied to artifacts"
    )
    parser.add_argument("--use-text-model-planner", action="store_true")
    parser.add_argument(
        "--brain",
        choices=["codex", "api"],
        default="api",
        help="LongSeq brain backend; defaults to configured DeepSeek API (independent of Codex autoresearch)",
    )
    parser.add_argument("--codex-model", default="gpt-6-astra")
    parser.add_argument(
        "--codex-effort", choices=["low", "medium", "high", "xhigh", "max"], default="high"
    )
    parser.add_argument("--codex-timeout", type=float, default=120)
    parser.add_argument(
        "--backend",
        choices=["playwright", "browsergym"] if demo else ["playwright", "chrome"],
        default="playwright",
    )
    parser.add_argument("--mode", choices=["flat", "once", "event", "dynamic"], default="event")
    parser.add_argument(
        "--policy",
        choices=["rule", "jev", "llm"] if demo else ["jev", "llm"],
        default="rule" if demo else "jev",
    )
    parser.add_argument(
        "--planner", choices=["rule", "llm"] if demo else ["llm"], default="rule" if demo else "llm"
    )
    parser.add_argument("--output", default=f"runs/{time.time_ns()}")
    parser.add_argument("--max-actions", type=int, default=300)
    parser.add_argument("--max-seconds", type=float, default=300)
    parser.add_argument("--max-planner-calls", type=int, default=20)
    parser.add_argument("--max-feedback-calls", type=int, default=400)
    parser.add_argument("--tuning", help="Load an AgentTuning JSON profile (dynamic mode)")
    parser.add_argument(
        "--brain-interval",
        type=int,
        default=12,
        help="Maximum browser actions per LLM guidance window; 1 is per-step baseline",
    )
    parser.add_argument("--candidates", type=int, default=32)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument(
        "--live-preview",
        action="store_true",
        help="Publish inspector screenshots (not sent to models)",
    )
    parser.add_argument("--browser-hourly-cost", type=float)
    if demo:
        parser.add_argument("--records", type=int, default=12)
        parser.add_argument("--popup", action="store_true")
        parser.add_argument("--reorder", action="store_true")
        parser.add_argument("--injection", action="store_true")
        parser.add_argument("--delayed-save-ms", type=int, default=0)
        parser.add_argument("--lost-ack", action="store_true")


async def dispatch(args):
    if getattr(args, "env_file", None):
        load_env_file(args.env_file)
    if getattr(args, "use_text_model_planner", False) or getattr(args, "brain", None) == "api":
        use_text_model_for_planner()
    if args.command == "benchmark":
        from .benchmark import run_pilot

        return await run_pilot(args)
    if args.command == "autoresearch":
        from .autoresearch import run_research

        return await run_research(args)
    if args.command == "observe":
        root = Path(args.directory)
        paths = (
            [root]
            if (root / "report.json").exists() or (root / "model-calls.jsonl").exists()
            else sorted(p.parent for p in root.rglob("report.json"))
        )
        if not paths:
            raise ValueError("no reports or durable model-call logs found")
        for path in paths:
            analysis = save_analysis(path)
            print(
                json.dumps(
                    {
                        "run": str(path),
                        "quality": analysis["quality"],
                        "findings": [f["code"] for f in analysis["findings"]],
                    }
                )
            )
        return 0
    if args.command in {"demo", "run", "browse"}:
        report = await run_trial(args)
        print(json.dumps(report["result"], indent=2, ensure_ascii=False))
        print(f"Artifacts: {Path(args.output).resolve()}")
        return 0 if report["result"]["status"] == "success" else 1
    if args.command == "matrix":
        root = Path(args.output)
        reports = []
        for count in args.sizes:
            for mode in ["flat", "once", "event"]:
                for repeat in range(args.repeats):
                    args.mode = mode
                    report = await run_trial(
                        args, count=count, output=root / f"{mode}-{count}-{repeat}"
                    )
                    reports.append(report)
                    print(
                        f"{mode:5} n={count:3} repeat={repeat} strict={report['result']['strict_success']}",
                        flush=True,
                    )
        summary = summarize(reports)
        write_json(root / "summary.json", summary)
        print(json.dumps(summary, indent=2))
        return 0 if all(r["result"]["strict_success"] for r in reports) else 1
    if args.command == "report":
        reports = [json.loads(p.read_text()) for p in Path(args.directory).rglob("report.json")]
        print(json.dumps(summarize(reports), indent=2))
        return 0
    if args.command == "calibrate":
        print(json.dumps(calibration(json.loads(Path(args.labels).read_text())), indent=2))
        return 0


def main():
    local_browsers = Path(__file__).resolve().parents[2] / ".browsers"
    if local_browsers.exists():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(local_browsers))
    parser = argparse.ArgumentParser(
        description="Jev LongSeq browser controller and evaluation harness"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Run the offline visible-DOM catalog sandbox")
    demo.add_argument("--goal", help="Custom prompt for the catalog (dynamic mode; ungraded)")
    common(demo, demo=True)
    run = commands.add_parser("run", help="Run a trusted JSON task using real model adapters")
    run.add_argument("task")
    common(run, demo=False)
    browse = commands.add_parser("browse", help="Dynamic feedback loop from a goal and start URL")
    browse.add_argument("url", nargs="?", default="", help="Optional; infer from --goal if omitted")
    browse.add_argument("--goal", required=True)
    browse.add_argument("--allow-origin", action="append", default=[])
    common(browse, demo=False)
    browse.set_defaults(mode="dynamic", candidates=250, backend="chrome")
    matrix = commands.add_parser(
        "matrix", help="Repeat synthetic smoke tests across lengths and control modes"
    )
    common(matrix, demo=True)
    matrix.add_argument("--sizes", type=int, nargs="+", default=[4, 12, 32])
    matrix.add_argument("--repeats", type=int, default=1)
    benchmark = commands.add_parser("benchmark", help="Bounded pilot: one or two long tasks only")
    common(benchmark, demo=True)
    benchmark.set_defaults(backend="browsergym", policy="jev", planner="llm", max_planner_calls=4)
    benchmark.add_argument("--suite", choices=["catalog", "webarena", "public-web"], default="public-web")
    benchmark.add_argument("--task-ids", type=int, nargs="+", default=[50, 332])
    benchmark.add_argument("--sizes", type=int, nargs="+", default=[24, 32])
    benchmark.add_argument("--seed", type=int, default=0)
    report = commands.add_parser("report")
    report.add_argument("directory")
    observe = commands.add_parser("observe", help="Analyze completed or interrupted run telemetry")
    observe.add_argument("directory")
    research = commands.add_parser(
        "autoresearch", help="Bounded benchmark → diagnose → improve loop"
    )
    common(research, demo=True)
    research.set_defaults(
        mode="dynamic",
        policy="jev",
        planner="llm",
        records=12,
        candidates=64,
        max_actions=100,
        max_feedback_calls=24,
        max_seconds=240,
    )
    research.add_argument("--max-trials", type=int, default=2)
    research.add_argument("--suite", choices=["webarena", "catalog", "public-web"], default="public-web")
    research.add_argument("--task-ids", type=int, nargs="+", default=[50, 332])
    research.add_argument("--seed", type=int, default=0)
    research.add_argument("--study-seconds", type=float, default=1500)
    research.add_argument("--max-model-attempts", type=int, default=300)
    research.add_argument("--max-tokens", type=int, default=600000)
    research.add_argument("--max-cost-usd", type=float)
    research.add_argument("--metric", choices=["tokens", "latency", "cost"], default="tokens")
    research.add_argument("--min-improvement", type=float, default=0.05)
    research.add_argument("--max-latency-regression", type=float, default=0.10)
    research.add_argument("--max-token-regression", type=float, default=0.10)
    research.add_argument("--resume", action="store_true")
    calibrate = commands.add_parser("calibrate")
    calibrate.add_argument("labels")
    args = parser.parse_args()
    if args.command == "matrix" and args.repeats < 1:
        parser.error("--repeats must be positive")
    if (
        hasattr(args, "browser_hourly_cost")
        and args.browser_hourly_cost is not None
        and args.browser_hourly_cost < 0
    ):
        parser.error("browser hourly cost must be nonnegative")
    try:
        return asyncio.run(dispatch(args))
    except (ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
