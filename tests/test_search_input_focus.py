"""Visible search textboxes keep their query; normal/date inputs still commit on blur."""
import pytest

from jev_browser.browser import PlaywrightBackend
from jev_browser.dynamic import generate_dynamic
from jev_browser.protocol import Operation, Task


@pytest.mark.parametrize("name", ["Search...", "Search…", "Search", "搜索"])
async def test_plain_text_search_in_grid_does_not_lose_query_or_options_on_automatic_tab(name):
    task = Task(id="account-search", sandbox=True, control_mode="dynamic",
                objective="Select the requested Rent account in the first row.")
    async with PlaywrightBackend(task) as browser:
        await browser.load_html(f'''<table><thead><tr><th>Account</th></tr></thead><tbody><tr><td>
            <input placeholder="{name}" type="text"
              oninput="document.querySelector('#result').hidden=false"
              onblur="this.value='';document.querySelector('#result').hidden=true">
            <div id="result" role="option" hidden>Rent Expense</div></td></tr></tbody></table>''')
        obs = await browser.observe()
        field = next(e for e in obs.elements if e.name == name and e.editable)
        assert field.role == "textbox" and field.grid_ref and field.row_ref
        action = next(a for a in generate_dynamic(obs, task)
                      if a.operation == Operation.FILL and a.element_ref == field.id and a.bound_value is None)
        action.bound_value = "Rent"
        receipt = await browser.execute(action)
        assert receipt.status == "ok"
        assert await browser.page.locator("input").input_value() == "Rent"
        assert await browser.page.locator("#result").is_visible()
        assert await browser.page.locator("input").evaluate("el => document.activeElement === el")
        fresh = await browser.observe()
        assert next(e for e in fresh.elements if e.name == name).value == "Rent"
        assert any(e.role == "option" and e.name == "Rent Expense" for e in fresh.elements)


@pytest.mark.parametrize("name,kind,value", [("Posting date", "text", "2026-06-30"),
    ("Email", "text", "ada@example.test"), ("Search budget", "text", "100")])
async def test_normal_and_date_inputs_still_receive_native_commit_blur(name, kind, value):
    task = Task(id="commit", sandbox=True, control_mode="dynamic", objective="Fill the requested field.")
    async with PlaywrightBackend(task) as browser:
        await browser.load_html(f'''<label>{name}<input type="{kind}" onblur="
            document.querySelector('#state').textContent='committed'"></label><button>Next</button>
            <output id="state">draft</output>''')
        obs = await browser.observe()
        action = next(a for a in generate_dynamic(obs, task)
                      if a.operation == Operation.FILL and a.bound_value is None)
        action.bound_value = value
        assert (await browser.execute(action)).status == "ok"
        assert await browser.page.locator("input").input_value() == value
        assert await browser.page.locator("#state").inner_text() == "committed"
