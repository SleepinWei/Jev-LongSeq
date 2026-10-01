from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Protocol

from .candidates import authorize, generate, validate_plan
from .memory import Memory, all_checks, check, visible_fields
from .models import InvalidPlanOutput
from .observability import Observer, ResourceLimit
from .protocol import (
    Action,
    Budget,
    Contract,
    Observation,
    Operation,
    Plan,
    RunResult,
    Task,
    digest,
    now,
)


class Backend(Protocol):
    async def observe(self) -> Observation: ...
    async def execute(self, action: Action): ...


class Controller:
    def __init__(
        self,
        task: Task,
        backend: Backend,
        policy,
        *,
        planner=None,
        mode: str = "event",
        budget: Budget | None = None,
        output: Path | None = None,
        observer: Observer | None = None,
    ):
        if type(self) is Controller and task.control_mode == "dynamic":
            raise ValueError("dynamic tasks require DynamicController and semantic feedback")
        if mode not in {"flat", "once", "event"}:
            raise ValueError("mode must be flat, once, or event")
        if mode != "flat" and planner is None:
            raise ValueError("hierarchical control requires a planner")
        self.task, self.backend, self.policy, self.planner, self.mode = (
            task,
            backend,
            policy,
            planner,
            mode,
        )
        self.budget, self.output = budget or Budget(), output
        self.observer = observer or Observer(output)
        self.memory = Memory()
        self.plan: Plan | None = None
        self.completed: set[str] = set()
        self.lengths: dict[str, int] = {}
        self.actions = self.cycles = self.planner_calls = self.false_completions = (
            self.finish_requests
        ) = 0
        self.grounding_rejections = 0
        self.subtask_lengths: list[int] = []
        self.violations: list[str] = []
        self.events: list[dict] = []
        self.final_answer = ""
        if output:
            output.mkdir(parents=True, exist_ok=True)

    def log(self, kind: str, **data):
        self.observer.context.update(cycle=self.cycles)
        if kind in {"observation", "finish_observation"}:
            self.observer.context["observation_id"] = data["observation"]["observation_id"]
        if kind in {"brain_requested", "replan_requested"}:
            self.observer.context["trigger"] = data.get("reason")
        event = {"time": now(), "kind": kind, "event_id": len(self.events),
                 "run_id": self.observer.run_id, "cycle": self.cycles, **data}
        self.events.append(event)
        if self.output:
            with (self.output / "trajectory.jsonl").open("a") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    async def replan(self, obs: Observation, reason: str) -> bool:
        if self.mode == "flat" or (self.mode == "once" and self.plan is not None):
            return False
        if self.planner_calls >= self.budget.max_planner_calls:
            return False
        contract = self.current(obs)
        feedback = {
            "current_contract": contract.model_dump(mode="json") if contract else None,
            "completed_subtasks": sorted(self.completed),
            "actions_used": self.actions,
            "subtask_actions_used": self.lengths.get(contract.id, 0) if contract else 0,
            "unsatisfied_predicates": self.unsatisfied(
                contract.success_predicates if contract else self.task.success_predicates, obs
            ),
            "entity_progress": self.memory.progress(self.task, obs, contract),
            "recent_actions": self.memory.events[-12:],
        }
        self.log("replan_requested", reason=reason, feedback=feedback)
        for attempt in range(2):
            if self.planner_calls >= self.budget.max_planner_calls:
                return False
            self.planner_calls += 1
            try:
                plan = await self.planner.plan(
                    self.task.model_copy(deep=True), obs, self.memory, reason, feedback=feedback
                )
                break
            except InvalidPlanOutput as exc:
                self.log("invalid_plan", attempt=attempt, **exc.diagnostic)
                if attempt == 1:
                    raise
                feedback["schema_error"] = exc.diagnostic
        validate_plan(plan, self.task, self.memory)
        self.plan = plan
        self.final_answer = ""
        self.completed.clear()
        self.lengths.clear()
        self.log("plan", reason=reason, plan=plan.model_dump(mode="json"))
        return True

    def unsatisfied(self, predicates, obs: Observation) -> list[dict]:
        missing = []
        for predicate in predicates:
            if check(predicate, self.memory, obs):
                continue
            # Preserve any/not as a whole; their branches are alternatives, not requirements.
            if predicate.kind == "all":
                missing.extend(self.unsatisfied(predicate.children, obs))
            else:
                missing.append(predicate.model_dump(mode="json"))
        return missing

    def current(self, obs: Observation) -> Contract | None:
        if self.plan is None:
            return None
        while True:
            changed = False
            for subtask in self.plan.subtasks:
                if subtask.id in self.completed or not set(subtask.depends_on) <= self.completed:
                    continue
                if all_checks(subtask.success_predicates, self.memory, obs) and all_checks(
                    subtask.invariants, self.memory, obs
                ):
                    self.completed.add(subtask.id)
                    for binding in subtask.bindings:
                        if binding.name == "final_answer":
                            self.final_answer = binding.value
                    length = self.lengths.get(subtask.id, 0)
                    self.subtask_lengths.append(length)
                    self.log("subtask_completed", subtask_id=subtask.id, actions=length)
                    changed = True
                else:
                    return subtask
            if not changed:
                return None

    def result(self, status, reason):
        return RunResult(
            task_id=self.task.id,
            status=status,
            reason=reason,
            actions=self.actions,
            cycles=self.cycles,
            planner_calls=self.planner_calls,
            elapsed_s=time.monotonic() - self.started,
            false_completions=self.false_completions,
            finish_requests=self.finish_requests,
            grounding_rejections=self.grounding_rejections,
            violations=self.violations,
            subtask_lengths=self.subtask_lengths,
        )

    def internal_action(self, obs, operation):
        return Action(
            id=f"internal-{self.actions}",
            operation=operation,
            observation_id=obs.observation_id,
            document_version=obs.document_version,
            tab_id=obs.tab_id,
        )

    async def execute(self, action: Action, obs: Observation, contract: Contract | None = None):
        denial = authorize(action, self.task, self.memory, obs)
        if denial:
            self.log("permission_denied", reason=denial)
            return denial
        if action.effect != "read":
            self.memory.pending_writes[action.write_key] = {
                "predicate": action.readback.model_dump(),
                "observation_id": obs.observation_id,
                "waits": 0,
                "subtask_id": contract.id if contract else None,
            }
            self.memory.invalidate(action.entity, action.readback.field)
        self.actions += 1
        if contract:
            self.lengths[contract.id] = self.lengths.get(contract.id, 0) + 1
        receipt = await self.observer.measure("browser.execute", self.backend.execute, action)
        if receipt.status == "stale":
            self.grounding_rejections += 1
            if action.write_key:
                self.memory.pending_writes.pop(action.write_key, None)
        if receipt.status == "rejected" and action.write_key:
            self.memory.pending_writes.pop(action.write_key, None)
        if receipt.status == "ok" and action.operation == Operation.EXTRACT:
            facts = self.memory.extract(obs, self.task.extraction)
            self.log("extraction", facts=[f.model_dump() for f in facts])
        self.memory.events.append(
            {
                "operation": action.operation,
                "description": action.description,
                "entity": action.entity,
                "element_ref": action.element_ref,
                "before": self.memory.view(obs),
                "receipt": receipt.model_dump(),
            }
        )
        self.log("action", action=action.model_dump(mode="json"), receipt=receipt.model_dump())
        return None

    async def run(self) -> RunResult:
        self.started = time.monotonic()
        try:
            async with asyncio.timeout(self.budget.max_seconds):
                result = await self._loop()
        except TimeoutError:
            result = self.result("budget_exhausted", "wall-clock deadline reached")
        except ResourceLimit as exc:
            result = self.result("budget_exhausted", str(exc))
        except asyncio.CancelledError:
            self.checkpoint()
            raise
        except Exception as exc:
            self.log("error", error_type=type(exc).__name__, detail=str(exc)[:500])
            result = self.result("failed", f"{type(exc).__name__}: {str(exc)[:200]}")
        self.log("result", result=result.model_dump())
        self.checkpoint()
        if self.output:
            (self.output / "result.json").write_text(result.model_dump_json(indent=2))
        return result

    def checkpoint(self):
        """Synchronous atomic checkpoint survives an outer cancellation deadline."""
        if self.output:
            temporary = self.output / "memory.json.tmp"
            temporary.write_text(json.dumps(self.memory.export(), ensure_ascii=False, indent=2))
            temporary.replace(self.output / "memory.json")

    async def _loop(self):
        stalled = loading = 0
        previous = None
        progress_key = None
        visits_without_progress: dict[str, int] = {}
        for _ in range(self.budget.max_cycles):
            self.cycles += 1
            self.observer.context["cycle"] = self.cycles
            obs = await self.observer.measure("browser.observe", self.backend.observe)
            self.memory.observe(obs, self.task.extraction)
            self.log("observation", observation=obs.model_dump())
            if obs.http_status is not None and obs.http_status >= 400:
                return self.result("needs_attention",
                                   f"page load failed: HTTP {obs.http_status} at {obs.url}")
            if obs.challenge or "unsupported_iframe" in obs.errors:
                return self.result("needs_attention", "access challenge or unsupported iframe")
            if not all_checks(self.task.invariants, self.memory, obs):
                self.violations.append("task invariant failed")
                return self.result("failed", "hard constraint violated")
            if self.actions >= self.budget.max_actions:
                return self.result("budget_exhausted", "atomic action budget reached")
            if obs.loading:
                loading += 1
                if loading > self.budget.loading_waits:
                    return self.result("needs_attention", "page loading deadline exceeded")
                denial = await self.execute(self.internal_action(obs, Operation.WAIT), obs)
                if denial:
                    return self.result("needs_attention", denial)
                continue
            loading = 0
            # A write is unresolved until a later, independently observed value confirms it.
            if self.memory.pending_writes:
                pending_id = next(iter(self.memory.pending_writes.values())).get("subtask_id")
                pending_contract = (
                    next((s for s in self.plan.subtasks if s.id == pending_id), None)
                    if self.plan
                    else None
                )
                denial = await self.execute(
                    self.internal_action(obs, Operation.EXTRACT), obs, pending_contract
                )
                if denial:
                    return self.result("needs_attention", denial)
                for key, pending in list(self.memory.pending_writes.items()):
                    from .protocol import Predicate

                    predicate = Predicate.model_validate(pending["predicate"])
                    fact = self.memory.facts.get(predicate.entity, {}).get(predicate.field)
                    fresh = fact and fact.source.observation_id == obs.observation_id
                    if fresh and check(predicate, self.memory, obs):
                        self.memory.confirmed_writes.add(key)
                        del self.memory.pending_writes[key]
                        self.log("write_confirmed", write_key=key, source=fact.source.model_dump())
                    else:
                        pending["waits"] += 1
                        if pending["waits"] >= self.budget.readback_waits:
                            return self.result(
                                "needs_attention", "unknown write outcome; no resubmission"
                            )
                if self.memory.pending_writes and self.actions < self.budget.max_actions:
                    denial = await self.execute(
                        self.internal_action(obs, Operation.WAIT), obs, pending_contract
                    )
                    if denial:
                        return self.result("needs_attention", denial)
                continue
            if self.mode != "flat" and self.plan is None:
                if not await self.replan(obs, "initial"):
                    return self.result("budget_exhausted", "no valid plan within planner budget")
            contract = self.current(obs)
            if contract and not all_checks(contract.invariants, self.memory, obs):
                if contract.recovery == "stop" or not await self.replan(obs, "subtask_invariant"):
                    return self.result("needs_attention", "subtask condition invalidated")
                continue
            if contract and self.lengths.get(contract.id, 0) >= contract.max_actions:
                if contract.recovery == "stop" or not await self.replan(obs, "subtask_budget"):
                    return self.result("budget_exhausted", "subtask action budget reached")
                continue
            facts_state = {
                e: {k: (f.value, f.valid) for k, f in fields.items()}
                for e, fields in self.memory.facts.items()
            }
            signature = (obs.document_version, digest(facts_state), tuple(sorted(self.completed)))
            stalled = stalled + 1 if signature == previous else 0
            previous = signature
            current_progress = signature[1:]
            if current_progress != progress_key:
                visits_without_progress.clear()
                progress_key = current_progress
            visits_without_progress[obs.document_version] = (
                visits_without_progress.get(obs.document_version, 0) + 1
            )
            looping = visits_without_progress[obs.document_version] > self.budget.no_progress_limit
            if stalled >= self.budget.no_progress_limit or looping:
                reason = (
                    "no_progress" if stalled >= self.budget.no_progress_limit else "revisit_cycle"
                )
                stalled = 0
                visits_without_progress.clear()
                if not await self.replan(obs, reason):
                    return self.result("needs_attention", "local policy made no progress")
                continue
            # This is a real, authorized, metered action shared by all policies/modes.
            # Partial/loading/ambiguous pages remain available for normal navigation.
            if (
                self.memory.needs_capture(obs, self.task.extraction)
                and Operation.EXTRACT in self.task.allowed_operations
                and (contract is None or Operation.EXTRACT in contract.allowed_operations)
            ):
                action = self.internal_action(obs, Operation.EXTRACT)
                action.description = "Capture configured visible evidence before the next decision"
                action.entity = visible_fields(obs.text)[self.task.extraction.entity_label][0]
                denial = await self.execute(action, obs, contract)
                if denial:
                    return self.result("needs_attention", denial)
                continue
            candidates = generate(
                obs,
                self.task,
                self.memory,
                contract,
                self.budget.candidate_limit,
                final_answer=self.final_answer,
            )
            if not candidates:
                return self.result("needs_attention", "no permitted candidates")
            decision = await self.observer.measure(
                "policy.choose", self.policy.choose, self.task, obs, self.memory, contract, candidates
            )
            self.log(
                "decision",
                decision=decision.model_dump(),
                candidates=[a.model_dump(mode="json") for a in candidates],
            )
            selected = next((a for a in candidates if a.id == decision.choice), None)
            if selected is None:
                if await self.replan(obs, "unknown_choice"):
                    continue
                return self.result("needs_attention", "policy returned a choice outside candidates")
            threshold = self.budget.confidence_threshold
            if (
                threshold is not None
                and decision.confidence is not None
                and decision.confidence < threshold
            ):
                if await self.replan(obs, "low_confidence"):
                    continue
                return self.result("needs_attention", "confidence escalation")
            if selected.operation == Operation.REPLAN:
                if await self.replan(obs, "candidate_insufficient"):
                    continue
                return self.result(
                    "needs_attention", "candidate missing or replan budget exhausted"
                )
            if selected.operation == Operation.FINISH:
                self.final_answer = selected.bound_value or ""
                self.finish_requests += 1
                final_obs = await self.backend.observe()
                self.memory.observe(final_obs, self.task.extraction)
                self.log("finish_observation", observation=final_obs.model_dump())
                if not all_checks(self.task.invariants, self.memory, final_obs):
                    self.violations.append("task invariant failed at finish")
                    return self.result("failed", "hard constraint violated at finish")
                if (
                    not self.memory.pending_writes
                    and not self.violations
                    and not final_obs.challenge
                    and all_checks(self.task.success_predicates, self.memory, final_obs)
                ):
                    return self.result("success", "all trusted success predicates verified")
                self.false_completions += 1
                if not await self.replan(final_obs, "false_completion"):
                    return self.result("failed", "completion request rejected by verifier")
                continue
            denial = await self.execute(selected, obs, contract)
            if denial:
                return self.result("needs_attention", denial)
        return self.result("budget_exhausted", "decision cycle budget reached")
