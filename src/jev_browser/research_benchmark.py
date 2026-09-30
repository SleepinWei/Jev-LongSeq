"""Paired public task trials; official per-task outcomes remain visible."""

from __future__ import annotations

from .evaluation import efficiency_profile
from .observability import write_json
from .protocol import digest


def aggregate(reports):
    first = reports[0]
    calls = [call for report in reports for call in report["model_calls"]]
    results = [r["result"] for r in reports]
    strict = (None if any(r.get("strict_success") is None for r in results)
              else all(r.get("strict_success") is True for r in results))
    elapsed = sum(r["end_to_end_s"] for r in reports)
    actions = sum(r["actions"] for r in results)
    return {
        "manifest": {
            **first["manifest"],
            "task_id": f"{first['manifest'].get('suite', 'webarena')}-pair",
            "task_ids": [r["manifest"]["task_id"] for r in reports],
            "task_hash": digest([r["manifest"]["task_hash"] for r in reports]),
            "fixture_hash": digest([r["manifest"]["fixture_hash"] for r in reports]),
        },
        "result": {
            "status": "success" if strict else "failed",
            "strict_success": strict,
            "reason": "independently graded paired task results",
            "actions": actions,
            "elapsed_s": elapsed,
            "passed_tasks": sum(r.get("strict_success") is True for r in results),
            "total_tasks": len(results),
            "violations": [v for r in results for v in r.get("violations", [])],
            "false_completions": sum(r.get("false_completions", 0) for r in results),
        },
        "grade": {"strict_success": strict,
                  "data_valid": all(r.get("grade", {}).get("data_valid", True) for r in reports)},
        "model_calls": calls,
        "efficiency": efficiency_profile(calls, actions=actions, elapsed_s=elapsed),
        "end_to_end_s": elapsed,
        "total_cost_usd": sum(r["total_cost_usd"] for r in reports)
        if all(r.get("total_cost_usd") is not None for r in reports)
        else None,
        "task_reports": reports,
    }


async def run_pair(args, *, count, output):
    from .public_benchmark import run_webarena
    from .public_web import run_public_web
    from .saas_benchmark import run_saas

    suite = getattr(args, "suite", "webarena")
    runner = {"public-web": run_public_web, "webarena": run_webarena,
              "saas-bench": run_saas}[suite]

    output.mkdir(parents=True, exist_ok=False)
    reports = []
    for selected in args._selected_tasks:
        write_json(
            output / "progress.json",
            {
                "phase": "benchmark",
                "active_task": selected["id"],
                "completed_tasks": len(reports),
                "total_tasks": len(args._selected_tasks),
            },
        )
        report = await runner(args, selected, output / f"{suite}-{selected['id']}")
        reports.append(report)
    report = aggregate(reports)
    write_json(output / "manifest.json", report["manifest"])
    write_json(output / "report.json", report)
    write_json(
        output / "progress.json",
        {
            "phase": "complete",
            "completed_tasks": len(reports),
            "total_tasks": len(reports),
            "active_task": None,
        },
    )
    return report
