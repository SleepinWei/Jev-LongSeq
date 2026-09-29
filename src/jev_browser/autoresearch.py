"""Bounded, resumable empirical search over the LLM/Jev agent's efficiency controls."""

from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import time
from pathlib import Path

from .observability import Observer, ResourceLimit, save_analysis, write_json
from .protocol import AgentTuning, digest, now
from .research_evidence import experiment_memory, save_conclusions
from .researcher import CodexResearcher


class StudyGate:
    """Check between model attempts. In-flight tokens/cost can exceed a threshold once."""

    def __init__(
        self, path, *, seconds, attempts, tokens, cost=None, previous=None, browser_hourly_cost=0
    ):
        self.path, self.started = path, time.monotonic()
        self.seconds, self.attempt_limit, self.token_limit, self.cost_limit = (
            seconds,
            attempts,
            tokens,
            cost,
        )
        self.browser_hourly_cost = browser_hourly_cost or 0
        previous = previous or {}
        self.prior_seconds = previous.get("elapsed_s", 0)
        self.used = {
            "attempts": previous.get("attempts", 0),
            "completed_attempts": previous.get("completed_attempts", 0),
            "known_tokens": previous.get("known_tokens", 0),
            "known_cost_usd": previous.get("known_cost_usd", 0.0),
            "unknown_usage_attempts": previous.get("unknown_usage_attempts", 0),
            "unknown_cost_attempts": previous.get("unknown_cost_attempts", 0),
        }

    @property
    def elapsed(self):
        return self.prior_seconds + time.monotonic() - self.started

    @property
    def remaining_seconds(self):
        return max(0, self.seconds - self.elapsed)

    def save(self):
        value = {
            **self.used,
            "elapsed_s": self.elapsed,
            "updated_at": now(),
            "browser_cost_upper_bound_usd": self.elapsed / 3600 * self.browser_hourly_cost,
        }
        write_json(self.path, value)
        return value

    def check(self):
        if self.remaining_seconds <= 0:
            raise ResourceLimit("study wall-clock budget reached")
        if self.used["attempts"] >= self.attempt_limit:
            raise ResourceLimit("study model-attempt budget reached")
        if self.used["known_tokens"] >= self.token_limit:
            raise ResourceLimit("study known-token threshold reached")
        if self.cost_limit is not None:
            if self.used["unknown_cost_attempts"]:
                raise ResourceLimit("study cost became unknown; stop before spending more")
            upper = self.used["known_cost_usd"] + self.elapsed / 3600 * self.browser_hourly_cost
            if upper >= self.cost_limit:
                raise ResourceLimit("study model/browser cost threshold reached")

    def before_call(self):
        self.check()
        self.used["attempts"] += 1
        self.save()  # dispatch is accounted for even if the process is killed

    def record(self, record):
        self.used["completed_attempts"] += 1
        incoming, outgoing = record.get("input_tokens"), record.get("output_tokens")
        self.used["known_tokens"] += (incoming or 0) + (outgoing or 0)
        self.used["unknown_usage_attempts"] += incoming is None or outgoing is None
        cost = record.get("cost_usd")
        self.used["unknown_cost_attempts"] += cost is None
        self.used["known_cost_usd"] += cost or 0
        self.save()


def proposal(profile: AgentTuning, analysis: dict, tried: set[str]) -> dict | None:
    """One falsifiable change at a time; no benchmark answers enter agent configuration."""
    codes = {f["code"] for f in analysis["findings"]}
    if codes & {"transport_instability", "setup_failure"}:
        return None
    options = []
    if codes & {"incomplete_handoff", "false_completion"}:
        options.append(
            ("prompt_variant", "coverage", "Explicit coverage guidance may improve handoff.")
        )
    if "schema_repair" in codes:
        options.append(
            ("prompt_variant", "compact", "Less redundant output may reduce schema repairs.")
        )
    if "brain_latency" in codes:
        options.extend(
            [
                (
                    "brain_interval",
                    min(48, max(profile.brain_interval + 1, profile.brain_interval * 2)),
                    "A longer autonomous Jev window may reduce slow LLM requests.",
                ),
                (
                    "prompt_variant",
                    "compact",
                    "Shorter LLM responses may reduce latency and output tokens.",
                ),
            ]
        )
    if "context_volume" in codes:
        options.extend(
            [
                (
                    "recent_evidence",
                    max(1, profile.recent_evidence - 1),
                    "A smaller recent-evidence window may reduce repeated Jev input tokens.",
                ),
                (
                    "excerpt_chars",
                    max(600, profile.excerpt_chars - 600),
                    "Shorter visible excerpts may reduce context cost while keeping source archives.",
                ),
            ]
        )
    options.extend(
        [
            ("prompt_variant", "compact", "Test concise guidance with the same task and verifier."),
            (
                "prompt_variant",
                "coverage",
                "Test explicit coverage summaries without preset task rules.",
            ),
            (
                "brain_interval",
                max(1, profile.brain_interval // 2),
                "Earlier feedback may prevent wasteful local-policy loops.",
            ),
        ]
    )
    for field, value, hypothesis in options:
        if getattr(profile, field) == value:
            continue
        candidate = AgentTuning.model_validate({**profile.model_dump(), field: value})
        if digest(candidate.model_dump()) in tried:
            continue
        return {
            "profile": candidate.model_dump(),
            "change": {"field": field, "before": getattr(profile, field), "after": value},
            "hypothesis": hypothesis,
            "evidence_codes": sorted(codes),
        }
    return None


def compatible(base, candidate):
    left, right = base["manifest"], candidate["manifest"]
    for key in (
        "code_hash",
        "fixture_hash",
        "task_hash",
        "policy",
        "backend",
        "live_preview",
        "brain_backend",
        "codex_model",
        "codex_effort",
        "codex_timeout",
    ):
        if left.get(key) != right.get(key) or (key.endswith("hash") and not left.get(key)):
            return False
    for key in (
        "max_actions",
        "max_cycles",
        "max_feedback_calls",
        "candidate_limit",
        "max_seconds",
    ):
        if left["budget"].get(key) != right["budget"].get(key):
            return False

    def models(report):
        return sorted(
            {
                (c["kind"].split("_")[0], c.get("resolved_model"))
                for c in report["model_calls"]
                if c.get("resolved_model")
            }
        )

    return models(base) == models(candidate)


def compare(base, candidate, *, metric, min_improvement, latency_regression, token_regression):
    def verdict(decision, reason, **extra):
        return {"decision": decision, "reason": reason, **extra}

    if not compatible(base, candidate):
        return verdict("inconclusive", "task, code, model, backend or non-tunable budgets differ")
    if base.get("task_reports") or candidate.get("task_reports"):
        left = {r["manifest"]["task_id"]: r for r in base.get("task_reports", [])}
        right = {r["manifest"]["task_id"]: r for r in candidate.get("task_reports", [])}
        if not left or left.keys() != right.keys():
            return verdict("inconclusive", "paired task IDs differ")
        for task_id, prior in left.items():
            current = right[task_id]
            if not compatible(prior, current):
                return verdict("inconclusive", f"task {task_id} settings differ")
            if prior["result"].get("strict_success"):
                decision = compare(
                    prior,
                    current,
                    metric=metric,
                    min_improvement=-1,
                    latency_regression=latency_regression,
                    token_regression=token_regression,
                )
                if decision["decision"] != "keep":
                    return verdict(decision["decision"], f"task {task_id}: {decision['reason']}")
    for report in (base, candidate):
        if report.get("grade", {}).get("data_valid") is False:
            return verdict("inconclusive", "public dataset changed or could not be verified")
        if any(c.get("error") or c.get("status", 200) >= 400 for c in report["model_calls"]):
            return verdict("inconclusive", "transport failures confound this small comparison")
    quality = candidate["result"]
    if (
        quality.get("strict_success") is not True
        or quality.get("violations")
        or candidate.get("grade", {}).get("duplicates", 0)
        or candidate.get("grade", {}).get("violations", 0)
    ):
        return verdict(
            "reject", "candidate did not strictly complete without violations/duplicates"
        )

    def usage(report):
        rows = report["model_calls"]
        if not rows or any(
            r.get("input_tokens") is None or r.get("output_tokens") is None for r in rows
        ):
            return None
        return sum(r["input_tokens"] + r["output_tokens"] for r in rows)

    before_tokens, after_tokens = usage(base), usage(candidate)
    if before_tokens is None or after_tokens is None:
        return verdict("inconclusive", "unknown token usage prevents efficiency/regression checks")
    if metric == "cost" and any(r.get("total_cost_usd") is None for r in (base, candidate)):
        return verdict("inconclusive", "unknown dollar cost prevents cost comparison")
    if base["result"].get("strict_success") is not True:
        return verdict(
            "keep",
            "candidate restores strict task completion",
            validation="two_task_provisional"
            if base.get("task_reports")
            else "single_task_provisional",
        )
    if candidate["end_to_end_s"] > base["end_to_end_s"] * (1 + latency_regression):
        return verdict("reject", "latency regression exceeds tolerance")
    if after_tokens > before_tokens * (1 + token_regression):
        return verdict("reject", "token regression exceeds tolerance")
    if metric == "tokens":
        before, after = before_tokens, after_tokens
    elif metric == "latency":
        before, after = base["end_to_end_s"], candidate["end_to_end_s"]
    else:
        before, after = base.get("total_cost_usd"), candidate.get("total_cost_usd")
    if before is None or after is None or before <= 0:
        return verdict("inconclusive", "objective metric is missing or has a zero baseline")
    gain = (before - after) / before
    return verdict(
        "keep" if gain >= min_improvement else "reject",
        "measured improvement" if gain >= min_improvement else "improvement below threshold",
        metric=metric,
        before=before,
        after=after,
        relative_improvement=gain,
        validation="two_task_provisional"
        if base.get("task_reports")
        else "single_task_provisional",
    )


def experiment_config(args):
    keys = (
        "records",
        "backend",
        "policy",
        "mode",
        "planner",
        "brain",
        "codex_model",
        "codex_effort",
        "codex_timeout",
        "max_actions",
        "max_seconds",
        "max_feedback_calls",
        "candidates",
        "popup",
        "injection",
        "reorder",
        "lost_ack",
        "delayed_save_ms",
        "live_preview",
        "metric",
        "min_improvement",
        "max_latency_regression",
        "max_token_regression",
        "browser_hourly_cost",
    )
    return {
        **{k: getattr(args, k) for k in keys},
        "suite": getattr(args, "suite", "catalog"),
        "task_ids": getattr(args, "task_ids", []),
        "seed": getattr(args, "seed", 0),
        "researcher": {
            "backend": "codex_cli",
            "model": args.codex_model,
            "reasoning_effort": args.codex_effort,
            "timeout_s": args.codex_timeout,
        },
        "initial_profile": args._tuning.model_dump(),
        "code_hash": digest(
            {p.name: digest(p.read_text()) for p in Path(__file__).parent.glob("*.py")}
        ),
        "models": {k: os.environ.get(k) for k in ("TYPESAFE_MODEL", "PLANNER_MODEL")},
        "model_settings_hash": digest(
            {
                k: os.environ.get(k)
                for k in (
                    "TYPESAFE_ENDPOINT",
                    "PLANNER_ENDPOINT",
                    "TYPESAFE_TIMEOUT_SECONDS",
                    "PLANNER_TIMEOUT_SECONDS",
                    "TYPESAFE_INPUT_PER_MILLION",
                    "TYPESAFE_OUTPUT_PER_MILLION",
                    "PLANNER_INPUT_PER_MILLION",
                    "PLANNER_OUTPUT_PER_MILLION",
                )
            }
        ),
    }


async def run_research(args, *, runner=None):
    from .cli import resolve_tuning, run_trial
    from .config import use_text_model_for_planner

    if args.brain == "api":
        use_text_model_for_planner()

    suite = getattr(args, "suite", "catalog")
    public = suite in {"webarena", "public-web"}
    if suite == "public-web":
        if len(args.task_ids) != 2 or set(args.task_ids) != {50, 332}:
            raise ValueError("public-web requires the two task types 50 and 332")
        args.backend = "playwright"
    if suite == "webarena":
        if len(args.task_ids) != 2 or len(set(args.task_ids)) != 2:
            raise ValueError("WebArena autoresearch requires exactly two distinct task IDs")
        if not set(args.task_ids) <= {47, 48, 49, 50, 51, 332, 333}:
            raise ValueError("choose two reviewed read-only order tasks")
        args.backend = "browsergym"

    if args.mode != "dynamic" or args.policy != "jev":
        raise ValueError("autoresearch evaluates the dynamic LLM/Jev loop")
    if not (1 <= args.max_trials <= 20 and 1 <= args.records <= 100):
        raise ValueError("max-trials must be 1–20 and records 1–100")
    if min(args.study_seconds, args.max_model_attempts, args.max_tokens, args.max_seconds) <= 0:
        raise ValueError("research budgets must be positive")
    if not all(
        math.isfinite(v)
        for v in (
            args.study_seconds,
            args.max_seconds,
            args.min_improvement,
            args.max_latency_regression,
            args.max_token_regression,
        )
    ):
        raise ValueError("research budgets and thresholds must be finite")
    if not (
        0 < args.min_improvement < 1
        and 0 <= args.max_latency_regression < 1
        and 0 <= args.max_token_regression < 1
    ):
        raise ValueError("invalid improvement/regression thresholds")
    if args.max_cost_usd is not None or args.metric == "cost":
        raise ValueError(
            "Codex researcher subscription calls have no measured dollar price; use tokens or latency"
        )
    root = Path(args.output)
    resolve_tuning(args)
    config = experiment_config(args)
    if args.resume:
        state = json.loads((root / "study.json").read_text())
        fresh_retry = not state["trials"] and state.get("phase") == "preflight"
        if state["config"] != config and not fresh_retry:
            raise ValueError("resume requires the same code, models, task and comparison settings")
        state["config"] = config
        previous_budget = json.loads((root / "budget.json").read_text())
        if any(t["status"] == "running" for t in state["trials"]):
            raise ValueError(
                "interrupted trial needs inspection; it will not be replayed or promoted"
            )
    else:
        if root.exists() and any(root.iterdir()):
            raise ValueError("research output must be empty; use --resume for an existing study")
        root.mkdir(parents=True, exist_ok=True)
        initial = resolve_tuning(args)
        state = {
            "version": 1,
            "started_at": now(),
            "config": config,
            "trials": [],
            "incumbent": None,
            "best_profile": initial.model_dump(),
            "best_validated": False,
            "next_proposal": None,
            "status": "running",
        }
        previous_budget = None
    state.update(status="running", phase="preflight", updated_at=now(), pid=os.getpid())
    state["limits"] = {
        k: getattr(args, k)
        for k in ("max_trials", "study_seconds", "max_seconds", "max_model_attempts", "max_tokens")
    }
    write_json(root / "study.json", state)
    if public:
        from .benchmark import check_webarena
        from .public_web import check_public_web
        from .research_benchmark import run_pair

        check = check_public_web if suite == "public-web" else check_webarena
        preflight = await check(args.task_ids)
        write_json(root / "preflight.json", preflight)
        state["preflight"] = preflight
        state["tasks"] = preflight["selected_tasks"]
        if preflight["status"] != "ready":
            state.update(status="blocked", stop_reason=f"{suite}_unavailable", updated_at=now())
            write_json(root / "study.json", state)
            write_json(root / "budget.json", previous_budget or {})
            save_conclusions(root, {**state, "budget": previous_budget or {}}, {})
            print(f"Research blocked by {suite} preflight: {root.resolve()}")
            return 2
        args._selected_tasks = preflight["selected_tasks"]
        runner = runner or run_pair
    trial_seconds = args.max_seconds * (2 if public else 1)
    gate = StudyGate(
        root / "budget.json",
        seconds=args.study_seconds,
        attempts=args.max_model_attempts,
        tokens=args.max_tokens,
        cost=args.max_cost_usd,
        previous=previous_budget,
        browser_hourly_cost=args.browser_hourly_cost,
    )
    runner = runner or run_trial
    research_observer = Observer(root / "researcher", gate=gate)
    researcher = CodexResearcher(args, research_observer)
    reports = {
        t["id"]: json.loads((root / t["directory"] / "report.json").read_text())
        for t in state["trials"]
        if t["status"] == "complete"
    }
    analyses = {t["id"]: save_analysis(root / t["directory"], reports[t["id"]])
                for t in state["trials"] if t["status"] == "complete"}
    tried = {digest(t["profile"]) for t in state["trials"]}
    stop = "trial_budget"
    gate.save()
    try:
        for index in range(len(state["trials"]), args.max_trials):
            gate.check()
            if gate.remaining_seconds < trial_seconds:
                stop = "insufficient_time_for_a_comparable_trial"
                break
            change = state["next_proposal"]
            if index and change is None:
                state.update(phase="analyzing", updated_at=now())
                write_json(root / "study.json", state)
                incumbent_analysis = analyses[state["incumbent"]]
                history, evidence = experiment_memory(state["trials"], analyses, state["incumbent"])
                observations = {**incumbent_analysis, "evidence": evidence}
                analysis_record = {"trial_index": index, "incumbent": state["incumbent"],
                                   "status": "analyzing", "history": history, "evidence": evidence}
                analysis_path = root / "researcher" / f"analysis-{index:03}.json"
                write_json(analysis_path, analysis_record)
                research_observer.context.update(cycle=index, phase="research")
                try:
                    async with asyncio.timeout(gate.remaining_seconds):
                        change = await research_observer.measure(
                            "research.propose",
                            researcher.propose,
                            AgentTuning.model_validate(state["best_profile"]),
                            observations,
                            tried,
                            history,
                        )
                except ResourceLimit:
                    raise
                except Exception as exc:
                    stop = "researcher_failed"
                    state["researcher_error"] = type(exc).__name__
                    state["last_advice"] = {"decision": "error", "hypothesis": "分析器未返回有效提案，保留原配置。"}
                    write_json(analysis_path, {**analysis_record, "status": "failed",
                                               "error_type": type(exc).__name__})
                    break
                state["last_advice"] = getattr(researcher, "last_advice", None) or {
                    "decision": "try" if change else "stop",
                    "hypothesis": (change or {}).get("hypothesis", "没有受现有证据支持的未尝试配置。")}
                write_json(analysis_path, {**analysis_record, "status": "complete",
                                           "advice": state["last_advice"]})
                state["next_proposal"] = change
                write_json(root / "study.json", state)
                if change is None:
                    stop = "no_supported_untried_improvement"
                    break
                gate.check()
                if gate.remaining_seconds < trial_seconds:
                    stop = "insufficient_time_for_a_comparable_trial"
                    break
            profile = AgentTuning.model_validate(
                change["profile"] if change else state["best_profile"]
            )
            trial = {
                "id": index,
                "directory": f"trial-{index:03}",
                "status": "running",
                "profile": profile.model_dump(),
                "proposal": change,
            }
            state["trials"].append(trial)
            state["status"] = "running"
            state.update(phase="benchmark", active_trial=index, updated_at=now())
            write_json(root / "study.json", state)
            trial_args = copy.copy(args)
            trial_args.command, trial_args.mode = "benchmark" if public else "demo", "dynamic"
            trial_args._tuning, trial_args._research_gate = profile, gate
            output = root / trial["directory"]
            print(f"Trial {index + 1}/{args.max_trials}: {profile.model_dump()}", flush=True)
            report = await runner(trial_args, count=args.records, output=output)
            analysis = save_analysis(output, report)
            analyses[index] = analysis
            state.update(phase="evaluating", updated_at=now())
            write_json(root / "study.json", state)
            trial.update(status="complete", analysis="observability.json", result=report["result"])
            reports[index] = report
            tried.add(digest(profile.model_dump()))
            if state["incumbent"] is None:
                selection = {"decision": "baseline", "reason": "initial measured configuration"}
                state["incumbent"] = index
                state["best_validated"] = report["result"].get("strict_success") is True
            else:
                selection = compare(
                    reports[state["incumbent"]],
                    report,
                    metric=args.metric,
                    min_improvement=args.min_improvement,
                    latency_regression=args.max_latency_regression,
                    token_regression=args.max_token_regression,
                )
                if selection["decision"] == "keep":
                    state.update(
                        incumbent=index, best_profile=profile.model_dump(), best_validated=True
                    )
            trial["selection"] = selection
            write_json(output / "selection.json", selection)
            state["next_proposal"] = None
            if state["best_validated"]:
                write_json(root / "best-profile.json", state["best_profile"])
            else:
                write_json(root / "baseline-profile.json", state["best_profile"])
            write_json(root / "study.json", state)
            save_conclusions(root, {**state, "budget": gate.save()}, analyses)
            print(
                f"  {selection['decision']}: {selection['reason']}; status={report['result']['status']}",
                flush=True,
            )
            if report.get("grade", {}).get("data_valid") is False:
                stop = "public_data_unavailable"
                break
            if analysis["failures"] or selection["decision"] == "inconclusive":
                stop = "transport_instability" if analysis["failures"] else "incomparable_trial"
                break
    except ResourceLimit as exc:
        stop = str(exc)
    except BaseException:
        stop = "interrupted_or_runner_error"
        raise
    finally:
        await researcher.transport.aclose()
        save_analysis(root / "researcher")
        state.update(
            status="stopped",
            phase="finished",
            stop_reason=stop,
            budget=gate.save(),
            updated_at=now(),
        )
        write_json(root / "study.json", state)
        if state["next_proposal"]:
            write_json(root / "next-proposal.json", state["next_proposal"])
        else:
            (root / "next-proposal.json").unlink(missing_ok=True)
        save_conclusions(root, state, analyses)
    print(f"Research artifacts: {root.resolve()}")
    return 0 if state["best_validated"] else 1
