from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

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
        self.confirmed_writes: set[str] = set()
        self.events: list[dict[str, Any]] = []
        self.page_notes: dict[str, dict] = {}
        self.observed_entities: set[str] = set()
        self.evidence: dict[str, dict] = {}
        self.feedback: dict[str, Any] = {}
        self.dynamic_mode = False
        self.recent_evidence_limit = 4

    def observe(self, obs: Observation, extraction: Extraction | None = None) -> None:
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
            return {
                "brain_guidance": self.feedback.get("next_goal", ""),
                "working_memory": self.feedback.get("working_memory", ""),
                "pending_writes": [
                    {"action": {k: p["action"].get(k) for k in
                                ("operation", "description", "bound_value")},
                     "before_excerpt": p.get("before_excerpt", ""),
                     "expected_goal": p.get("expected_goal", ""), "waits": p["waits"]}
                    for p in self.pending_writes.values()
                ],
                "recent_events": [
                    {"operation": e["operation"], "description": e["description"],
                     "receipt": e["receipt"]["status"]} for e in self.events[-3:]
                ],
                "recent_evidence": [
                    {"url": e["source"]["url"], "quote": e["source"]["quote"]}
                    for e in list(self.evidence.values())[-self.recent_evidence_limit:]
                ],
                "execution_feedback": self.feedback.get("execution_feedback", {}),
                "actions_since_brain": [
                    {"operation": e["operation"], "description": e["description"],
                     "receipt": e["receipt"]["status"]}
                    for e in self.events[self.feedback.get("action_cursor", 0):]
                ],
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

    def export(self) -> dict:
        return {
            **self.context(list(self.facts), limit=len(self.facts)),
            "history": [f.model_dump() for f in self.history],
            "visits": self.visits,
            "conflicts": self.conflicts,
            "observed_urls": sorted(self.observed_urls),
            "confirmed_writes": sorted(self.confirmed_writes),
            "observed_entities": sorted(self.observed_entities),
            **({"evidence_archive": self.evidence, "brain_feedback": self.feedback}
               if self.dynamic_mode else {}),
            **({"pending_writes": self.pending_writes} if self.dynamic_mode else {}),
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
