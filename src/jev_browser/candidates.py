from __future__ import annotations

from urllib.parse import urlsplit

from .memory import Memory, visible_fields
from .protocol import Action, Contract, Observation, Operation, Predicate, Task, digest


def origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def allowed_url(url: str, task: Task) -> bool:
    if task.sandbox and url == "about:blank":
        return True
    parsed = urlsplit(url)
    return (
        parsed.scheme in {"http", "https"}
        and not parsed.username
        and not parsed.password
        and origin(url) in task.allowed_origins
    )


def validate_plan(plan, task: Task, memory: Memory) -> None:
    for contract in plan.subtasks:
        if not set(contract.allowed_operations) <= set(task.allowed_operations):
            raise ValueError("planner tried to expand allowed operations")
        for binding in contract.bindings:
            if binding.source == "user":
                if not any(binding == b for b in task.bindings):
                    raise ValueError("planner invented a user binding")
            elif binding.source == "fact":
                if memory.get(binding.entity, binding.field) != binding.value:
                    raise ValueError("binding not supported by an observed fact")
            elif not task.allow_generated_bindings:
                raise ValueError("generated bindings are disabled")


def generate(
    obs: Observation,
    task: Task,
    memory: Memory,
    contract: Contract | None,
    limit: int = 32,
    final_answer: str = "",
) -> list[Action]:
    permitted = set(task.allowed_operations)
    if contract:
        permitted &= set(contract.allowed_operations)
    values = {b.name: b.value for b in task.bindings}
    if final_answer:
        values.setdefault("final_answer", final_answer)
    if contract:
        values.update({b.name: b.value for b in contract.bindings})
    fields = visible_fields(obs.text)
    entity = fields.get(task.extraction.entity_label, ("", 0))[0]
    preferred, regular = [], []

    def make(op: Operation, description: str, **kwargs) -> Action:
        return Action(
            id="",
            operation=op,
            observation_id=obs.observation_id,
            document_version=obs.document_version,
            tab_id=obs.tab_id,
            description=description,
            **kwargs,
        )

    # Escape hatches and navigation are reserved before task-specific ranking.
    if Operation.REPLAN in task.allowed_operations:
        preferred.append(make(Operation.REPLAN, "NO_MATCH: request a revised subtask"))
    if Operation.FINISH in task.allowed_operations and (
        not task.requires_final_answer or values.get("final_answer")
    ):
        preferred.append(
            make(
                Operation.FINISH,
                "Submit bound final answer; independent verifier must agree",
                bound_value=values.get("final_answer"),
            )
        )
    if Operation.WAIT in permitted:
        preferred.append(make(Operation.WAIT, "Wait briefly for an expected page update"))
    if Operation.EXTRACT in permitted:
        preferred.append(
            make(Operation.EXTRACT, "Extract displayed labelled fields with provenance")
        )
    if Operation.SCROLL in permitted:
        preferred.append(make(Operation.SCROLL, "Scroll down one viewport", bound_value="down"))
        preferred.append(make(Operation.SCROLL, "Scroll up one viewport", bound_value="up"))
    if Operation.BACK in permitted and obs.url != task.start_url and len(memory.visits) > 1:
        preferred.append(make(Operation.BACK, "Return to the previous document"))
    if Operation.SWITCH_TAB in permitted:
        for tab_id, url in obs.tabs.items():
            if tab_id != obs.tab_id and allowed_url(url, task):
                preferred.append(
                    make(Operation.SWITCH_TAB, f"Switch to {tab_id}: {url}", bound_value=tab_id)
                )
    for element in obs.elements:
        if not element.enabled:
            continue
        rules = [r for r in task.rules if r.name == element.name]
        if element.href and allowed_url(element.href, task) and Operation.OPEN_URL in permitted:
            regular.append(
                make(
                    Operation.OPEN_URL,
                    f"Open observed link: {element.name}",
                    bound_value=element.href,
                )
            )
        for rule in rules:
            if rule.operation not in permitted:
                continue
            if rule.effect != "read" and rule.id not in task.approved_writes:
                continue
            bound = values.get(rule.binding) if rule.binding else None
            if rule.operation in (Operation.FILL, Operation.SELECT) and bound is None:
                continue
            if rule.operation == Operation.SELECT and bound not in element.options:
                continue
            readback = None
            write_key = None
            if rule.effect != "read":
                if not (entity and rule.readback_field and rule.readback_value is not None):
                    continue
                write_key = digest([rule.id, entity, bound])
                if write_key in memory.pending_writes or write_key in memory.confirmed_writes:
                    continue
                readback = Predicate(
                    kind="fact", entity=entity, field=rule.readback_field, value=rule.readback_value
                )
            action = make(
                rule.operation,
                f"{element.name} | {element.context}",
                element_ref=element.id,
                bound_value=bound,
                effect=rule.effect,
                rule_id=rule.id,
                entity=entity,
                write_key=write_key,
                readback=readback,
            )
            if element.name.casefold() in {"next", "previous", "back", "close", "cancel"}:
                preferred.append(action)
            else:
                regular.append(action)
    if contract and contract.entity_refs:
        regular.sort(key=lambda a: not any(e in a.description for e in contract.entity_refs))
    # Never silently prune safety/navigation options when K is too small.
    if len(preferred) > limit:
        raise ValueError("candidate limit cannot retain required navigation; increase K")
    result = preferred + regular[: limit - len(preferred)]
    for number, action in enumerate(result):
        action.id = f"a{number}"
    return result


def authorize(action: Action, task: Task, memory: Memory, obs: Observation) -> str | None:
    if action.operation not in task.allowed_operations:
        return "operation is outside task permissions"
    if not allowed_url(obs.url, task):
        return "current origin is not authorized"
    if action.operation == Operation.OPEN_URL:
        if action.bound_value not in memory.observed_urls or not allowed_url(
            action.bound_value, task
        ):
            return "URL was not observed or is outside authorized origins"
    if action.effect != "read":
        if action.rule_id not in task.approved_writes:
            return "write requires independent approval"
        if not action.write_key or not action.readback:
            return "write requires an entity and a readback predicate"
        if action.write_key in memory.pending_writes or action.write_key in memory.confirmed_writes:
            return "duplicate or unresolved write"
    return None
