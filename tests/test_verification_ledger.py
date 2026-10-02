from unittest.mock import AsyncMock

from jev_browser.dynamic import DynamicController, Feedback, planning_location
from jev_browser.protocol import Element, Observation, Receipt, Task


def report(query="", tab="hr"):
    return Observation(
        observation_id="o",
        document_version="v",
        tab_id=tab,
        url="https://example.test/exits" + query,
        title="Exits",
        text="Nothing to show",
    )


def controller():
    return DynamicController(
        Task(
            id="reports",
            sandbox=True,
            control_mode="dynamic",
            objective="Verify exit report and create vendor",
            allowed_origins=["https://example.test"],
        ),
        AsyncMock(),
        AsyncMock(),
        feedback=AsyncMock(),
    )


def plan(goal="Check exit report"):
    return Feedback(
        next_goal=goal, verification={"goal": goal, "fallback_goal": "Create vendor"}
    ).model_dump()


def test_paraphrase_query_and_tab_cannot_reset_exhausted_report_or_duplicate_obligation():
    agent = controller()
    agent.memory.feedback = plan()
    agent.arm_verification(report("?date=1"))
    assert agent.defer_verification(report("?date=1"))
    agent.memory.feedback = plan("Reload and inspect exact matching row")
    changed = report("?date=2#table", tab="another-hr-tab")
    agent.arm_verification(changed)
    assert agent.verification_exhausted(changed)
    assert agent.defer_verification(changed)
    assert len(agent.memory.unresolved_verifications) == 1
    assert agent.memory.unresolved_verifications[0]["status"] == "unresolved"
    assert agent.memory.export()["verification_ledger"] == agent.memory.verification_ledger


def test_active_allowance_survives_query_and_tab_changes():
    agent = controller()
    agent.memory.feedback = plan()
    first, second = report("?date=1"), report("?date=2", tab="other")
    agent.actions = 3
    agent.arm_verification(first)
    agent.actions = 7
    agent.arm_verification(second)
    assert (
        agent.verification_runs[planning_location(first)]
        == agent.verification_runs[planning_location(second)]
    )
    assert agent.verification_runs[planning_location(second)][0] == 3


def test_spa_routes_and_recreated_environments_have_distinct_verification_allowances():
    agent = controller()
    agent.memory.feedback = plan()
    assert agent.defer_verification(report("#/employee-exits?date=1"))
    assert agent.verification_exhausted(report("#/employee-exits?date=2"))
    assert not agent.verification_exhausted(report("#/general-ledger"))
    agent.memory.environment_id = "recreated"
    assert not agent.verification_exhausted(report("#/employee-exits?date=1"))


async def test_write_elsewhere_cannot_revive_exhausted_report_but_local_write_can():
    from jev_browser.dynamic import generate_dynamic

    agent = controller()
    agent.memory.feedback = plan()
    assert agent.defer_verification(report())
    agent.memory.feedback = Feedback(next_goal="Save vendor").model_dump()
    vendor = report().model_copy(
        update={
            "url": "https://example.test/vendors/new",
            "elements": [Element(id="save", role="button", name="Save")],
        }
    )
    agent.backend.execute.return_value = Receipt(action_id="save", status="ok")
    action = next(a for a in generate_dynamic(vendor, agent.task) if a.element_ref == "save")
    await agent.perform(action, vendor)
    assert agent.confirm_transition(
        "confirmed", vendor.model_copy(update={"text": "Vendor saved"}), "fresh readback"
    )
    agent.memory.feedback = plan()
    assert agent.verification_exhausted(report())
    local = report().model_copy(
        update={"elements": [Element(id="save", role="button", name="Save")]}
    )
    action = next(a for a in generate_dynamic(local, agent.task) if a.element_ref == "save")
    await agent.perform(action, local)
    assert agent.confirm_transition(
        "confirmed", local.model_copy(update={"text": "Report settings saved"}), "fresh readback"
    )
    agent.memory.feedback = plan()
    assert not agent.verification_exhausted(report())
    assert agent.memory.unresolved_verifications[0]["status"] == "unresolved"
