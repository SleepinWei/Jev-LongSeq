import asyncio

import pytest

from jev_browser.browser import PlaywrightBackend
from jev_browser.candidates import generate
from jev_browser.controller import Controller
from jev_browser.fixture import RulePlanner, RulePolicy, catalog_html, demo_task
from jev_browser.memory import Memory
from jev_browser.protocol import Binding, Budget, Decision, InteractionRule, Operation


async def test_rendered_refresh_icons_and_navigation_search_are_distinct():
    from jev_browser.protocol import Task

    definition = Task(id='report-controls', objective='Inspect a report', control_mode='dynamic', sandbox=True)
    async with PlaywrightBackend(definition) as browser:
        await browser.load_html('''
            <header><div><button>Search</button><kbd>⌘K</kbd></div></header>
            <main><label>Employee<input role="combobox"></label>
              <button><svg class="lucide-refresh-cw" width="20" height="20"><rect width="20" height="20"/></svg></button>
              <button><svg width="20" height="20"><defs><symbol id="icon-reload"><rect width="20" height="20"/></symbol></defs><use href="#icon-reload"/></svg></button>
              <button><svg class="icon-refresh" style="display:none"></svg></button>
            </main>''')
        obs = await browser.observe()
        search = next(e for e in obs.elements if e.name == 'Search')
        assert 'Navigation: Search ⌘K' in search.context
        assert 'Keyboard shortcut: ⌘K' in search.context
        assert search.search_query is None and search.search_scope is None
        assert any(e.name == 'refresh (icon control)' for e in obs.elements)
        assert any(e.name == 'reload (icon control)' for e in obs.elements)
        buttons = [e for e in obs.elements if e.role == 'button']
        assert buttons[-1].name == ''  # Hidden asset does not name a visible control.


async def test_options_keep_unique_owner_and_field_context():
    from jev_browser.protocol import Task

    definition = Task(id='local-options', objective='Choose a linked record', control_mode='dynamic', sandbox=True)
    async with PlaywrightBackend(definition) as browser:
        await browser.load_html('''
            <div><label>Employee<input role="combobox" value="Ada"></label>
              <div role="option">EMP-7 Ada</div></div>
            <div><label>Department<input role="combobox"></label></div>''')
        obs = await browser.observe()
        field = next(e for e in obs.elements if e.name == 'Employee')
        option = next(e for e in obs.elements if e.role == 'option')
        assert option.option_owner == field.id and field.popup_open
        assert option.context == 'Options for: Employee'
        assert not next(e for e in obs.elements if e.name == 'Department').popup_open


async def test_password_snapshot_distinguishes_empty_and_filled_without_exposing_value():
    from jev_browser.dynamic import action_key, generate_dynamic, semantic_key
    from jev_browser.protocol import Task

    task = Task(id="password", objective="Fill the password", control_mode="dynamic", sandbox=True)
    async with PlaywrightBackend(task) as browser:
        await browser.load_html('<label>Password <input type="password"></label>')
        empty = await browser.observe()
        assert empty.elements[0].value == ""
        action = next(a for a in generate_dynamic(empty, task) if a.operation == Operation.FILL)
        action.bound_value = "test-only-secret"
        assert (await browser.execute(action)).status == "ok"
        filled = await browser.observe()
        assert filled.elements[0].value == "[redacted]"
        assert "test-only-secret" not in filled.model_dump_json()
        assert semantic_key(empty) != semantic_key(filled)
        assert action_key(action, empty) != action_key(action, filled)
        await browser.page.locator('input').fill('')
        assert (await browser.observe()).elements[0].value == ""


async def test_visible_sibling_form_labels_name_fields_without_guessing_ambiguous_groups():
    from jev_browser.dynamic import generate_dynamic
    from jev_browser.protocol import Task

    task = Task(id="labels", objective="Fill Company and date", control_mode="dynamic", sandbox=True)
    async with PlaywrightBackend(task) as browser:
        await browser.load_html('''
            <div><label>Company</label><div><input role="combobox"></div></div>
            <div><label>Separation Begins On</label><div><input></div></div>
            <div><label>Ambiguous</label><input><input></div>
            <div><label style="display:none">Hidden label</label><input></div>
        ''')
        obs = await browser.observe()
        assert [e.name for e in obs.elements] == ["Company", "Separation Begins On", "", "", ""]
        descriptions = [a.description for a in generate_dynamic(obs, task)
                        if a.operation == Operation.FILL]
        assert any("Fill combobox: Company" in d for d in descriptions)
        assert any("Fill textbox: Separation Begins On" in d for d in descriptions)


@pytest.mark.parametrize("role", ["textbox", "combobox"])
async def test_dynamic_fill_commits_plain_input_but_keeps_link_dropdown_open(role):
    from jev_browser.dynamic import generate_dynamic
    from jev_browser.protocol import Task

    task = Task(id="commit-input", objective="Set the date or pick a User",
                control_mode="dynamic", sandbox=True)
    async with PlaywrightBackend(task) as browser:
        await browser.load_html(f'''<label>Value<input role="{role}"
            onchange="document.querySelector('p').textContent='Committed: '+this.value"
            onblur="document.querySelector('span').textContent='Dropdown closed'"></label>
            <button>Next</button><p>Uncommitted</p><span>Dropdown open</span>''')
        obs = await browser.observe()
        action = next(a for a in generate_dynamic(obs, task) if a.operation == Operation.FILL)
        action.bound_value = "2026-06-30"
        assert (await browser.execute(action)).status == "ok"
        assert await browser.page.locator('input').input_value() == "2026-06-30"
        if role == "textbox":
            assert await browser.page.locator('p').inner_text() == "Committed: 2026-06-30"
            assert await browser.page.locator('span').inner_text() == "Dropdown closed"
        else:
            assert await browser.page.locator('p').inner_text() == "Uncommitted"
            assert await browser.page.locator('span').inner_text() == "Dropdown open"


async def test_fill_losing_document_during_commit_is_unknown_and_not_replayed(monkeypatch):
    from playwright.async_api import Error

    from jev_browser.dynamic import generate_dynamic
    from jev_browser.protocol import Task

    task = Task(id="commit-race", objective="Fill once", control_mode="dynamic", sandbox=True)
    async with PlaywrightBackend(task) as browser:
        await browser.load_html('<label>Date<input></label><button>Next</button>')
        obs = await browser.observe()
        action = next(a for a in generate_dynamic(obs, task) if a.operation == Operation.FILL)
        action.bound_value = "2026-06-30"
        handle = (await browser.snapshot_handle.get_property(action.element_ref)).as_element()
        browser.handles[action.element_ref] = handle

        async def lost_context(*args, **kwargs):
            raise Error("Execution context was destroyed")

        monkeypatch.setattr(handle, 'press', lost_context)
        receipt = await browser.execute(action)
        assert receipt.status == "unknown"  # fill already happened; preflight stale would be unsafe.
        assert await browser.page.locator('input').input_value() == "2026-06-30"


@pytest.mark.parametrize("operation", [Operation.CLICK, Operation.WAIT])
async def test_navigation_during_action_preflight_is_stale_without_dispatch(monkeypatch, operation):
    from playwright.async_api import Error

    from jev_browser.dynamic import generate_dynamic
    from jev_browser.protocol import Task

    task = Task(id="navigation-race", objective="Click once", control_mode="dynamic", sandbox=True)
    async with PlaywrightBackend(task) as browser:
        await browser.load_html('<button onclick="document.querySelector(\'p\').textContent=\'clicked\'">'
                                'Continue</button><p>untouched</p>')
        obs = await browser.observe()
        action = next(a for a in generate_dynamic(obs, task) if a.operation == operation)

        async def navigation_race(*args):
            raise Error("Page.evaluate: Execution context was destroyed, most likely because of a navigation")

        monkeypatch.setattr(browser.page, "evaluate", navigation_race)
        receipt = await browser.execute(action)
        assert receipt.status == "stale"
        assert "preflight" in receipt.detail
        assert await browser.page.locator("p").inner_text() == "untouched"


@pytest.mark.parametrize("mode", ["structured", "dynamic"])
async def test_empty_http_error_stops_before_model_call_and_is_recoverable(tmp_path, mode):
    from jev_browser.dynamic import DynamicController
    from jev_browser.inspector import Store
    from jev_browser.protocol import Task

    class RejectedPage(PlaywrightBackend):
        async def _route(self, route):
            await route.fulfill(status=200 if route.request.url.endswith('/ok') else 403,
                                content_type="text/html", body="")

    class NoCalls:
        def __getattr__(self, name):
            raise AssertionError(f"HTTP error must stop before a model call: {name}")

    task = (Task(id="blocked", objective="read", control_mode="dynamic")
            if mode == "dynamic" else demo_task(1))
    task.sandbox = False
    task.start_url = "https://blocked.example/"
    task.allowed_origins = ["https://blocked.example"]
    async with RejectedPage(task, output=tmp_path, live_preview=True) as browser:
        obs = await browser.observe()
        assert obs.http_status == 403
        assert not obs.text
        controller = (DynamicController(task, browser, NoCalls(), feedback=NoCalls())
                      if mode == "dynamic" else Controller(task, browser, NoCalls(), mode="flat"))
        result = await controller.run()
        assert result.status == "needs_attention"
        assert "HTTP 403" in result.reason
        assert result.actions == 0
        await browser.page.goto("https://blocked.example/ok")
        assert (await browser.observe()).http_status == 200
    navigations = Store(tmp_path).navigations(tmp_path)
    assert [n["status"] for n in navigations] == [403, 200]


async def test_finish_reobserves_after_policy_latency():
    from jev_browser.protocol import Predicate

    task = demo_task(1)
    task.success_predicates = [Predicate(kind="text", value="Complete")]
    async with PlaywrightBackend(task) as browser:
        await browser.load_html("<p>Complete</p>")

        class DelayedFinish:
            async def choose(self, task, obs, memory, contract, candidates):
                await browser.page.locator("p").evaluate("el => el.textContent = 'Incomplete'")
                return Decision(
                    choice=next(a.id for a in candidates if a.operation == Operation.FINISH)
                )

        result = await Controller(task, browser, DelayedFinish(), mode="flat").run()
    assert result.status == "failed"
    assert result.false_completions == 1


@pytest.mark.parametrize(
    "mode,count,options",
    [
        ("flat", 4, {}),
        ("once", 4, {}),
        ("event", 32, {}),
        ("event", 12, {"popup": True, "injection": True}),
        ("event", 12, {"reorder": True}),
        ("event", 4, {"delayed_save_ms": 350}),
    ],
)
async def test_strict_catalog_completion(mode, count, options):
    task = demo_task(count)
    async with PlaywrightBackend(task) as browser:
        await browser.load_html(catalog_html(count, **options))
        controller = Controller(task, browser, RulePolicy(), planner=RulePlanner(), mode=mode)
        result = await controller.run()
        grade = await browser.page.evaluate("window.__grade()")
    assert result.status == "success", result.reason
    assert grade["strict_success"], grade
    assert grade["duplicates"] == 0
    assert len(grade["visited"]) == count
    if not options.get("reorder"):
        assert result.planner_calls == (0 if mode == "flat" else 1)
    else:
        assert any(e.get("reason") == "subtask_budget" for e in controller.events)
    assert len(controller.memory.facts) == count
    assert all(f.source.quote and f.source.observation_id for f in controller.memory.history)


async def test_dom_change_rejects_old_action_before_click():
    task = demo_task(1)
    async with PlaywrightBackend(task) as browser:
        await browser.load_html(catalog_html(1))
        obs = await browser.observe()
        action = next(
            a
            for a in generate(obs, task, Memory(), None)
            if a.element_ref and "Edit" in a.description
        )
        await browser.page.locator("h1").evaluate("el => el.textContent = 'changed'")
        receipt = await browser.execute(action)
        assert receipt.status == "stale"
        assert (await browser.page.evaluate("window.__grade()"))["visited"] == []


async def test_identical_rerender_cannot_reuse_detached_handle():
    task = demo_task(1)
    async with PlaywrightBackend(task) as browser:
        await browser.load_html(catalog_html(1))
        obs = await browser.observe()
        action = next(a for a in generate(obs, task, Memory(), None) if "Edit |" in a.description)
        await browser.page.get_by_role("button", name="Edit", exact=True).evaluate(
            "el => el.replaceWith(el.cloneNode(true))"
        )
        receipt = await browser.execute(action)
        assert receipt.status == "stale"


async def test_observation_excludes_hidden_and_offscreen_state():
    task = demo_task(1)
    async with PlaywrightBackend(task) as browser:
        await browser.load_html("""<p>Visible: yes</p><p hidden>Secret: hidden</p>
          <p style="display:none">Secret: display</p><p aria-hidden="true">Secret: aria</p>
          <div style="opacity:0"><p>Secret: transparent ancestor</p></div>
          <p style="margin-top:2000px">Secret: offscreen</p><input type="hidden" value="hidden-token">""")
        obs = await browser.observe()
        assert "Visible: yes" in obs.text
        assert "Secret" not in obs.text
        assert not obs.elements


async def test_unknown_write_is_not_repeated():
    task = demo_task(1)
    async with PlaywrightBackend(task) as browser:
        await browser.load_html(catalog_html(1, lost_ack=True))
        controller = Controller(task, browser, RulePolicy(), planner=RulePlanner())
        result = await controller.run()
        grade = await browser.page.evaluate("window.__grade()")
    assert result.status == "needs_attention"
    assert "no resubmission" in result.reason
    assert grade["duplicates"] == 0
    assert len(controller.memory.pending_writes) == 1
    assert (
        sum(e["kind"] == "action" and e["action"]["effect"] == "write" for e in controller.events)
        == 1
    )


class ChooseOperation:
    def __init__(self, operation):
        self.operation = operation

    async def choose(self, task, obs, memory, contract, candidates):
        return Decision(choice=next(a.id for a in candidates if a.operation == self.operation))


async def test_false_completion_is_rejected():
    task = demo_task(1)
    async with PlaywrightBackend(task) as browser:
        await browser.load_html(catalog_html(1))
        result = await Controller(
            task, browser, ChooseOperation(Operation.FINISH), mode="flat"
        ).run()
    assert result.status == "failed"
    assert result.false_completions == 1
    assert result.finish_requests == 1


async def test_no_progress_triggers_bounded_event_replanning():
    task = demo_task(1)
    async with PlaywrightBackend(task) as browser:
        await browser.load_html(catalog_html(1))
        controller = Controller(
            task,
            browser,
            ChooseOperation(Operation.EXTRACT),
            planner=RulePlanner(),
            budget=Budget(no_progress_limit=2, max_planner_calls=2),
        )
        result = await controller.run()
    assert result.status == "needs_attention"
    assert result.planner_calls == 2
    assert any(e.get("reason") == "no_progress" for e in controller.events)
    assert result.actions < 10


async def test_action_budget_and_wall_clock_deadline():
    task = demo_task(1)

    class Slow:
        async def choose(self, *_):
            await asyncio.sleep(2)

    async with PlaywrightBackend(task) as browser:
        await browser.load_html(catalog_html(1))
        limited = await Controller(
            task, browser, RulePolicy(), mode="flat", budget=Budget(max_actions=1)
        ).run()
        timed = await Controller(
            task, browser, Slow(), mode="flat", budget=Budget(max_seconds=0.1)
        ).run()
    assert limited.status == timed.status == "budget_exhausted"
    assert limited.actions == 1
    assert "wall-clock" in timed.reason


async def test_challenge_is_escalated_without_bypass():
    task = demo_task(1)
    async with PlaywrightBackend(task) as browser:
        await browser.load_html("<h1>Verify you are human</h1><button>Continue</button>")
        result = await Controller(task, browser, RulePolicy(), mode="flat").run()
    assert result.status == "needs_attention"
    assert result.actions == 0


async def test_form_parameters_come_from_trusted_bindings():
    task = demo_task(1)
    task.bindings = [
        Binding(name="person", value="Ada", source="user"),
        Binding(name="tier", value="pro", source="user"),
    ]
    task.rules = [
        InteractionRule(id="name", name="Name", operation=Operation.FILL, binding="person"),
        InteractionRule(id="tier", name="Tier", operation=Operation.SELECT, binding="tier"),
    ]
    async with PlaywrightBackend(task) as browser:
        await browser.load_html("""<label>Name<input></label><label>Tier<select>
          <option value="basic">Basic</option><option value="pro">Pro</option></select></label>""")
        obs = await browser.observe()
        action = next(
            a for a in generate(obs, task, Memory(), None) if a.operation == Operation.FILL
        )
        assert action.bound_value == "Ada"
        assert (await browser.execute(action)).status == "ok"
        obs = await browser.observe()
        action = next(
            a for a in generate(obs, task, Memory(), None) if a.operation == Operation.SELECT
        )
        assert (await browser.execute(action)).status == "ok"
        assert await browser.page.locator("input").input_value() == "Ada"
        assert await browser.page.locator("select").input_value() == "pro"


async def test_readonly_derived_display_required_star_and_grid_row_scope():
    from jev_browser.dynamic import generate_dynamic
    from jev_browser.protocol import Task

    task = Task(id='readonly', objective='Inspect the form', control_mode='dynamic', sandbox=True)
    async with PlaywrightBackend(task) as browser:
        await browser.load_html('''<style>label.reqd::after {content: '*';}</style>
            <div class="frappe-control"><label class="reqd">Company</label>
              <div class="control-value like-disabled-input">TechVista</div></div>
            <div class="frappe-control"><label class="reqd">Derived Empty</label>
              <div class="control-value like-disabled-input" style="height:30px"></div></div>
            <div class="frappe-control" style="display:none"><label>Hidden</label>
              <div class="control-value like-disabled-input">secret</div></div>
            <div class="grid-row"><span>Row 1</span><label>Activity<input></label></div>
            <div class="grid-row"><span>Row 2</span><label>Activity<input></label></div>''')
        browser.errors = ['page_error:derived link failed'] + ['blocked_request:noise'] * 30
        obs = await browser.observe()
        company = next(e for e in obs.elements if e.name == 'Company')
        assert company.value == 'TechVista' and company.required and company.read_only
        assert not company.enabled and not company.editable
        assert any(e.name == 'Derived Empty' and e.required and e.value == '' for e in obs.elements)
        assert not any(e.name == 'Hidden' for e in obs.elements)
        assert not any(a.element_ref == company.id for a in generate_dynamic(obs, task))
        rows = [e for e in obs.elements if e.name == 'Activity']
        assert 'Row 1' in rows[0].context and 'Row 2' not in rows[0].context
        assert 'Row 2' in rows[1].context and 'Row 1' not in rows[1].context
        assert 'page_error:derived link failed' in obs.errors


async def test_grounded_blur_commits_link_clear_without_clicking_an_option():
    from jev_browser.dynamic import generate_dynamic
    from jev_browser.protocol import Task

    task = Task(id='blur', objective='Restore unsaved draft', control_mode='dynamic', sandbox=True)
    async with PlaywrightBackend(task) as browser:
        await browser.load_html('''<label>Employee<input role="combobox" value="Ada"
            onblur="document.querySelector('p').textContent='Cleared: '+this.value"></label>
            <button>Save</button><p>Uncommitted</p>''')
        obs = await browser.observe()
        action = next(a for a in generate_dynamic(obs, task) if a.operation == Operation.FILL)
        action.bound_value = ''
        await browser.execute(action)
        assert (await browser.blur_input(obs, action.element_ref)).status == 'stale'
        fresh = await browser.observe()
        assert (await browser.blur_input(fresh, fresh.elements[0].id)).status == 'ok'
        assert await browser.page.locator('p').inner_text() == 'Cleared:'


async def test_recovered_runtime_exception_is_archived_but_new_identical_failure_stays_active():
    from jev_browser.protocol import Task

    task = Task(id='error-lifecycle', objective='Inspect recovery', control_mode='dynamic', sandbox=True)
    async with PlaywrightBackend(task) as browser:
        await browser.load_html('<p>Company TechVista</p>')
        failure = "page_error:Cannot read properties of undefined (reading 'fields_dict')"
        browser.errors.append(failure)
        before = await browser.observe()
        browser.acknowledge_runtime_recovery(before)
        after = await browser.observe()
        assert failure not in after.errors
        assert failure in browser.errors  # Full error provenance survives.
        with pytest.raises(ValueError, match='current observation'):
            browser.acknowledge_runtime_recovery(before)
        browser.errors.append(failure)
        assert failure in (await browser.observe()).errors


async def test_observed_escape_hint_activates_only_front_navigation_dialog():
    from jev_browser.dynamic import generate_dynamic
    from jev_browser.protocol import Task

    task = Task(id='palette', objective='Close navigation search', control_mode='dynamic', sandbox=True)
    async with PlaywrightBackend(task) as browser:
        await browser.load_html('''<div role="dialog" id="palette">
            <input role="combobox" aria-label="Search or type a command">
            <a href="#report">Employee Exits Report</a>
            <button><svg class="icon-search-help"></svg></button>
            <p><kbd style="cursor:pointer">esc</kbd> to close</p></div>
            <p id="result">Not closed</p>
            <script>window.keys=[];document.querySelector('input').addEventListener('keydown',e=>{
              window.keys.push(e.key);if(e.key==='Escape'){
                document.getElementById('palette').remove();
                document.getElementById('result').textContent='Palette closed';}});</script>''')
        obs = await browser.observe()
        assert any(e.name == 'help (icon control)' for e in obs.elements)
        shortcuts = [e for e in obs.elements if e.activation_key]
        assert len(shortcuts) == 1 and shortcuts[0].activation_key == 'Escape'
        assert len({e.id for e in obs.elements}) == len(obs.elements)
        action = next(a for a in generate_dynamic(obs, task) if a.element_ref == shortcuts[0].id)
        receipt = await browser.execute(action)
        assert receipt.status == 'ok' and 'observed Escape' in receipt.detail
        assert await browser.page.evaluate('window.keys') == ['Escape']
        fresh = await browser.observe()
        assert not fresh.dialogs and 'Palette closed' in fresh.text


@pytest.mark.parametrize('case', ['outside_dialog', 'hidden_hint', 'business_form', 'confirmation', 'wrong_key'])
async def test_escape_capability_requires_visible_unambiguous_navigation_hint(case):
    from jev_browser.protocol import Task

    task = Task(id='no-escape', objective='Read', control_mode='dynamic', sandbox=True)
    role = '' if case == 'outside_dialog' else 'role="dialog"'
    hint = '<kbd>enter</kbd> to close' if case == 'wrong_key' else '<kbd>esc</kbd> to close'
    if case == 'hidden_hint':
        hint = '<span style="display:none">' + hint + '</span>'
    extra = '<label>Draft<input value="Unsaved"></label>' if case == 'business_form' else ''
    if case == 'confirmation':
        extra = '<button>Yes</button><button>No</button>'
    async with PlaywrightBackend(task) as browser:
        await browser.load_html(f'<div {role}><input role="combobox" aria-label="Search">{extra}<p>{hint}</p></div>')
        assert not any(e.activation_key for e in (await browser.observe()).elements)


async def test_front_help_dialog_hides_background_palette_and_escape_capability():
    from jev_browser.protocol import Task

    task = Task(id='modal-stack', objective='Close help', control_mode='dynamic', sandbox=True)
    async with PlaywrightBackend(task) as browser:
        await browser.load_html('''<div role="dialog" style="position:fixed;z-index:100;top:20px">
          <input role="combobox" aria-label="Search"><p><kbd>esc</kbd> to close</p>
          <button>Background action</button></div>
          <div role="dialog" style="position:fixed;z-index:200;top:40px">
            <h2>Search Help</h2><button>Close Help</button></div>''')
        obs = await browser.observe()
        assert len(obs.dialogs) == 1 and 'Search Help' in obs.dialogs[0]
        assert [e.name for e in obs.elements] == ['Close Help']
        assert not any(e.activation_key for e in obs.elements)


async def test_escape_preflight_rejects_replaced_confirmation_without_key_dispatch():
    from jev_browser.dynamic import generate_dynamic
    from jev_browser.protocol import Task

    task = Task(id='escape-stale', objective='Close navigation search', control_mode='dynamic', sandbox=True)
    async with PlaywrightBackend(task) as browser:
        await browser.load_html('''<div role="dialog"><input role="combobox" aria-label="Search">
          <p><kbd>esc</kbd> to close</p></div>
          <script>window.keys=[];document.addEventListener('keydown',e=>window.keys.push(e.key));</script>''')
        obs = await browser.observe()
        shortcut = next(e for e in obs.elements if e.activation_key)
        action = next(a for a in generate_dynamic(obs, task) if a.element_ref == shortcut.id)
        await browser.page.locator('[role=dialog]').evaluate("el=>el.innerHTML='<button>Yes</button><button>No</button>'")
        assert (await browser.execute(action)).status == 'stale'
        assert await browser.page.evaluate('window.keys') == []
