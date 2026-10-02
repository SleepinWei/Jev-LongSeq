from unittest.mock import AsyncMock

import pytest

from jev_browser.dynamic import (
    DynamicController,
    Feedback,
    StageControl,
    StageEntry,
    action_key,
    generate_dynamic,
    stage_plan_diagnostics,
)
from jev_browser.protocol import Element, Observation, Operation, Task


def definition():
    return Task(id="entry", sandbox=True, control_mode="dynamic", objective="Create the journal")


def page():
    return Observation(
        observation_id="o",
        document_version="v",
        tab_id="finance",
        url="https://example.test/vendors",
        title="Vendors",
        text="Vendor created. Accounting",
        tabs={"hr": "https://example.test/exits"},
        elements=[
            Element(id="filter", role="button", name="Filter"),
            Element(id="quick", role="button", name="Quick new"),
        ],
    )


@pytest.mark.parametrize("case", ["missing", "unauthorized", "consumed", "old_verification"])
def test_stage_entry_rejects_nonexecutable_or_unrelated_contracts(case):
    obs, task = page(), definition()
    entry = StageEntry(intent="navigate", operation="click", element_ref="quick")
    feedback = Feedback(
        next_goal="Locate journal",
        stage_entry=entry,
        stage_controls=[StageControl(element_ref="quick", operations=["click"])],
    )
    consumed = set()
    if case == "missing":
        entry.element_ref = "accounting-not-observed"
    elif case == "unauthorized":
        feedback.stage_controls = [StageControl(element_ref="filter", operations=["click"])]
    elif case == "consumed":
        candidate = next(a for a in generate_dynamic(obs, task) if a.element_ref == "quick")
        consumed.add(action_key(candidate, obs))
    else:
        feedback.verification = {"goal": "Recheck old HR report", "fallback_goal": "Create journal"}
        feedback = Feedback.model_validate(feedback.model_dump())
    assert stage_plan_diagnostics(feedback, obs, task, consumed=consumed)


async def test_entry_survives_small_candidate_pages_and_blocks_unrelated_tab_switches():
    obs = page()
    obs.elements = [
        Element(id=f"e{i}", role="button", name=f"Other {i}") for i in range(40)
    ] + obs.elements
    task = definition()
    task.allowed_origins = ["https://example.test"]
    feedback = Feedback(
        next_goal="Use creation menu",
        stage_entry=StageEntry(intent="navigate", operation="click", element_ref="quick"),
        stage_controls=[
            StageControl(element_ref=e.id, operations=["click"]) for e in obs.elements[-40:]
        ],
    )
    brain = AsyncMock()
    brain.review.return_value = feedback
    agent = DynamicController(task, AsyncMock(), AsyncMock(), feedback=brain)
    await agent.review(obs, phase="step")
    actions = agent.generate_stage_candidates(obs, limit=8, offset=0)
    assert any(a.element_ref == "quick" for a in actions)
    assert not any(a.operation == Operation.SWITCH_TAB for a in actions)
    agent.backend.execute.assert_not_awaited()


def test_cross_tab_verification_must_target_the_entry_destination():
    task = definition()
    task.allowed_origins = ["https://example.test"]
    feedback = Feedback(
        next_goal="Inspect report",
        stage_entry=StageEntry(intent="verify", operation="switch_tab", tab_id="hr"),
        stage_controls=[],
        verification={
            "goal": "Read HR report",
            "fallback_goal": "Return to finance",
            "target_url": "https://example.test/exits?date=2026-06-30",
        },
    )
    assert not stage_plan_diagnostics(feedback, page(), task)
    feedback.verification.target_url = "https://example.test/vendors"
    assert stage_plan_diagnostics(feedback, page(), task)
