"""Small pilot runner; explicit preflight and no substitution for unavailable suites."""

from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path

import httpx

from .protocol import digest, now


def webarena_preflight(task_ids: list[int]) -> dict:
    if not 1 <= len(task_ids) <= 2:
        raise ValueError("pilot must contain one or two tasks")
    dist = importlib.metadata.distribution("libwebarena")
    source = Path(dist.locate_file("webarena/test.raw.json"))
    configs = json.loads(source.read_text())
    selected = []
    for task_id in task_ids:
        config = next((c for c in configs if c["task_id"] == task_id), None)
        if config is None:
            raise ValueError(f"unknown official WebArena task: {task_id}")
        # Do not serialize eval/reference answers into anything given to the agent.
        selected.append({"id": task_id, "intent": config["intent"], "sites": config["sites"]})
    required = [
        "WA_SHOPPING",
        "WA_SHOPPING_ADMIN",
        "WA_REDDIT",
        "WA_GITLAB",
        "WA_WIKIPEDIA",
        "WA_MAP",
        "WA_HOMEPAGE",
    ]
    return {
        "checked_at": now(),
        "suite": "webarena",
        "browsergym": importlib.metadata.version("browsergym-core"),
        "libwebarena": dist.version,
        "task_manifest_sha256": digest(source.read_text()),
        "selected_tasks": selected,
        "missing_environment": [k for k in required if not os.environ.get(k)],
        "model_keys_present": {
            k: bool(os.environ.get(k)) for k in ("TYPESAFE_API_KEY", "PLANNER_API_KEY")
        },
    }


async def check_webarena(task_ids):
    report = webarena_preflight(task_ids)
    report["unreachable_sites"] = []
    if not report["missing_environment"]:
        async with httpx.AsyncClient(timeout=10) as client:
            for site in sorted({s for task in report["selected_tasks"] for s in task["sites"]}):
                try:
                    response = await client.get(os.environ[f"WA_{site.upper()}"])
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    report["unreachable_sites"].append({"site": site, "error": type(exc).__name__})
    report["status"] = (
        "blocked"
        if (
            report["missing_environment"]
            or report["unreachable_sites"]
            or not all(report["model_keys_present"].values())
        )
        else "ready"
    )
    report["reason"] = (
        "WebArena sites or model configuration unavailable; no model calls started."
        if report["status"] == "blocked"
        else "WebArena configuration and selected sites ready."
    )
    return report


async def run_pilot(args):
    from .cli import run_trial, write_json
    from .evaluation import summarize

    if args.suite != "saas-bench" and (getattr(args, "preflight_only", False)
                                       or getattr(args, "environment_only", False)):
        raise ValueError("--preflight-only and --environment-only require --suite saas-bench")
    root = Path(args.output)
    if root.exists() and any(root.iterdir()):
        raise ValueError("pilot output directory must be empty")
    root.mkdir(parents=True, exist_ok=True)
    if args.suite == "saas-bench":
        from .saas_benchmark import check_saas, run_saas

        report = await check_saas(args)
        write_json(root / "preflight.json", report)
        if report["status"] != "ready" or getattr(args, "preflight_only", False):
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["status"] == "ready" else 2
        reports = [await run_saas(args, task, root / f"saas-bench-{task['id']}")
                   for task in report["selected_tasks"]]
    elif args.suite in {"webarena", "public-web"}:
        from .public_web import check_public_web, run_public_web

        check = check_public_web if args.suite == "public-web" else check_webarena
        report = await check(args.task_ids)
        if report["status"] == "blocked":
            write_json(root / "preflight.json", report)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 2
        report["status"] = "ready"
        write_json(root / "preflight.json", report)
        from .public_benchmark import run_webarena

        runner = run_public_web if args.suite == "public-web" else run_webarena
        reports = [
            await runner(args, task, root / f"{args.suite}-{task['id']}")
            for task in report["selected_tasks"]
        ]
    else:
        if not 1 <= len(args.sizes) <= 2 or any(n < 20 or n > 100 for n in args.sizes):
            raise ValueError("catalog pilot allows 1–2 tasks, each with 20–100 records")
        reports = []
        for i, size in enumerate(args.sizes):
            reports.append(await run_trial(args, count=size, output=root / f"catalog-{size}-{i}"))
            print(json.dumps(reports[-1]["result"], ensure_ascii=False), flush=True)
    summary = summarize(reports)
    summary["scope"] = "one/two-task pilot only; no estimate of aggregate benchmark performance"
    if args.suite == "saas-bench" and getattr(args, "environment_only", False):
        summary["scope"] = "Environment smoke only; no agent performance measured"
        write_json(root / "summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if all(r["grade"]["data_valid"] and not r["environment"]["cleanup_error"]
                        for r in reports) else 2
    write_json(root / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.suite == "saas-bench" and any(r["grade"].get("data_valid") is False for r in reports):
        return 2
    return 0 if all(r["result"]["strict_success"] for r in reports) else 1
