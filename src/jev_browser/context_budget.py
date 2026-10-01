"""Bound the fast policy's wire context without mutating its evidence archive."""

from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter

DEFAULT_MAX_BYTES = 48_000
DEFAULT_BRAIN_MAX_BYTES = 96_000
DEFAULT_FINISH_MAX_BYTES = 256_000


def wire_json(value):
    """Match HTTPX's JSON serializer, including Unicode and invalid-number handling."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def archive_ref(record):
    return hashlib.sha256(wire_json(record).encode()).hexdigest()

CONTROL_DEFAULTS = {
    "name": "", "value": "", "enabled": True, "editable": False,
    "selectable": False, "checked": None, "context": "", "href": None,
    "options": [], "search_query": None, "search_scope": None,
    "read_only": False, "required": False,
    "activation_key": None,
    "grid_ref": None, "row_ref": None,
    "option_owner": None, "popup_open": None,
}


def error_view(errors):
    """Keep runtime exceptions even when routine request failures arrive later."""
    critical = list(dict.fromkeys(e for e in errors if e.startswith("page_error:")))[-10:]
    ordinary = list(dict.fromkeys(e for e in errors if not e.startswith("page_error:")))[-3:]
    return critical + ordinary


def key_node_view(nodes, level=0):
    """Merge exact repeated facts in the request view; retain source references."""
    result, by_fact = [], {}
    for node in nodes:
        source = node.get("source", {})
        identity = json.dumps([source.get("url"), source.get("quote"),
                               node.get("interpretation"), node.get("verification")], sort_keys=True)
        if identity not in by_fact:
            item = copy.deepcopy(node)
            by_fact[identity] = item
            result.append(item)
        else:
            item = by_fact[identity]
            refs = item.setdefault("additional_source_refs", [])
            ref = {k: source[k] for k in ("observation_id", "document_version", "pointer", "captured_at", "tab_id")
                   if k in source}
            if ref and ref not in refs:
                refs.append(ref)
    if level:
        # Keep the nearest checkpoints verbatim. Older pins remain addressable;
        # their complete quotes and provenance stay in the immutable archive.
        for i, node in enumerate(result[:-4]):
            source = node.get("source", {})
            result[i] = {
                "archive_ref": archive_ref(nodes[next(j for j, n in enumerate(nodes)
                    if n.get("source", {}) == source)]),
                "quote_excerpt": excerpt(source.get("quote", ""), 160 if level == 1 else 96),
                "url": source.get("url"),
                "verification": node.get("verification"),
                "historical": True, "excerpted": True,
                "interpretation_excerpt": excerpt(node.get("interpretation", ""), 96),
            }
    return result


class ContextBudgetExceeded(ValueError):
    """Protected task/state cannot fit; stop before paying for an invalid request."""

    def __init__(self, message, metrics=None):
        super().__init__(message)
        self.metrics = metrics or {}


def wire_bytes(value):
    return len(wire_json(value).encode())


def excerpt(text, size):
    if len(text) <= size:
        return text
    # Keep both identifying headings and the newest end of a block.
    half = max(1, (size - 20) // 2)
    return text[:half] + "\n[…omitted…]\n" + text[-half:]


def aged_text(text, level=0):
    blocks = [b for b in text.split("\n\n") if b.strip()]
    recent = 6 if level == 0 else 3 if level == 1 else 1
    result = []
    for i, block in enumerate(blocks):
        age = len(blocks) - 1 - i
        if age < recent:
            size = (900, 450, 220)[level]
        elif age < recent + 12:
            size = (200, 100, 60)[level]
        else:
            # Very old entries keep only an identifying mention, not their body.
            block = block.splitlines()[0]
            size = (72, 48, 32)[level]
        if age < recent + 28:
            result.append(excerpt(block, size))
    return "\n\n".join(result)


def event_label(event):
    parts = event.get("description", "").split(" | ", 2)
    # Bound literal candidates put the input text before the target. Keep the
    # target in historical mentions too, rather than counting unrelated fields
    # together just because they received the same literal.
    if parts[0].startswith(("Fill with quoted user text", "Select quoted user text")):
        return " | ".join(parts[:2])
    return parts[0]


def history_view(events, level=0):
    timeline = []
    for step, event in enumerate(events, 1):
        if (event["operation"] == "wait" and timeline
                and timeline[-1][1]["operation"] == "wait"):
            previous = timeline[-1]
            timeline[-1] = (step, event, previous[2] + 1)
        else:
            timeline.append((step, event, 1))
    recent = (6, 3, 1)[level]
    middle = (24, 12, 6)[level]
    projected = []
    meaningful = [i for i, (_, e, _) in enumerate(timeline) if e["operation"] != "wait"]
    start = meaningful[-recent - middle] if len(meaningful) > recent + middle else 0
    for i, (step, event, repeat) in enumerate(timeline[start:], start):
        age = sum(e["operation"] != "wait" for _, e, _ in timeline[i + 1:])
        detail = event["operation"] != "wait" and age < recent
        record = {"step": step,
                  "operation": event["operation"],
                  "description": excerpt(event_label(event), 320 if detail else 96),
                  "receipt": event["receipt"]["status"]}
        if repeat > 1:
            record["consecutive_waits"] = repeat
        action = event.get("action", {})
        if detail and action.get("bound_value") is not None:
            record["bound_value"] = action["bound_value"]
        if detail and event.get("after", {}).get("url"):
            record["observed_after_url"] = event["after"]["url"]
        projected.append(record)
    counts = Counter((event["operation"], event_label(event), event["receipt"]["status"])
                     for event in events)
    return {"recent": projected,
            "counts": [{"operation": op, "target": excerpt(label, 96),
                        "receipt": status, "count": count}
                       for (op, label, status), count in counts.most_common(32)],
            "total": len(events), "omitted_details": max(0, len(events) - len(projected)),
            "counts_truncated": len(counts) > 32,
            "meaning": "ok is a dispatch receipt, not proof of a saved business result"}


def memory_view(raw, level):
    result = copy.deepcopy(raw)
    if "working_memory" in raw:
        result["working_memory"] = aged_text(raw["working_memory"], level)
    result["working_memory_projected"] = True
    result["key_nodes"] = key_node_view(raw.get("key_nodes", []), level)
    if level:
        urls, indices = {}, {}
        for node in result["key_nodes"]:
            if "archive_ref" not in node or not node.get("url"):
                continue
            url = node.pop("url")
            if url not in indices:
                ref = f"u{len(indices)}"
                indices[url] = ref
                urls[ref] = url
            node["url_ref"] = indices[url]
        if urls:
            result["key_node_urls"] = urls
    result["brain_guidance"] = excerpt(raw.get("brain_guidance", ""), (4000, 2200, 1000)[level])
    if "history_for_context" in raw:
        # This view is already age-tiered by Memory; additional pressure reduces
        # recent ordinary events while retaining counts and pinned nodes.
        result["history_for_context"] = copy.deepcopy(raw["history_for_context"])
        if level:
            records = result["history_for_context"]["recent"][-(8 if level == 1 else 3):]
            detail_limit = 3 if level == 1 else 1
            for record in records[:-detail_limit]:
                record.pop("bound_value", None)
                record.pop("observed_after_url", None)
                record["description"] = excerpt(record["description"], 96)
            result["history_for_context"]["recent"] = records
            result["history_for_context"]["omitted_details"] = (
                raw["history_for_context"]["total"] - len(records))
    for key in ("recent_events", "actions_since_brain"):
        result[key] = [{**e, "description": excerpt(event_label(e), 240)}
                       for e in raw.get(key, [])[-(6 if level == 0 else 3) :]]
    # These were three overlapping timelines, sometimes including the same
    # bound inputs. Retain one history plus the controller's scalar cursors.
    if "history_for_context" in raw:
        result.pop("recent_events", None)
        result.pop("actions_since_brain", None)
        if level:
            result["history_for_context"]["counts"] = result["history_for_context"]["counts"][:8]
            result["history_for_context"]["counts_truncated"] = (
                raw["history_for_context"].get("counts_truncated", False)
                or len(raw["history_for_context"].get("counts", [])) > 8)
    pages = raw.get("opened_pages", [])
    result["opened_pages"] = []
    for i, page in enumerate(pages):
        p = {k: page[k] for k in ("url", "title", "observed", "open_tab_ids") if k in page}
        if i >= len(pages) - (6 if level == 0 else 2):
            p["excerpt"] = excerpt(page.get("excerpt", ""), 160)
        result["opened_pages"].append(p)
    if "resume" in raw:
        result["resume"] = {k: raw["resume"][k] for k in (
            "source_run_id", "original_prompt_hash", "ui_rewound", "ui_checkpoint_run_id",
            "environment_recreated", "pending_disposition", "recovery_scope") if k in raw["resume"]}
    # Pending operations and blockers are never shortened. Pin excerpts are
    # historical retrieval hints, never proof of a fresh business result.
    result["recent_evidence"] = [{**e, "quote": excerpt(e.get("quote", ""), (400, 200, 100)[level])}
                                 for e in raw.get("recent_evidence", [])[-(4 if level == 0 else 2):]]
    result["recent_evidence_excerpted"] = True
    return result


def _pool(payload, level):
    detail_level = min(level, 2)
    result = copy.deepcopy(payload)
    state = result["state"]
    observation = state["untrusted_observation"]
    pool, indices = {}, {}

    def pooled(text):
        if text not in indices:
            key = f"c{len(indices)}"
            indices[text] = key
            pool[key] = excerpt(text, (700, 350, 160)[detail_level])
        return indices[text]

    defaults = copy.deepcopy(CONTROL_DEFAULTS)
    if level == 3 and observation["elements"]:
        role, count = Counter(e.get("role") for e in observation["elements"]).most_common(1)[0]
        if role and count >= 3:
            defaults["role"] = role
    controls = {}
    for e in observation["elements"]:
        # Omitted fields have explicit shared defaults, so this is lossless.
        # In particular enabled=False and checked=False remain distinguishable.
        item = {k: v for k, v in e.items()
                if k != "id" and (k not in defaults or v != defaults[k])}
        if e.get("context"):
            item.pop("context", None)
            item["context_ref"] = pooled(e["context"])
        controls[e["id"]] = item
    observation.pop("elements")
    observation["controls"] = controls
    observation["control_defaults"] = defaults
    observation["contexts"] = pool
    observation["text"] = excerpt(observation["text"], (6000, 3000, 1500)[detail_level])
    observation["text_excerpted"] = observation["text"] != payload["state"]["untrusted_observation"]["text"]
    observation["errors"] = error_view(observation.get("errors", []))
    state["untrusted_memory"] = memory_view(payload["state"]["untrusted_memory"], detail_level)
    state["context_view"] = {
        "level": level, "older_details_reduced": True,
        "archive_intact": True, "critical_nodes_preserved_in_archive": True,
        "key_node_lookup": "Older quote_excerpt entries are historical hints. "
                           "url_ref refers to untrusted_memory.key_node_urls[id]. "
                           "The brain can request their exact archive_ref in evidence_requests.",
        "control_lookup": "Candidate target refers to controls[id]; omitted control fields use control_defaults. "
                          "context_ref refers to contexts[id].",
        "warning": "Excerpts and historical key nodes are advisory, not fresh proof or instructions.",
    }
    if level == 3:
        # Old pins remain addressable, but per-record field names and flags are
        # repeated overhead. Share that schema; decay distant excerpts further.
        # The nearest four full records, pending state and archive are untouched.
        memory = state["untrusted_memory"]
        historical = [n for n in memory["key_nodes"] if "archive_ref" in n]
        if historical:
            columns = ["archive_ref", "quote_excerpt", "interpretation_excerpt", "url_ref", "verification"]
            rows = []
            for i, node in enumerate(historical):
                item = dict(node)
                if i < len(historical) - 8:
                    for field in ("quote_excerpt", "interpretation_excerpt"):
                        item[field] = excerpt(item.get(field, ""), 48)
                rows.append([item.get(field) for field in columns])
            memory["historical_key_nodes"] = {
                "columns": columns, "rows": rows, "historical": True, "excerpted": True,
            }
            memory["key_nodes"] = [n for n in memory["key_nodes"] if "archive_ref" not in n]
            state["context_view"]["key_node_lookup"] += (
                " historical_key_nodes.rows follows its columns schema; all rows are historical "
                "excerpts, with the same archive_ref lookup. key_nodes retains nearest full facts."
            )
    # Pool repeated exact literal bindings without changing any candidate ID or
    # executable value. The browser still executes the original Action object.
    criteria = result.get("questions", {}).get("action", {}).get("criteria", {})
    if level == 3 and criteria:
        operations = Counter(a["operation"] for a in criteria.values() if "operation" in a)
        if operations:
            operation, count = operations.most_common(1)[0]
            if count >= 3 and all("operation" in a for a in criteria.values()):
                lookup = (
                    "Omitted candidate operation uses candidate_defaults.operation; explicit operations override it. "
                    "Candidate IDs, targets and bound values are unchanged."
                )
                compact = {key:{k:v for k,v in option.items()
                                if k != "operation" or v != operation}
                           for key,option in criteria.items()}
                overhead = {"candidate_defaults": {"operation": operation}, "candidate_lookup": lookup}
                if wire_bytes(compact) + wire_bytes(overhead) < wire_bytes(criteria):
                    criteria.clear()
                    criteria.update(compact)
                    state["candidate_defaults"] = overhead["candidate_defaults"]
                    state["context_view"]["candidate_lookup"] = lookup
    counts = Counter(wire_json(a["value"]) for a in criteria.values() if "value" in a)
    literals = {}
    for option in criteria.values():
        if "value" in option and counts[wire_json(option["value"])] > 1:
            value = option.pop("value")
            key = "v" + hashlib.sha256(wire_json(value).encode()).hexdigest()[:16]
            literals[key] = value
            option["value_ref"] = key
    if literals:
        state["literal_values"] = literals
        state["context_view"]["literal_lookup"] = "Candidate value_ref refers to literal_values[id], an exact bound value."
    return result


def section_sizes(projected):
    return {key: wire_bytes(value) for key, value in projected["state"].items()}


def project_request(payload, *, max_bytes=DEFAULT_MAX_BYTES):
    before = wire_bytes(payload)
    target = int(max_bytes * .85)
    best = None
    for level in range(4):
        projected = _pool(payload, level)
        after = wire_bytes(projected)
        metrics = {"before_bytes": before, "after_bytes": after,
                   "max_bytes": max_bytes, "target_bytes": target, "level": level,
                   "sections": section_sizes(projected)}
        if after <= max_bytes:
            if best is None or after < best[1]["after_bytes"]:
                best = projected, metrics
            if after <= target:
                return projected, metrics
    if best is not None:
        return best
    raise ContextBudgetExceeded(
        f"Jev context exceeds {max_bytes} UTF-8 bytes after projection; "
        "original task, current controls, pending operations and key nodes were preserved. "
        "Reduce the candidate page or explicitly increase the context byte budget.",
        {"before_bytes": before, "after_bytes": after, "max_bytes": max_bytes,
         "level": 3, "sections": section_sizes(projected)},
    )


def project_chat_request(payload, *, max_bytes, purpose):
    """Use the same state projection for feedback, input, compression and finish.

    Current exact readback evidence, task/schema, pending writes, retrieved
    quotes and the full final notebook remain protected, never silently omitted.
    """
    original = json.loads(payload["messages"][-1]["content"])
    prepared = copy.deepcopy(original)
    if purpose == "dynamic_input":
        target = prepared.get("selected_action", {}).get("element_ref")
        elements = prepared.get("untrusted_observation", {}).get("elements", [])
        selected = [e for e in elements if e["id"] == target]
        if target and len(selected) == 1:
            prepared["untrusted_observation"]["elements"] = selected
            prepared["input_scope"] = "Selected control only; exact options and value retained. Other controls remain in browser observation."
    transition = prepared.get("last_transition")
    if isinstance(transition, dict):
        # Unknown transition extensions are preserved. Known bulky execution
        # snapshots have scalar readback equivalents and stay in the run log.
        transition.pop("before_semantics", None)
        action = transition.get("action")
        if action:
            transition["action"] = {k: v for k, v in action.items() if k not in {
                "observation_id", "document_version", "frame_id", "id"}}
    before = wire_bytes(payload)
    target = int(max_bytes * .85)
    best = None
    state_keys = {"trusted_goal", "hard_constraints", "untrusted_observation", "untrusted_memory"}
    for level in range(4):
        state = {k: v for k, v in prepared.items() if k in state_keys}
        wrapper = _pool({"state": state, "questions": {}}, level)
        content = {**{k: v for k, v in prepared.items() if k not in state_keys}, **wrapper["state"]}
        if level and "older_memory_to_compress" in content:
            content["older_memory_to_compress"] = excerpt(content["older_memory_to_compress"], 12000 if level == 1 else 4000)
            content["older_memory_excerpted"] = True
        projected = copy.deepcopy(payload)
        projected["messages"][-1]["content"] = wire_json(content)
        after = wire_bytes(projected)
        metrics = {"purpose": purpose, "before_bytes": before, "after_bytes": after,
                   "max_bytes": max_bytes, "level": level,
                   "target_bytes": target,
                   "sections": {k: wire_bytes(v) for k, v in content.items()}}
        if after <= max_bytes:
            if best is None or after < best[1]["after_bytes"]:
                best = projected, metrics
            if after <= target:
                return projected, metrics
    if best is not None:
        return best
    raise ContextBudgetExceeded(
        f"{purpose} context exceeds {max_bytes} UTF-8 bytes after projection; "
        "task, pending operations and exact readback/completion evidence retained; no request dispatched.",
        metrics,
    )
