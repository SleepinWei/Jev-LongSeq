"""Scoped fast-policy context; the complete evidence archive remains in Memory."""

from urllib.parse import urlsplit

from .protocol import digest

JEV_LED_SYSTEM = (
    " You lead the execution loop. Continue the original task across pages and dialogs; "
    "DS guidance is advisory and may belong to a previous page. Use fresh control identity "
    "and values. Ordinary navigation or opening a form does not require another DS plan. "
    "Choose request_replan/NO ACTION when the goal, dependency, control or result is unclear. "
    "An unbound fill/select is a NO ACTION request for DS to supply that field's value; "
    "it will not dispatch until you select the prepared value on a fresh observation. "
    "Assess only the previous action's local effect. not_applied means an ordinary text "
    "fill is visibly still blank/unchanged, not that a Save/Submit failed. The controller "
    "must corroborate it. Use pending for asynchronous updates and unknown/NO ACTION "
    "for ambiguous writes. Do not submit again. When a field rejects its value, inspect "
    "placeholder/validation_message; ask DS for a repair rather than repeating that value. "
    "Task progress distinguishes local effects from persisted business records. Follow "
    "the original task's dependencies, do not invent new ordering constraints, and do "
    "not abandon an unfinished draft. Request finish only after all requested work has evidence."
)


def input_context_key(memory, obs, element):
    """Prepared values expire when the document, dialog or other field values change."""
    return digest([memory.environment_id, obs.tab_id, obs.url, obs.document_id, obs.dialogs,
        element.id, element.role, element.name, element.context, element.grid_ref, element.row_ref,
        element.input_type, element.placeholder, element.validation_message, element.options,
        [(e.id, e.name, e.value, e.grid_ref, e.row_ref) for e in obs.elements
         if e.id != element.id and (e.editable or e.selectable or e.read_only)]])


def policy_context(memory, obs, content):
    raw = content["untrusted_memory"]
    scope = memory.feedback.get("execution_scope", {})
    url = urlsplit(obs.url)
    same_scope = (scope.get("environment_id") == memory.environment_id
        and scope.get("location") == [obs.tab_id, url.scheme, url.netloc, url.path, url.fragment]
        and scope.get("dialogs", []) == obs.dialogs)
    relevant_nodes, older_refs = [], []
    for node in memory.key_nodes.values():
        source = node.get("source", {})
        if source.get("url") == obs.url:
            relevant_nodes.append(node)
        else:
            older_refs.append({"url": source.get("url"), "quote": source.get("quote", "")[:160]})
    checkpoints = [{k: c.get(k) for k in
        ("environment_id", "target", "status", "fields", "basis", "action_key")}
        for c in memory.write_checkpoints]
    projected = {
        "loop": memory.feedback.get("jev_loop", {}),
        "brain_guidance": raw.get("brain_guidance", ""),
        "guidance_matches_current_scope": same_scope,
        "planned_inputs": raw.get("planned_inputs", []) if same_scope else [],
        "working_memory": raw.get("working_memory", ""),
        "execution_window": raw.get("execution_window"),
        "pending_writes": raw.get("pending_writes", []),
        "recent_history": raw.get("history_for_context", {}).get("recent", [])[-6:],
        "current_environment_readbacks": raw.get("current_environment_readbacks", {}),
        "business_checkpoints": checkpoints,
        "unresolved_verifications": raw.get("unresolved_verifications", []),
        "verification_ledger": raw.get("verification_ledger", {}),
        "blockers": raw.get("blockers", []),
        "current_page_key_nodes": relevant_nodes[-8:],
        "older_key_node_hints": older_refs[-8:],
        "opened_pages": raw.get("opened_pages", []),
        "execution_feedback": raw.get("execution_feedback", {}),
        "evidence_scope": "Local readbacks/checkpoints are not whole-task completion. "
                          "Omitted older evidence remains archived; ask DS if needed.",
    }
    if raw.get("resume"):
        projected.update({k: raw[k] for k in
            ("resume", "resume_warning_scope", "interrupted_operations") if k in raw})
    content["untrusted_memory"] = projected
    content["policy_context_profile"] = "jev_led_v1"
    return content
