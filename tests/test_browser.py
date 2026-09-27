import asyncio

import pytest

from jev_browser.browser import PlaywrightBackend
from jev_browser.candidates import generate
from jev_browser.controller import Controller
from jev_browser.fixture import RulePlanner, RulePolicy, catalog_html, demo_task
from jev_browser.memory import Memory
from jev_browser.protocol import Binding, Budget, Decision, InteractionRule, Operation


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
