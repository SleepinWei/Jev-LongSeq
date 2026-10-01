from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def now() -> str:
    return datetime.now(UTC).isoformat()


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Operation(StrEnum):
    CLICK = "click"
    FILL = "fill"
    SELECT = "select"
    SCROLL = "scroll"
    WAIT = "wait"
    BACK = "back"
    SWITCH_TAB = "switch_tab"
    OPEN_URL = "open_observed_url"
    EXTRACT = "extract_visible"
    FINISH = "request_finish"
    REPLAN = "request_replan"
    MORE_CANDIDATES = "next_candidates"


class Element(Model):
    id: str
    role: str
    name: str
    value: str = ""
    enabled: bool = True
    context: str = ""
    href: str | None = None
    options: list[str] = Field(default_factory=list)
    editable: bool = False
    read_only: bool = False
    required: bool = False
    selectable: bool = False
    checked: bool | None = None
    search_query: str | None = None
    search_scope: str | None = None
    activation_key: Literal["Escape"] | None = None
    grid_ref: str | None = None
    row_ref: str | None = None
    option_owner: str | None = None
    popup_open: bool | None = None
    popup_kind: Literal["menu"] | None = None
    menu_owner: str | None = None


class GridCell(Model):
    column: str
    value: str


class GridRow(Model):
    key: str
    cells: list[GridCell] = Field(default_factory=list)
    control_refs: list[str] = Field(default_factory=list)


class VisibleGrid(Model):
    id: str
    name: str = ""
    rows: list[GridRow] = Field(default_factory=list)


class Observation(Model):
    observation_id: str
    tab_id: str
    frame_id: str = "main"
    document_version: str
    url: str
    title: str
    text: str
    elements: list[Element] = Field(default_factory=list)
    grids: list[VisibleGrid] = Field(default_factory=list)
    dialogs: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    loading: bool = False
    http_status: int | None = None
    challenge: bool = False
    tabs: dict[str, str] = Field(default_factory=dict)
    captured_at: str = Field(default_factory=now)


class Source(Model):
    url: str
    observation_id: str
    document_version: str
    captured_at: str
    pointer: str
    quote: str
    tab_id: str


class Fact(Model):
    entity: str
    field: str
    value: str
    source: Source
    valid: bool = True


class Predicate(Model):
    """Finite DSL. No eval, executable expressions, selectors or scripts."""

    kind: Literal["all", "any", "not", "fact", "text", "url", "no_pending_writes"]
    children: list[Predicate] = Field(default_factory=list)
    entity: str = ""
    field: str = ""
    op: Literal["eq", "ne", "lt", "le", "gt", "ge", "exists"] = "eq"
    value: str = ""

    @model_validator(mode="after")
    def validate_shape(self) -> Predicate:
        if self.kind in ("all", "any") and not self.children:
            raise ValueError("all/any requires non-empty children")
        if self.kind == "not" and len(self.children) != 1:
            raise ValueError("not requires exactly one child")
        if self.kind == "fact" and not (self.entity and self.field):
            raise ValueError("fact requires entity and field")
        if self.kind in ("text", "url") and not self.value:
            raise ValueError("text/url requires a nonempty value")
        if self.kind not in ("all", "any", "not") and self.children:
            raise ValueError("leaf predicates cannot have children")
        return self


class Binding(Model):
    name: str
    value: str
    source: Literal["user", "fact", "planner"]
    entity: str = ""
    field: str = ""


class Contract(Model):
    id: str
    objective: str
    entity_refs: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    allowed_operations: list[Operation]
    bindings: list[Binding] = Field(default_factory=list)
    success_predicates: list[Predicate] = Field(min_length=1)
    invariants: list[Predicate] = Field(default_factory=list)
    max_actions: int = Field(default=8, ge=1, le=1000)
    recovery: Literal["replan", "stop"] = "replan"


class Plan(Model):
    subtasks: list[Contract] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def dag(self) -> Plan:
        nodes = {s.id: s for s in self.subtasks}
        if len(nodes) != len(self.subtasks):
            raise ValueError("duplicate subtask id")
        visiting, done = set(), set()

        def visit(key: str) -> None:
            if key not in nodes:
                raise ValueError(f"missing dependency {key}")
            if key in visiting:
                raise ValueError("cyclic task graph")
            if key in done:
                return
            visiting.add(key)
            for dependency in nodes[key].depends_on:
                visit(dependency)
            visiting.remove(key)
            done.add(key)

        for key in nodes:
            visit(key)
        return self


class InteractionRule(Model):
    """Trusted task configuration, never generated by a page or planner."""

    id: str
    operation: Operation
    name: str
    effect: Literal["read", "write", "irreversible"] = "read"
    binding: str | None = None
    readback_field: str | None = None
    readback_value: str | None = None


class Extraction(Model):
    entity_label: str = "Entity"
    fields: list[str] = Field(default_factory=list)
    capture_on_observe: bool = False


class Task(Model):
    id: str
    objective: str
    control_mode: Literal["structured", "dynamic"] = "structured"
    start_url: str = "about:blank"
    allowed_origins: list[str] = Field(default_factory=list)
    allowed_operations: list[Operation] = Field(default_factory=lambda: list(Operation))
    constraints: list[str] = Field(default_factory=list)
    success_predicates: list[Predicate] = Field(default_factory=list)
    invariants: list[Predicate] = Field(default_factory=list)
    rules: list[InteractionRule] = Field(default_factory=list)
    extraction: Extraction = Field(default_factory=Extraction)
    bindings: list[Binding] = Field(default_factory=list)
    approved_writes: list[str] = Field(default_factory=list)
    allow_generated_bindings: bool = False
    requires_final_answer: bool = False
    sandbox: bool = False

    @model_validator(mode="after")
    def validate_control(self) -> Task:
        if self.control_mode == "structured" and not self.success_predicates:
            raise ValueError("structured tasks require success predicates")
        if self.control_mode == "dynamic" and (
            self.rules or self.success_predicates or self.bindings or self.approved_writes
            or self.extraction.fields or self.extraction.capture_on_observe
        ):
            raise ValueError("dynamic tasks use the goal and observations, not predefined rules")
        return self


class Action(Model):
    id: str
    operation: Operation
    observation_id: str
    document_version: str
    tab_id: str
    frame_id: str = "main"
    element_ref: str | None = None
    bound_value: str | None = None
    description: str = ""
    effect: Literal["read", "write", "irreversible"] = "read"
    rule_id: str | None = None
    entity: str = ""
    write_key: str | None = None
    readback: Predicate | None = None


class Decision(Model):
    choice: str
    confidence: float | None = Field(default=None, ge=0, le=1)
    outcome: Literal["none", "confirmed", "pending", "unknown"] | None = None


class Receipt(Model):
    action_id: str
    status: Literal["ok", "stale", "rejected", "timeout", "unknown", "error"]
    detail: str = ""
    duration_s: float = 0
    write_key: str | None = None


class Budget(Model):
    max_actions: int = Field(default=300, ge=1)
    max_cycles: int = Field(default=600, ge=1)
    max_planner_calls: int = Field(default=20, ge=1)
    max_feedback_calls: int = Field(default=400, ge=1)
    brain_interval: int = Field(default=12, ge=1)
    max_seconds: float = Field(default=300, gt=0)
    candidate_limit: int = Field(default=32, ge=8, le=255)
    no_progress_limit: int = Field(default=4, ge=1)
    loading_waits: int = Field(default=5, ge=1)
    readback_waits: int = Field(default=5, ge=1)
    confidence_threshold: float | None = Field(default=None, ge=0, le=1)


class AgentTuning(Model):
    """Research may tune efficiency knobs, never permissions, grading or safety checks."""

    brain_interval: int = Field(default=12, ge=1, le=48)
    recent_evidence: int = Field(default=4, ge=1, le=8)
    excerpt_chars: int = Field(default=2400, ge=600, le=4000)
    prompt_variant: Literal["balanced", "compact", "coverage"] = "balanced"
    search_readback_grace_s: float = Field(default=3.0, ge=0, le=10)


class RunResult(Model):
    task_id: str
    status: Literal["success", "failed", "budget_exhausted", "needs_attention"]
    reason: str
    actions: int
    cycles: int
    planner_calls: int
    feedback_calls: int = 0
    elapsed_s: float
    false_completions: int = 0
    finish_requests: int = 0
    grounding_rejections: int = 0
    violations: list[str] = Field(default_factory=list)
    subtask_lengths: list[int] = Field(default_factory=list)
    strict_success: bool | None = None
