from __future__ import annotations

import random
from collections import defaultdict
from statistics import mean


def efficiency_profile(ledger: list[dict], *, actions: int, elapsed_s: float) -> dict:
    """Measured usage only. Failed requests and missing prices never become free calls."""
    groups = defaultdict(list)
    for record in ledger:
        kind = record["kind"]
        group = (
            "brain"
            if kind in {"dynamic_feedback", "dynamic_finish"}
            else "input_helper"
            if kind == "dynamic_input"
            else "jev"
            if kind == "jev"
            else kind
        )
        groups[group].append(record)

    def totals(records):
        costs = [r.get("cost_usd") for r in records]
        return {
            "attempts": len(records),
            "http_attempts": sum(r.get("transport", "http") == "http" for r in records),
            "codex_invocations": sum(r.get("transport") == "codex_cli" for r in records),
            "successful_responses": sum(
                not r.get("error") and (r.get("success") is True or 200 <= r.get("status", 0) < 300)
                for r in records
            ),
            "known_input_tokens": sum(r.get("input_tokens") or 0 for r in records),
            "known_output_tokens": sum(r.get("output_tokens") or 0 for r in records),
            "unknown_usage_attempts": sum(
                r.get("input_tokens") is None or r.get("output_tokens") is None for r in records
            ),
            "request_time_s": sum(r.get("latency_s", 0) for r in records),
            "cost_usd": sum(costs) if all(c is not None for c in costs) else None,
        }

    result = {"by_component": {k: totals(v) for k, v in groups.items()}, "total": totals(ledger)}
    brain_calls = result["by_component"].get("brain", {}).get("successful_responses", 0)
    result.update(
        end_to_end_s=elapsed_s,
        browser_actions=actions,
        actions_per_brain_response=actions / brain_calls if brain_calls else None,
        known_input_tokens_per_action=result["total"]["known_input_tokens"] / actions
        if actions
        else None,
        seconds_per_action=elapsed_s / actions if actions else None,
    )
    return result


def quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * q
    lower = int(index)
    return ordered[lower] + (ordered[min(lower + 1, len(ordered) - 1)] - ordered[lower]) * (
        index - lower
    )


def summarize(runs: list[dict]) -> dict:
    if not runs:
        raise ValueError("no runs supplied")
    evaluated = [r for r in runs if r["result"].get("strict_success") is not None]
    success = sum(r["result"]["strict_success"] is True for r in evaluated)
    costs = [r.get("total_cost_usd") for r in runs]
    known = all(v is not None for v in costs)
    total_cost = sum(costs) if known else None
    finishes = sum(r["result"]["finish_requests"] for r in runs)
    false = sum(r["result"]["false_completions"] for r in runs)
    by_status = defaultdict(list)
    for run in runs:
        by_status[run["result"]["status"]].append(
            run.get("end_to_end_s", run["result"]["elapsed_s"])
        )
    return {
        "runs": len(runs),
        "graded_runs": len(evaluated),
        "strict_successes": success,
        "strict_success_rate": success / len(evaluated) if evaluated else None,
        "total_cost_usd": total_cost,
        "cost_per_success_usd": total_cost / success
        if known and success and len(evaluated) == len(runs)
        else None,
        "unknown_cost_runs": sum(v is None for v in costs),
        "false_completion_rate": false / finishes if finishes else None,
        "tasks_with_false_completion": sum(r["result"]["false_completions"] > 0 for r in runs)
        / len(runs),
        "constraint_violations": sum(
            len(r["result"]["violations"]) + r.get("grade", {}).get("violations", 0) for r in runs
        ),
        "duplicate_writes": sum(r.get("grade", {}).get("duplicates", 0) for r in runs),
        "latency_s": {
            s: {"p50": quantile(v, 0.5), "p95": quantile(v, 0.95)} for s, v in by_status.items()
        },
        "mean_atomic_actions": mean(r["result"]["actions"] for r in runs),
        "planner_calls": sum(r["result"]["planner_calls"] for r in runs),
    }


def paired_bootstrap(
    left: dict[str, list[bool]], right: dict[str, list[bool]], *, samples: int = 2000, seed: int = 0
) -> dict:
    """Cluster by task, not by independent-looking repeated trajectories."""
    if set(left) != set(right) or not left or samples < 1:
        raise ValueError("paired comparison requires identical non-empty task sets and samples > 0")
    if any(not values for values in [*left.values(), *right.values()]):
        raise ValueError("each task needs at least one run")
    deltas = [mean(left[k]) - mean(right[k]) for k in sorted(left)]
    rng = random.Random(seed)
    bootstrap = [mean(rng.choices(deltas, k=len(deltas))) for _ in range(samples)]
    return {
        "difference": mean(deltas),
        "ci95": [quantile(bootstrap, 0.025), quantile(bootstrap, 0.975)],
        "tasks": len(deltas),
        "samples": samples,
        "seed": seed,
    }


def calibration(labels: list[dict], bins: int = 10) -> dict:
    """Requires independently labelled action outcomes; never invent labels from confidence."""
    if not labels or bins < 1:
        raise ValueError("non-empty independently labelled samples and bins > 0 are required")
    for item in labels:
        if not isinstance(item["success"], bool) or not 0 <= item["confidence"] <= 1:
            raise ValueError("invalid confidence or independent outcome label")
    brier = mean((x["confidence"] - x["success"]) ** 2 for x in labels)
    ece = 0.0
    for number in range(bins):
        group = [x for x in labels if min(int(x["confidence"] * bins), bins - 1) == number]
        if group:
            ece += (
                len(group)
                / len(labels)
                * abs(mean(x["confidence"] for x in group) - mean(x["success"] for x in group))
            )
    curve = []
    for threshold in sorted({0.0, 1.0, *(x["confidence"] for x in labels)}):
        covered = [x for x in labels if x["confidence"] >= threshold]
        curve.append(
            {
                "threshold": threshold,
                "coverage": len(covered) / len(labels),
                "risk": 1 - mean(x["success"] for x in covered) if covered else None,
            }
        )
    return {"n": len(labels), "brier": brier, "ece": ece, "risk_coverage": curve}


def candidate_recall(candidate_sets: list[set[str]], valid_sets: list[set[str]]) -> float:
    if len(candidate_sets) != len(valid_sets) or not valid_sets or any(not s for s in valid_sets):
        raise ValueError("aligned independent, non-empty correct-action sets are required")
    return mean(bool(a & b) for a, b in zip(candidate_sets, valid_sets, strict=True))


def pass_four(groups: dict[str, list[bool]]) -> float:
    if not groups or any(len(values) != 4 for values in groups.values()):
        raise ValueError("pass^4 requires exactly four independent runs per task")
    return mean(all(values) for values in groups.values())
