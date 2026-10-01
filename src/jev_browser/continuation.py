"""Reconstruct an unsaved SaaS UI draft, then continue the original goal and notebook."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from .dynamic import generate_dynamic
from .memory import Memory
from .protocol import Observation, Operation, Task, digest


def load_continuation(directory, task, manifest, *, ui_directory=None, _visited=None,
                      _history_only=False):
    directory = Path(directory).expanduser().resolve()
    visited = set(_visited or ())
    if directory in visited or len(visited) >= 16:
        raise ValueError("invalid or cyclic continuation lineage")
    visited.add(directory)
    original = Task.model_validate_json((directory / "task.json").read_text())
    if original.model_dump() != task.model_dump():
        raise ValueError("continuation task/prompt/permissions differ from the original")
    previous = json.loads((directory / "manifest.json").read_text())
    for key in ("fixture_hash", "image_ids", "upstream_revision", "port_map", "slot_prefix"):
        if previous.get(key) != manifest.get(key):
            raise ValueError(f"continuation environment differs: {key}")
    raw = (directory / "memory.json").read_bytes()
    saved = json.loads(raw)
    rows = [json.loads(line) for line in (directory / "trajectory.jsonl").read_text().splitlines()]
    observations = [Observation.model_validate(row["observation"]) for row in rows
                    if row["kind"] in {"observation", "finish_observation"}]
    last = observations[-1]
    # Removed containers cannot preserve committed business state. This recovery
    # path is only for an observed unsaved draft and a narrowly bounded UI recipe.
    rewind = ui_directory is not None or _history_only
    if "Not Saved" not in last.text or (not rewind and "No rows" not in last.text):
        raise ValueError("recreated-environment continuation requires an unsaved empty draft")
    memory, recipe = Memory(), []
    parent = saved.get("resume_context", {}).get("source_directory")
    if parent:
        inherited = load_continuation(parent, task, manifest, _visited=visited,
                                      _history_only=rewind)
        memory, recipe = inherited["memory"], list(inherited["recipe"])
        visited.update(Path(path) for path in memory.resume_context["lineage_directories"])
    memory.dynamic_mode = True
    current = None
    unresolved = {p["action"]["observation_id"]: p["action"]
                  for p in saved.get("pending_writes", {}).values()}
    safe_buttons = {"Login", "Search ⌘K", "⌘K", "Edit Full Form"}
    if rewind:
        safe_buttons |= {"Add row", "close (icon control)"}
    for row in rows:
        if row["kind"] in {"observation", "finish_observation"}:
            current = Observation.model_validate(row["observation"])
            memory.observe(current)
        elif row["kind"] == "action":
            action, receipt = row["action"], row["receipt"]
            memory.events.append({"operation": action["operation"],
                                  "description": action["description"],
                                  "action": copy.deepcopy(action),
                                  "before": memory.view(current), "receipt": receipt})
            if rewind and action["operation"] == Operation.CLICK:
                target = next((e for e in current.elements if e.id == action["element_ref"]), None)
                if (target and "".join(target.name.casefold().split()) in {"save", "submit"}
                        and receipt["status"] not in {"stale", "rejected"}):
                    raise ValueError("saved click may have committed data; cannot rewind its environment")
            if receipt["status"] != "ok" or action["operation"] == Operation.WAIT:
                continue
            if unresolved.get(action["observation_id"]) == action:
                continue  # Never reconstruct the unknown final click.
            element = next((e for e in current.elements if e.id == action["element_ref"]), None)
            if not element or action["operation"] not in {Operation.CLICK, Operation.FILL}:
                raise ValueError("saved action cannot be reconstructed through visible UI")
            if action["operation"] == Operation.CLICK and not (
                element.role in {"link", "option"}
                or (element.role == "button" and element.name in safe_buttons)
            ):
                raise ValueError("saved click may have committed data; cannot recreate its environment")
            if not rewind:
                recipe.append({"action": action, "element": element.model_dump()})
    memory.feedback = copy.deepcopy(saved["brain_feedback"])
    memory.evidence = copy.deepcopy(saved["evidence_archive"])
    memory.key_nodes = copy.deepcopy(saved.get("key_nodes_archive", {}))
    memory.working_memory_archive = copy.deepcopy(saved.get("working_memory_archive", {}))
    # New checkpoints include the full inherited timeline; legacy checkpoints
    # still reconstruct it from their lineage and trajectory above.
    if saved.get("event_archive"):
        memory.events = copy.deepcopy(saved["event_archive"])
    memory.visits = copy.deepcopy(saved["visits"])
    memory.page_registry = copy.deepcopy(saved["page_registry"])
    memory.observed_urls = set(saved["observed_urls"])
    memory.observed_entities = set(saved["observed_entities"])
    memory.confirmed_writes = set(saved["confirmed_writes"])
    memory.interrupted_writes = copy.deepcopy(saved.get("interrupted_writes", {}))
    memory.interrupted_writes.update(copy.deepcopy(saved.get("pending_writes", {})))
    memory.resume_context = {
        "source_run_id": previous["run_id"], "source_directory": str(directory),
        "lineage_directories": [str(path) for path in sorted(visited)],
        "memory_file_sha256": hashlib.sha256(raw).hexdigest(),
        "original_prompt_hash": digest(original.objective),
        "original_working_memory_hash": digest(memory.feedback["working_memory"]),
        "restored_actions": len(memory.events), "restored_evidence": len(memory.evidence),
        "environment_recreated": True,
        "pending_disposition": "Old session ended; unknown operations are retained as interrupted, "
                               "not confirmed and not replayed in the recreated environment.",
        "recovery_scope": "Login/navigation/unsaved fields only. No saved business data restored. "
                          "Reconcile prior advisory memory against fresh observations before acting.",
    }
    if ui_directory is not None:
        ui_path = Path(ui_directory).expanduser().resolve()
        if ui_path not in visited:
            raise ValueError("UI checkpoint must belong to the saved continuation lineage")
        ui_checkpoint = load_continuation(ui_path, task, manifest)
        recipe, last = ui_checkpoint["recipe"], ui_checkpoint["last_observation"]
        memory.resume_context.update({
            "ui_checkpoint_directory": str(ui_path),
            "ui_checkpoint_run_id": ui_checkpoint["manifest"]["run_id"],
            "ui_rewound": True,
            "recovery_scope": "UI rewound to an earlier unsaved empty form checkpoint. "
                              "Latest memory/history retained verbatim; later unsaved rows/fields "
                              "are historical and must be reconciled with the fresh page. "
                              "No committed business data restored.",
        })
    calls = [json.loads(line) for line in (directory / "model-calls.jsonl").read_text().splitlines()]
    return {"memory": memory, "recipe": recipe, "manifest": previous,
            "model_names": {r["model"] for r in calls}, "model_calls": calls,
            "last_observation": last}


def validate_continuation_models(checkpoint, policy, brain, *, brain_model=None):
    """Require original models unless an explicit, auditable brain migration is requested."""
    if not brain_model:
        if {policy.model, brain.model} != checkpoint["model_names"]:
            raise ValueError("continuation model configuration differs from the original")
        return
    calls = checkpoint.get("model_calls", [])
    policy_calls = [r for r in calls if r.get("kind") == "jev"]
    brain_calls = [r for r in calls if r.get("kind", "").startswith("dynamic_")]
    if not policy_calls or not brain_calls or len(policy_calls) + len(brain_calls) != len(calls):
        raise ValueError("brain migration requires an unambiguous Jev/brain checkpoint ledger")
    if {r["model"] for r in policy_calls} != {policy.model}:
        raise ValueError("brain migration cannot change the Jev policy model")
    old_policy_hosts = {r.get("endpoint_host") for r in policy_calls}
    if old_policy_hosts != {urlsplit(policy.endpoint).hostname}:
        raise ValueError("brain migration cannot change the Jev policy provider")
    if brain.model != brain_model:
        raise ValueError("configured brain model differs from --saas-resume-brain-model")
    checkpoint["memory"].resume_context["brain_migration"] = {
        "explicitly_requested": True,
        "previous_models": sorted({r["model"] for r in brain_calls}),
        "previous_endpoint_hosts": sorted({r.get("endpoint_host") or "unknown" for r in brain_calls}),
        "model": brain.model,
        "endpoint_host": urlsplit(brain.endpoint).hostname,
        "policy_model": policy.model,
        "policy_endpoint_host": urlsplit(policy.endpoint).hostname,
    }


async def replay_draft_step(browser, task, source, operation, value, observer, index):
    for _ in range(60):
        obs = await browser.observe()
        matches = [e for e in obs.elements if e.role == source["role"]
                   and e.name == source["name"]
                   and (not source.get("href") or e.href == source["href"])]
        if len(matches) == 1:
            candidate = next((a for a in generate_dynamic(obs, task, limit=250)
                              if a.element_ref == matches[0].id and a.operation == operation
                              and a.bound_value is None), None)
            if candidate:
                candidate.bound_value = value
                receipt = await browser.execute(candidate)
                observer.append("resume-bootstrap.jsonl", {
                    "step": index, "operation": candidate.operation,
                    "description": candidate.description, "receipt": receipt.model_dump(),
                })
                if receipt.status == "ok":
                    return
                if receipt.status != "stale":
                    raise ValueError("recovery UI action was not confirmed; no replay: " + receipt.status)
        await asyncio.sleep(0.2)
    raise ValueError(f"cannot recover visible control: {source['role']} {source['name']}")


def linked_runtime_failure(obs):
    return any(e.startswith("page_error:") and any(m in e for m in ("fields_dict", "refresh_field"))
               for e in obs.errors)


def blank_derived_fields(obs):
    # Some UIs hide the mandatory star on a read-only fetched field. The runtime
    # exception together with a visible empty display is also a recovery signal.
    return [e for e in obs.elements if e.read_only and not e.value.strip()
            and (e.required or linked_runtime_failure(obs))]


async def repair_derived_draft(browser, task, checkpoint, fresh, observer):
    """One explicit reset/reselect in the full form, using checkpoint-visible identities."""
    missing = blank_derived_fields(fresh)
    if not missing or not linked_runtime_failure(fresh):
        return fresh, False
    recipe = checkpoint["recipe"]
    if not recipe or recipe[-1]["element"]["name"] != "Edit Full Form":
        raise ValueError("required derived fields are blank; no safe full-form recovery recipe")
    options = [s["element"] for s in recipe if s["element"]["role"] == "option"]
    original = checkpoint.get("last_observation", fresh)
    expected_fields = {(e.role, e.name, e.value) for e in original.elements
                       if e.editable and e.value and e.name != "Search"}
    pairs = [(field, option) for field in fresh.elements
             if field.editable and field.enabled and field.value and field.value != "[redacted]"
             and (field.role, field.name, field.value) in expected_fields
             for option in options if field.value in option["name"]]
    if len(pairs) != 1:
        raise ValueError("required derived fields are blank; linked input is ambiguous")
    field, option = pairs[0]
    source = field.model_dump()
    await replay_draft_step(browser, task, source, Operation.FILL, "", observer, "derived-reset")
    # Clearing a link leaves its autocomplete open. Native Tab commits the clear
    # and blurs it without selecting another option or submitting business data.
    for _ in range(60):
        obs = await browser.observe()
        targets = [e for e in obs.elements if e.role == field.role and e.name == field.name
                   and e.editable and e.value == ""]
        if len(targets) != 1:
            raise ValueError("derived reset did not visibly clear the original input")
        receipt = await browser.blur_input(obs, targets[0].id)
        observer.append("resume-bootstrap.jsonl", {"step": "derived-blur",
                        "receipt": receipt.model_dump()})
        if receipt.status == "ok":
            break
        if receipt.status != "stale":
            raise ValueError("derived reset blur was not confirmed; no replay")
        await asyncio.sleep(0.2)
    else:
        raise ValueError("derived reset blur could not be grounded")
    await replay_draft_step(browser, task, source, Operation.FILL, field.value, observer, "derived-refill")
    await replay_draft_step(browser, task, option, Operation.CLICK, None, observer, "derived-reselect")
    for _ in range(60):
        fresh = await browser.observe()
        if all(len(matches := [e for e in fresh.elements if e.role == field.role
                               and e.name == field.name and e.read_only]) == 1
               and matches[0].value.strip() for field in missing):
            return fresh, True
        await asyncio.sleep(0.2)
    raise ValueError("required derived fields remain blank after one reset; no retry")


async def reconstruct_ui(browser, task, checkpoint, observer):
    started = time.monotonic()
    for index, step in enumerate(checkpoint["recipe"]):
        await replay_draft_step(browser, task, step["element"], step["action"]["operation"],
                                step["action"]["bound_value"], observer, index)
    # The Full Form click returns before its route/grid has finished rendering.
    # Wait for the checkpoint's visible draft markers before validating or repairing.
    previous, stable = None, 0
    for _ in range(60):
        fresh = await browser.observe()
        if "Not Saved" in fresh.text and "No rows" in fresh.text and not fresh.loading:
            signature = (fresh.document_version, tuple(fresh.errors))
            stable = stable + 1 if signature == previous else 0
            previous = signature
            if stable >= 2:
                break
        else:
            previous, stable = None, 0
        await asyncio.sleep(0.2)
    else:
        raise ValueError("reconstructed draft did not settle; no model continuation")
    fresh, repaired = await repair_derived_draft(browser, task, checkpoint, fresh, observer)
    original = checkpoint["last_observation"]

    def fields(obs):
        # A unique named field remains identifiable when fetched sibling text
        # expands its context. Duplicate labels still require exact row scope.
        labels = Counter((e.role, e.name) for e in obs.elements if e.editable)
        return sorted((e.role, e.name, e.context if labels[e.role, e.name] > 1 else "", e.value)
                      for e in obs.elements
                      if e.editable and e.value and e.name != "Search")

    if fields(original) != fields(fresh) or "Not Saved" not in fresh.text or "No rows" not in fresh.text:
        raise ValueError("reconstructed draft differs from the saved visible draft")
    for field in original.elements:
        if field.read_only and field.value:
            matches = [e for e in fresh.elements if e.name == field.name and e.role == field.role
                       and e.read_only]
            if len(matches) > 1:
                matches = [e for e in matches if e.context == field.context]
            if len(matches) != 1 or matches[0].value != field.value:
                raise ValueError("reconstructed derived draft differs from the saved visible draft")
    if blank_derived_fields(fresh):
        raise ValueError("reconstructed draft has blank required derived fields")
    if any(e.startswith("page_error:") and (not repaired or not any(
            m in e for m in ("fields_dict", "refresh_field"))) for e in fresh.errors):
        raise ValueError("reconstructed draft has unresolved UI runtime errors")
    recovered_errors = [e for e in fresh.errors if e.startswith("page_error:")] if repaired else []
    if recovered_errors:
        observer.append("resume-bootstrap.jsonl", {
            "step": "derived-state-verified", "resolved_runtime_errors": recovered_errors,
            "visible_fields": [e.model_dump() for e in fresh.elements if e.read_only],
        })
        if hasattr(browser, "acknowledge_runtime_recovery"):
            browser.acknowledge_runtime_recovery(fresh)
    return {"steps": len(checkpoint["recipe"]), "elapsed_s": time.monotonic() - started,
            "restored_url": fresh.url, "draft_fields_match": True,
            "derived_fields_repaired": repaired, "resolved_runtime_errors": recovered_errors}


def restore_controller(controller, checkpoint, recovery):
    controller.memory = checkpoint["memory"]
    controller.memory.recent_evidence_limit = controller.tuning.recent_evidence
    controller.memory.resume_context["ui_recovery"] = recovery
    controller.consumed = controller.memory.confirmed_writes | set(controller.memory.interrupted_writes)
    controller.initial_phase = "resume"
    controller.log("resumed", **controller.memory.resume_context)
