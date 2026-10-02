from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from .context_budget import history_view
from .protocol import Contract, Extraction, Fact, Observation, Predicate, Source, Task, digest


def visible_fields(text: str) -> dict[str, tuple[str, int]]:
    """Extract only displayed `Label: value` lines. Ambiguous labels are discarded."""
    result: dict[str, tuple[str, int]] = {}
    ambiguous = set()
    for number, line in enumerate(text.splitlines(), 1):
        match = re.fullmatch(r"\s*([\w -]{1,60}):\s*(\S.*?)\s*", line)
        if match:
            label, value = match.groups()
            if label in result:
                ambiguous.add(label)
            result[label] = (value, number)
    return {key: value for key, value in result.items() if key not in ambiguous}


class Memory:
    def __init__(self) -> None:
        self.facts: dict[str, dict[str, Fact]] = {}
        self.history: list[Fact] = []
        self.conflicts: list[dict[str, Any]] = []
        self.visits: dict[str, dict[str, Any]] = {}
        self.observed_urls: set[str] = set()
        self.tab_entities: dict[str, str] = {}
        self.pending_writes: dict[str, dict[str, Any]] = {}
        self.interrupted_writes: dict[str, dict[str, Any]] = {}
        self.resume_context: dict[str, Any] = {}
        self.confirmed_writes: set[str] = set()
        self.environment_id = uuid4().hex
        self.confirmed_actions: list[dict[str, Any]] = []
        self.write_checkpoints: list[dict[str, Any]] = []
        self.unresolved_verifications: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.page_notes: dict[str, dict] = {}
        self.page_registry: dict[str, dict] = {}
        self.observed_entities: set[str] = set()
        self.evidence: dict[str, dict] = {}
        self.feedback: dict[str, Any] = {}
        self.key_nodes: dict[str, dict[str, Any]] = {}
        self.working_memory_archive: dict[str, str] = {}
        self.dynamic_mode = False
        self.recent_evidence_limit = 4

    def observe(self, obs: Observation, extraction: Extraction | None = None) -> None:
        tabs = {**obs.tabs, obs.tab_id: obs.url}
        for url in dict.fromkeys(tabs.values()):
            record = self.page_registry.setdefault(url, {
                "url": url, "title": "", "observed": False,
                "opened_from": obs.url if url != obs.url else None,
                "first_seen": obs.captured_at,
            })
            record["last_seen"] = obs.captured_at
        for url, record in self.page_registry.items():
            record["open_tab_ids"] = [tab for tab, target in tabs.items() if target == url]
        current = self.page_registry.pop(obs.url)
        current.update(title=obs.title, observed=True, last_observation_id=obs.observation_id,
                       excerpt=obs.text[:240])
        self.page_registry[obs.url] = current
        entry = self.visits.setdefault(obs.url, {"observations": 0, "extracted_versions": []})
        entry["observations"] += 1
        entry["last_version"] = obs.document_version
        self.observed_urls.add(obs.url)
        self.observed_urls.update(e.href for e in obs.elements if e.href)
        if extraction:
            entity = visible_fields(obs.text).get(extraction.entity_label, ("", 0))[0]
            if entity:
                self.observed_entities.add(entity)
        view_key = digest([obs.url, obs.tab_id, obs.frame_id, obs.document_version])
        self.page_notes.pop(view_key, None)
        self.page_notes[view_key] = {
            "url": obs.url,
            "tab_id": obs.tab_id,
            "frame_id": obs.frame_id,
            "title": obs.title,
            "observation_id": obs.observation_id,
            "document_version": obs.document_version,
            "visible_text": obs.text[:6000],
            "truncated": len(obs.text) > 6000,
        }
        while len(self.page_notes) > 8:
            del self.page_notes[next(iter(self.page_notes))]
        # Readback and WAIT can share one observation interval. Record the next
        # observed state for each action, without claiming an intermediate snapshot.
        intervening = 0
        for event in reversed(self.events):
            if "after" in event:
                break
            event["after"] = self.view(obs)
            event["intervening_actions"] = intervening
            intervening += 1

    @staticmethod
    def view(obs: Observation) -> dict:
        return {
            "url": obs.url,
            "tab_id": obs.tab_id,
            "observation_id": obs.observation_id,
            "document_version": obs.document_version,
        }

    def needs_capture(self, obs: Observation, config: Extraction) -> bool:
        """Only capture complete, unambiguous configured fields; never infer missing values."""
        if not config.capture_on_observe or not config.fields:
            return False
        values = visible_fields(obs.text)
        if config.entity_label not in values or not all(f in values for f in config.fields):
            return False
        entity = values[config.entity_label][0]
        return any(
            not (fact := self.facts.get(entity, {}).get(field))
            or fact.value != values[field][0]
            or fact.source.document_version != obs.document_version
            or fact.source.url != obs.url
            or fact.source.tab_id != obs.tab_id
            for field in config.fields
        )

    def progress(self, task: Task, obs: Observation, contract: Contract | None = None) -> dict:
        """Advisory entity progress from trusted, separable fact predicates only."""
        grouped: dict[str, list[Predicate]] = {}

        def leaves(predicate):
            if predicate.children:
                return [leaf for child in predicate.children for leaf in leaves(child)]
            return [predicate]

        def collect(predicate):
            parts = leaves(predicate)
            entities = {p.entity for p in parts if p.kind == "fact"}
            if len(entities) == 1 and all(p.kind == "fact" for p in parts):
                grouped.setdefault(next(iter(entities)), []).append(predicate)
            elif predicate.kind == "all":
                for child in predicate.children:
                    collect(child)

        for predicate in task.success_predicates:
            collect(predicate)
        entities = list(grouped)
        if contract and contract.entity_refs:
            entities = [e for e in contract.entity_refs if e in grouped]
        rows = []
        for entity in entities:
            predicates = grouped[entity]
            fields = sorted({p.field for pred in predicates for p in leaves(pred)})
            missing = [field for field in fields if self.get(entity, field) is None]
            pending = any(
                p["predicate"].get("entity") == entity for p in self.pending_writes.values()
            )
            verified = not missing and not pending and all_checks(predicates, self, obs)
            if verified:
                status = "verified"
            elif pending:
                status = "pending_write"
            elif not missing:
                status = "evidence_recorded"
            elif entity in self.observed_entities or entity in self.facts:
                status = "observed"
            else:
                status = "unseen"
            rows.append({"entity": entity, "status": status, "missing_fields": missing})
        return {
            "entities": rows[:100],
            "truncated": len(rows) > 100,
            "next_unverified_entity": next(
                (r["entity"] for r in rows if r["status"] != "verified"), None
            ),
        }

    def extract(self, obs: Observation, config: Extraction) -> list[Fact]:
        values = visible_fields(obs.text)
        if config.entity_label not in values:
            return []
        entity = values[config.entity_label][0]
        self.tab_entities[obs.tab_id] = entity
        extracted = []
        for field in config.fields:
            if field not in values:
                continue
            value, line = values[field]
            fact = Fact(
                entity=entity,
                field=field,
                value=value,
                source=Source(
                    url=obs.url,
                    observation_id=obs.observation_id,
                    document_version=obs.document_version,
                    captured_at=obs.captured_at,
                    pointer=f"visible-text:line:{line}",
                    quote=obs.text.splitlines()[line - 1],
                    tab_id=obs.tab_id,
                ),
            )
            previous = self.facts.get(entity, {}).get(field)
            # A change on the same source is a temporal update; differing sources conflict.
            if previous and previous.value != value and previous.source.url != obs.url:
                self.conflicts.append({"previous": previous.model_dump(), "new": fact.model_dump()})
                fact.valid = False
            self.facts.setdefault(entity, {})[field] = fact
            self.history.append(fact)
            extracted.append(fact)
        self.visits[obs.url]["extracted_versions"].append(obs.document_version)
        return extracted

    def get(self, entity: str, field: str) -> str | None:
        fact = self.facts.get(entity, {}).get(field)
        return fact.value if fact and fact.valid else None

    def invalidate(self, entity: str, field: str) -> None:
        fact = self.facts.get(entity, {}).get(field)
        if fact:
            fact.valid = False

    def context(self, entities: list[str] | None = None, limit: int = 100) -> dict:
        if self.dynamic_mode:
            pages = list(self.page_registry.values())[-24:]
            history = history_view(self.events)
            return {
                **({"unresolved_verifications": self.unresolved_verifications}
                   if self.unresolved_verifications else {}),
                **({"planned_inputs": self.feedback["inputs"]} if self.feedback.get("inputs") else {}),
                **({"verification_stage": self.feedback["verification"]}
                   if self.feedback.get("verification") else {}),
                **({"current_environment_readbacks": self.current_readbacks()}
                   if self.confirmed_actions else {}),
                **({"write_checkpoints": self.write_checkpoints}
                   if self.write_checkpoints else {}),
                **({"planning_handoff": self.feedback["planning_handoff"]}
                   if self.feedback.get("planning_handoff") else {}),
                **({"execution_scope": {k: v for k, v in self.feedback["execution_scope"].items()
                                        if k != "binding_origins"}}
                   if self.feedback.get("execution_scope") else {}),
                "opened_pages": pages,
                "opened_pages_total": len(self.page_registry),
                "opened_pages_truncated": len(self.page_registry) > len(pages),
                "brain_guidance": self.feedback.get("next_goal", ""),
                "working_memory": self.feedback.get("working_memory", ""),
                "key_nodes": list(self.key_nodes.values()),
                "blockers": self.feedback.get("blockers", []),
                "history_for_context": history,
                **({"resume": self.resume_context,
                    "resume_warning_scope": "Inherited work at startup only; later current-environment "
                                            "readbacks remain valid unless new contradictory evidence appears.",
                    "interrupted_operations": [
                        {"description": p["action"]["description"],
                         "operation": p["action"]["operation"],
                         "disposition": "previous session ended; not confirmed; do not replay"}
                        for p in self.interrupted_writes.values()
                    ]} if self.resume_context else {}),
                "pending_writes": [
                    {"action": {k: p["action"].get(k) for k in
                                ("operation", "description", "bound_value")},
                     "before_excerpt": p.get("before_excerpt", ""),
                     "expected_goal": p.get("expected_goal", ""), "waits": p["waits"],
                     "confirmation_scope": p.get("confirmation_scope", "action_effect"),
                     "before_dialogs": p.get("before_dialogs", [])}
                    for p in self.pending_writes.values()
                ],
                "recent_events": history["recent"][-3:],
                "recent_evidence": [
                    {"url": e["source"]["url"], "quote": e["source"]["quote"]}
                    for e in list(self.evidence.values())[-self.recent_evidence_limit:]
                ],
                "execution_feedback": self.feedback.get("execution_feedback", {}),
                "actions_since_brain": history_view(
                    self.events[self.feedback.get("action_cursor", 0):])["recent"],
            }
        keys = entities or list(self.facts)[-limit:]
        context = {
            "facts": {
                e: {k: f.model_dump() for k, f in self.facts.get(e, {}).items()}
                for e in keys[:limit]
            },
            "pending_writes": self.pending_writes,
            "recent_events": self.events[-6:],
            "tab_entities": self.tab_entities,
            "recent_visible_pages": list(self.page_notes.values())[-8:],
        }
        if self.feedback or self.evidence:
            context["dynamic_feedback"] = self.feedback
            context["visible_evidence"] = list(self.evidence.values())
        return context

    def current_readbacks(self) -> dict:
        current = [a for a in self.confirmed_actions if a["environment_id"] == self.environment_id]
        def boundary(action):
            return (action["business_commit_confirmed"] or "".join(action["target"].casefold().split())
                    in {"save", "submit", "publish", "approve", "保存", "提交"})
        return {
            "environment_id": self.environment_id,
            "meaning": "Immutable local action evidence in this environment, not whole-task completion. "
                       "Startup recovery warnings apply only to inherited work; they cannot invalidate "
                       "these later readbacks. New contradictory observations still require review.",
            "actions": [a for a in current if boundary(a)] + [a for a in current if not boundary(a)][-4:],
        }

    def export(self) -> dict:
        return {
            **self.context(list(self.facts), limit=len(self.facts)),
            "history": [f.model_dump() for f in self.history],
            "visits": self.visits,
            "page_registry": self.page_registry,
            "conflicts": self.conflicts,
            "observed_urls": sorted(self.observed_urls),
            "confirmed_writes": sorted(self.confirmed_writes),
            "confirmed_actions_archive": self.confirmed_actions,
            "write_checkpoints_archive": self.write_checkpoints,
            "environment_id": self.environment_id,
            "observed_entities": sorted(self.observed_entities),
            **({"evidence_archive": self.evidence, "brain_feedback": self.feedback}
               if self.dynamic_mode else {}),
            **({"key_nodes_archive": self.key_nodes,
                "working_memory_archive": self.working_memory_archive,
                "event_archive": self.events} if self.dynamic_mode else {}),
            **({"pending_writes": self.pending_writes} if self.dynamic_mode else {}),
            **({"interrupted_writes": self.interrupted_writes,
                "resume_context": self.resume_context} if self.resume_context else {}),
        }


def check(predicate: Predicate, memory: Memory, obs: Observation) -> bool:
    kind = predicate.kind
    if kind == "all":
        return all(check(p, memory, obs) for p in predicate.children)
    if kind == "any":
        return any(check(p, memory, obs) for p in predicate.children)
    if kind == "not":
        return not check(predicate.children[0], memory, obs)
    if kind == "no_pending_writes":
        return not memory.pending_writes
    if kind == "text":
        return predicate.value in obs.text
    if kind == "url":
        return predicate.value == obs.url
    actual = memory.get(predicate.entity, predicate.field)
    if predicate.op == "exists":
        return actual is not None
    if actual is None:
        return False
    expected = predicate.value
    if predicate.op == "eq":
        return actual == expected
    if predicate.op == "ne":
        return actual != expected
    try:
        left, right = Decimal(actual), Decimal(expected)
        if not (left.is_finite() and right.is_finite()):
            return False
        return {"lt": left < right, "le": left <= right, "gt": left > right, "ge": left >= right}[
            predicate.op
        ]
    except (InvalidOperation, ValueError):
        return False


def all_checks(predicates: list[Predicate], memory: Memory, obs: Observation) -> bool:
    return all(check(p, memory, obs) for p in predicates)
