"""Enforce observed prerequisites at planning and immediately before dispatch."""

from unittest.mock import AsyncMock

import pytest

from jev_browser.dynamic import (
    DependencyReview,
    DynamicController,
    ExecutionGroup,
    Feedback,
    GroupAction,
    StageControl,
    StageEntry,
    generate_dynamic,
    write_prerequisite_diagnostics,
)
from jev_browser.memory import Memory
from jev_browser.protocol import Element, Observation, Task


def form():
    return Observation(observation_id="draft", document_version="v1", document_id="doc",
        tab_id="tab", url="about:blank", title="New Record", text="New Record Not Saved",
        elements=[Element(id="derived", role="textbox", name="Derived Field", read_only=True),
                  Element(id="source", role="button", name="Open Source"),
                  Element(id="optional", role="combobox", name="Optional Link", editable=True),
                  Element(id="save", role="button", name="S a ve")])


def definition():
    return Task(id="contract", objective="Create the requested record", sandbox=True, control_mode="dynamic")


def review(status="not_applicable", quote="Derived Field (optional)"):
    return DependencyReview(element_ref="derived", status=status, evidence_quote=quote,
                            reason="Check the current visible field evidence.")


@pytest.mark.parametrize("status", ["resolved", "unresolved", "not_applicable"])
def test_advisory_status_does_not_override_blank_readonly_field(status):
    assert write_prerequisite_diagnostics(form(), Memory(), [review(status)])


def test_visible_value_resolves_risk_without_claiming_persistence_and_optional_link_does_not_block():
    obs, memory = form(), Memory()
    obs.elements[0].value = "Observed value"
    assert not write_prerequisite_diagnostics(obs, memory, [])
    assert not memory.confirmed_actions and not memory.pending_writes


def test_only_current_field_specific_explicit_optional_label_can_waive_unknown_hint():
    obs = form()
    obs.text += "\nDerived Field (optional)\nOptional Link (optional)"
    assert not write_prerequisite_diagnostics(obs, Memory(), [review()])
    assert write_prerequisite_diagnostics(obs, Memory(), [review(quote="Optional Link (optional)")])
    assert write_prerequisite_diagnostics(obs, Memory(), [review(), review()])
    obs.text = "No optional label on this page"
    assert write_prerequisite_diagnostics(obs, Memory(), [review()])


def test_optional_annotation_in_observed_control_name_is_accepted():
    obs = form()
    obs.elements[0].name = "Derived Field (optional)"
    assert not write_prerequisite_diagnostics(obs, Memory(), [review()])


def test_required_marker_overrides_optional_advice_and_works_after_hint_cap():
    obs = form()
    obs.elements[0].value = "Observed"
    obs.elements += [Element(id=f"f{i}", name=f"Field {i}", role="textbox", required=True, value="ok")
                     for i in range(45)]
    obs.elements[-1].value = ""
    errors = write_prerequisite_diagnostics(obs, Memory(), [])
    assert errors == [{"type": "write_required_field_blank", "element_ref": "f44", "name": "Field 44"}]
    obs.elements[0].required = True
    obs.elements[0].value = ""
    obs.text += "\nDerived Field (optional)"
    assert write_prerequisite_diagnostics(obs, Memory(), [review()])[0]["type"] == "write_required_field_blank"


def test_all_scoped_prior_refusals_are_checked_without_planner_hint_cap():
    obs, memory = form(), Memory()
    obs.elements[0].value = "Observed"
    for i in range(22):
        obs.elements.append(Element(id=f"r{i}", name=f"Refusal {i}", role="textbox", value="ok"))
        memory.key_nodes[str(i)] = {
            "verification": "form_validation_rejected", "environment_id": memory.environment_id,
            "missing_fields": [f"Refusal {i}"],
            "form_scope": {"document_id": "doc", "form_title": "New Record"},
            "source": {"tab_id": "tab", "url": "about:blank"}}
    obs.elements[-1].value = ""
    assert write_prerequisite_diagnostics(obs, memory, [])[0]["name"] == "Refusal 21"
    obs.elements.pop()
    assert write_prerequisite_diagnostics(obs, memory, [])[0]["type"] == "write_prerequisite_not_observed_or_ambiguous"


async def test_blocked_save_repairs_to_source_inspection_before_scope_installation():
    obs, brain = form(), AsyncMock()
    brain.review.side_effect = [
        Feedback(next_goal="Save", stage_controls=[StageControl(element_ref="save", operations=["click"])],
                 stage_entry=StageEntry(intent="act", operation="click", element_ref="save")),
        Feedback(next_goal="Inspect the observed source; keep the draft unresolved",
                 dependency_reviews=[review("unresolved", "Derived Field")],
                 stage_controls=[StageControl(element_ref="source", operations=["click"])],
                 stage_entry=StageEntry(intent="locate", operation="click", element_ref="source"))]
    controller = DynamicController(definition(), AsyncMock(), AsyncMock(), feedback=brain)
    await controller.review(obs, phase="ui_checkpoint")
    assert brain.review.await_count == 2
    assert brain.review.call_args.kwargs["diagnostic"][0]["type"] == "write_derived_field_unresolved"
    assert set(controller.memory.feedback["execution_scope"]["bindings"]) == {"source"}
    assert any(e["kind"] == "write_prerequisite_rejected" for e in controller.events)
    controller.backend.execute.assert_not_awaited()
    assert not controller.pending and not controller.memory.pending_writes


async def test_dispatch_gate_covers_legacy_scope_and_unsubstantiated_resolved_claim():
    obs = form()
    controller = DynamicController(definition(), AsyncMock(), AsyncMock(), feedback=AsyncMock())
    save = next(a for a in generate_dynamic(obs, definition()) if a.element_ref == "save")
    assert await controller.perform(save, obs) == "write prerequisites unresolved; no write dispatched"
    controller.backend.execute.assert_not_awaited()
    assert not controller.pending and not controller.consumed
    # Even a previously valid review cannot resolve a field that is blank again.
    controller.memory.feedback["dependency_reviews"] = [review("resolved", "Observed").model_dump()]
    assert await controller.perform(save, obs)
    controller.backend.execute.assert_not_awaited()


async def test_execution_group_cannot_bypass_fresh_dispatch_check():
    obs, brain = form(), AsyncMock()
    obs.elements[0].value = "Observed value"
    brain.review.return_value = Feedback(next_goal="Save the draft",
        stage_controls=[StageControl(element_ref="save", operations=["click"])],
        stage_entry=StageEntry(intent="act", operation="click", element_ref="save"),
        execution_groups=[ExecutionGroup(goal="Save", actions=[GroupAction(element_ref="save", operation="click")])])
    controller = DynamicController(definition(), AsyncMock(), AsyncMock(), feedback=brain)
    await controller.review(obs, phase="initial")
    assert controller.execution_groups
    obs.elements[0].value = ""
    save = next(a for a in generate_dynamic(obs, definition()) if a.element_ref == "save")
    assert controller.stage_action_allowed(save, obs)
    assert await controller.perform(save, obs) == "write prerequisites unresolved; no write dispatched"
    assert not controller.pending and not controller.consumed
    controller.backend.execute.assert_not_awaited()


async def test_pending_unknown_transaction_is_retained_when_prerequisites_are_missing():
    obs = form()
    controller = DynamicController(definition(), AsyncMock(), AsyncMock(), feedback=AsyncMock())
    controller.pending = {"key": "original-unknown-write"}
    controller.memory.pending_writes["original-unknown-write"] = {"status": "unknown"}
    save = next(a for a in generate_dynamic(obs, definition()) if a.element_ref == "save")
    assert await controller.perform(save, obs)
    assert controller.pending == {"key": "original-unknown-write"}
    assert controller.memory.pending_writes["original-unknown-write"]["status"] == "unknown"
    controller.backend.execute.assert_not_awaited()
