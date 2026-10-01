import pytest

from jev_browser.browser import PlaywrightBackend
from jev_browser.context_budget import ContextBudgetExceeded, project_request
from jev_browser.dynamic import DynamicController, Feedback, evidence_text, generate_dynamic
from jev_browser.memory import Memory
from jev_browser.models import state
from jev_browser.protocol import (
    Budget,
    Decision,
    Element,
    GridCell,
    GridRow,
    Observation,
    Operation,
    Receipt,
    Task,
    VisibleGrid,
)


def task():
    return Task(id="grid", control_mode="dynamic", sandbox=True,
                objective="Add a row and enter Revoke system access in that row.")


GRID_HTML = r'''<div class="grid-field"><label>Activities</label>
<div class="grid-heading-row"><div class="grid-static-col">No.</div>
<div class="grid-static-col">Activity Name</div><div class="grid-static-col">User</div></div>
<div class="grid-row"><div class="grid-static-col row-index">1</div>
<div class="grid-static-col"><label>Activity Name<input value="Return company laptop"></label></div>
<div class="grid-static-col"><label>User<input role="combobox" value="rajesh@example.com"></label></div></div>
<button onclick="
const row=document.querySelector('.grid-row');
row.children[1].innerHTML='<button>Return company laptop</button>';
row.children[2].innerHTML='<a href=\'#\'>rajesh@example.com</a>';
const next=document.createElement('div');next.className='grid-row';
next.innerHTML='<div class=\'grid-static-col row-index\'>2</div>'+
'<div class=\'grid-static-col\'><label>Activity Name<input></label></div>'+
'<div class=\'grid-static-col\'><label>User<input role=\'combobox\'></label></div>';
row.after(next);">Add row</button></div>'''


@pytest.mark.parametrize("footer_outside_grid", [False, True])
async def test_grid_observation_preserves_cell_values_when_editor_collapses(footer_outside_grid):
    html = GRID_HTML
    if footer_outside_grid:
        html = html.replace('<div class="grid-field"><label>Activities</label>',
                            '<div class="frappe-control"><label>Activities</label><div class="form-grid">')
        html = html.replace('<button onclick=', '</div><button onclick=')
    async with PlaywrightBackend(task()) as browser:
        await browser.load_html(html + '<div class="grid-field" style="display:none">'
                                '<div class="grid-row">hidden row</div></div>')
        before = await browser.observe()
        assert len(before.grids) == 1
        grid = before.grids[0]
        assert len(grid.id) == 17  # Stable compact ref, rather than a repeated DOM path.
        assert grid.name == "Activities" and len(grid.rows) == 1
        add = next(e for e in before.elements if e.name == "Add row")
        assert add.grid_ref == grid.id and add.row_ref is None
        action = next(a for a in generate_dynamic(before, task()) if a.element_ref == add.id)
        assert (await browser.execute(action)).status == "ok"
        after = await browser.observe()
        assert after.grids[0].id == grid.id
        assert [r.key for r in after.grids[0].rows] == ["1", "2"]
        assert after.grids[0].rows[0].cells == grid.rows[0].cells
        inputs = [e for e in after.elements if e.editable]
        assert inputs and all(e.row_ref == "2" and e.value == "" for e in inputs)
        assert f"Grid {grid.id}; row 1; Activity Name = Return company laptop" in evidence_text(after)
        assert f"Grid {grid.id}; row 2; Activity Name = " in evidence_text(after)


async def test_pending_model_can_continue_to_fill_new_row_without_repeating_add():
    class Policy:
        async def choose(self, task, obs, memory, contract, candidates):
            if memory.pending_writes:
                return Decision(choice=next(a.id for a in candidates if a.operation == Operation.WAIT),
                                outcome="pending")
            if len(obs.grids[0].rows) == 1:
                target = next(e for e in obs.elements if e.name == "Add row")
                operation = Operation.CLICK
            else:
                target = next(e for e in obs.elements if e.row_ref == "2" and e.name == "Activity Name")
                operation = Operation.FILL
            return Decision(choice=next(a.id for a in candidates
                                        if a.element_ref == target.id and a.operation == operation))

    class Brain:
        phases = []

        async def review(self, *args, **kwargs):
            self.phases.append(kwargs["phase"])
            return Feedback(next_goal="Add a row, then populate its Activity Name.", last_outcome="pending")

        async def value(self, *args):
            return "Revoke system access"

    async with PlaywrightBackend(task()) as browser:
        await browser.load_html(GRID_HTML)
        brain = Brain()
        agent = DynamicController(task(), browser, Policy(), feedback=brain,
                                  budget=Budget(max_actions=2, readback_waits=2))
        result = await agent.run()
        assert result.status == "budget_exhausted"
        after = await browser.observe()
        assert len(after.grids[0].rows) == 2
        assert next(e.value for e in after.elements if e.name == "Activity Name") == "Revoke system access"
        proof = next(e for e in agent.events if e["kind"] == "transition_confirmed")
        assert proof["basis"] == "fresh_visible_grid_append"
        assert proof["confirmation_scope"] == "draft_row_added"
        assert brain.phases == ["initial", "draft_row_added"]
        assert not any(e["kind"] == "brain_requested" and e["reason"] == "action_readback"
                       for e in agent.events)


async def test_generic_append_confirmation_requests_new_row_guidance():
    async with PlaywrightBackend(task()) as browser:
        await browser.load_html(GRID_HTML)
        before = await browser.observe()
        agent = DynamicController(task(), browser, None, feedback=None)
        action = next(a for a in generate_dynamic(before, task()) if a.operation == Operation.CLICK
                      and next(e for e in before.elements if e.id == a.element_ref).name == 'Add row')
        await agent.perform(action, before)
        after = await browser.observe()
        assert agent.confirm_transition('confirmed', after, 'model_readback')
        assert agent.ui_review_due and not agent.stage_review_due
        assert not agent.memory.confirmed_actions[-1]['business_commit_confirmed']


def observations(initial_rows=1):
    cell = GridCell(column="Activity", value="Original activity")
    original = GridRow(key="1", cells=[cell])
    grid = VisibleGrid(id="grid-1", name="Activities", rows=[original] if initial_rows else [])
    before = Observation(observation_id="before", document_version="v1", tab_id="tab-0",
                         url="about:blank", title="Draft", text="Not Saved", grids=[grid],
                         elements=[Element(id="add", role="button", name="Add row", grid_ref=grid.id)])
    after = before.model_copy(deep=True)
    after.observation_id, after.document_version = "after", "v2"
    field = Element(id="new-field", role="textbox", name="Activity", grid_ref=grid.id,
                    row_ref=str(initial_rows + 1), editable=True)
    after.elements.append(field)
    after.grids[0].rows.append(GridRow(key=field.row_ref, cells=[GridCell(column="Activity", value="")],
                                      control_refs=[field.id]))
    return before, after


class Backend:
    calls = 0

    async def execute(self, action):
        self.calls += 1
        return Receipt(action_id=action.id, status="ok")


@pytest.mark.parametrize("initial_rows", [0, 1])
async def test_local_grid_append_confirmation_is_not_a_business_commit(initial_rows):
    before, after = observations(initial_rows)
    backend = Backend()
    agent = DynamicController(task(), backend, None, feedback=None)
    action = next(a for a in generate_dynamic(before, task()) if a.element_ref == "add")
    await agent.perform(action, before)
    assert agent.confirm_visible_grid_row(after)
    assert not agent.pending and not agent.memory.pending_writes
    assert agent.last_transition["business_commit_confirmed"] is False
    assert agent.last_transition["readback_proof"]["added_row"]["key"] == str(initial_rows + 1)
    assert not agent.memory.feedback.get("complete") and backend.calls == 1


@pytest.mark.parametrize("case", ["unknown", "same_observation", "other_grid", "lost_value",
                                 "duplicate_rows", "multiple_new_rows", "missing_cells", "no_field",
                                 "dialog", "loading", "page_error", "other_url", "save"])
async def test_grid_append_does_not_clear_uncertain_or_unrelated_mutations(case):
    before, after = observations()
    if case == "save":
        before.elements[0].name = "Save"
    agent = DynamicController(task(), Backend(), None, feedback=None)
    action = next(a for a in generate_dynamic(before, task()) if a.element_ref == "add")
    await agent.perform(action, before)
    if case == "unknown":
        agent.pending["dispatch_status"] = "unknown"
    elif case == "same_observation":
        after.observation_id = before.observation_id
    elif case == "other_grid":
        after.grids[0].id = "other"
    elif case == "lost_value":
        after.grids[0].rows[0].cells[0].value = ""
    elif case == "duplicate_rows":
        after.grids[0].rows[-1].key = "1"
    elif case == "multiple_new_rows":
        after.grids[0].rows.append(after.grids[0].rows[-1].model_copy(deep=True))
    elif case == "missing_cells":
        after.grids[0].rows[-1].cells = []
    elif case == "no_field":
        after.elements[-1].editable = False
    elif case == "dialog":
        after.dialogs = ["Confirm Submit?"]
    elif case == "loading":
        after.loading = True
    elif case == "page_error":
        after.errors = ["page_error: UI crashed"]
    elif case == "other_url":
        after.url = "https://other.test"
    assert not agent.confirm_visible_grid_row(after)
    assert agent.pending and agent.memory.pending_writes


def test_context_projection_keeps_exact_current_grid_rows_and_empty_values():
    _, obs = observations()
    payload = {"state": state(task(), obs, Memory(), None), "questions": {}}
    projected, _ = project_request(payload)
    current = projected["state"]["untrusted_observation"]
    assert current["grids"] == payload["state"]["untrusted_observation"]["grids"]
    control = {**current["control_defaults"], **current["controls"]["new-field"]}
    assert control["value"] == "" and control["row_ref"] == "2"


async def test_context_overflow_is_not_retried_as_invalid_feedback():
    class Brain:
        calls = 0

        async def review(self, *args, **kwargs):
            self.calls += 1
            raise ContextBudgetExceeded("protected context exceeds budget", {"max_bytes": 1})

    brain = Brain()
    agent = DynamicController(task(), Backend(), None, feedback=brain)
    before, _ = observations()
    action = next(a for a in generate_dynamic(before, task()) if a.element_ref == "add")
    await agent.perform(action, before)
    with pytest.raises(ContextBudgetExceeded):
        await agent.review(before, phase="action_readback")
    assert brain.calls == 1
    assert agent.pending and agent.memory.pending_writes
    assert not any(e["kind"] == "invalid_feedback" for e in agent.events)
