"""Visible link input and committed model value may differ until native blur."""
from playwright.async_api import Error

from jev_browser.browser import PlaywrightBackend
from jev_browser.dynamic import generate_dynamic
from jev_browser.protocol import Operation, Task


async def test_empty_link_clear_commits_before_same_record_is_selected_again():
    task = Task(id="link-clear", objective="Re-resolve the requested employee's Company",
                sandbox=True, control_mode="dynamic")
    async with PlaywrightBackend(task) as browser:
        await browser.load_html('''<label>Employee<input role="combobox" value="EMP-007"
            onblur="window.record.employee=this.value;"></label>
          <div role="option" onmousedown="event.preventDefault()" onclick="
            if(window.record.employee!=='EMP-007'){document.querySelector('#company').textContent='Example Company'};
            window.record.employee='EMP-007';document.querySelector('input').value='EMP-007';">
            EMP-007 Requested Employee</div>
          <div class="frappe-control"><label>Company</label>
            <div id="company" class="control-value like-disabled-input" style="height:30px"></div></div>
          <script>window.record={employee:'EMP-007'}</script>''')
        obs = await browser.observe()
        clear = next(a for a in generate_dynamic(obs, task) if a.operation == Operation.FILL and a.bound_value == "")
        receipt = await browser.execute(clear)
        assert receipt.status == "ok" and "automatic_blur=native_tab" in receipt.detail
        assert await browser.page.evaluate("window.record.employee") == ""
        fresh = await browser.observe()
        query = next(a for a in generate_dynamic(fresh, task) if a.operation == Operation.FILL and a.bound_value is None)
        query.bound_value = "Requested"
        assert (await browser.execute(query)).status == "ok"
        assert await browser.page.locator('input').evaluate("el => document.activeElement === el")
        assert await browser.page.evaluate("window.record.employee") == ""
        fresh = await browser.observe()
        option = next(a for a in generate_dynamic(fresh, task)
                      if a.operation == Operation.CLICK and a.description.startswith("option:"))
        assert (await browser.execute(option)).status == "ok"
        final = await browser.observe()
        assert next(e.value for e in final.elements if e.name == "Company") == "Example Company"


async def test_clear_blur_failure_is_unknown_and_not_retried(monkeypatch):
    task = Task(id="link-clear-error", objective="Clear the current Employee",
                sandbox=True, control_mode="dynamic")
    async with PlaywrightBackend(task) as browser:
        await browser.load_html('<label>Employee<input role="combobox" value="EMP-007"></label><button>Next</button>')
        obs = await browser.observe()
        clear = next(a for a in generate_dynamic(obs, task) if a.operation == Operation.FILL and a.bound_value == "")
        handle = (await browser.snapshot_handle.get_property(clear.element_ref)).as_element()
        browser.handles[clear.element_ref] = handle
        presses = []

        async def interrupted(key, **kwargs):
            presses.append(key)
            raise Error("interrupted native blur")

        monkeypatch.setattr(handle, "press", interrupted)
        receipt = await browser.execute(clear)
        assert receipt.status == "unknown" and presses == ["Tab"]
        assert await browser.page.locator('input').input_value() == ""
