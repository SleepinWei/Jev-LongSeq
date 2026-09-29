from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

from playwright.async_api import Error, TimeoutError, async_playwright

from .candidates import allowed_url
from .protocol import Action, Element, Observation, Operation, Receipt, Task, digest

NATIVE_SELECTOR = ('a[href],button,input,textarea,select,summary,[contenteditable="true"],'
            '[role="button"],[role="link"],[role="textbox"],[role="combobox"],'
            '[role="checkbox"],[role="radio"],[role="tab"],[role="option"],[role="menuitem"]')
SELECTOR = NATIVE_SELECTOR + ',div,span,img,svg,[onclick],[tabindex]'
# Reads rendered DOM only. No application globals, hidden attributes or backend state.
SNAPSHOT = r"""selector => {
  const visible = el => {
    if (!el || el.closest('[hidden],[inert],[aria-hidden="true"],script,style,noscript')) return false;
    for (let node = el; node; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (style.opacity === '0' || style.visibility === 'hidden' || style.display === 'none') return false;
    }
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0'
      && r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0
      && r.top < innerHeight && r.left < innerWidth;
  };
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const lines = [];
  while (walker.nextNode()) {
    const n = walker.currentNode;
    if (visible(n.parentElement) && n.textContent.trim()) lines.push(n.textContent.trim());
  }
  const modal = [...document.querySelectorAll('dialog[open],[role="dialog"]')].filter(visible);
  const nativeSelector = __NATIVE_SELECTOR__, custom = new Set();
  const iconName = el => [...el.querySelectorAll('img[alt],svg[aria-label],svg title')]
    .filter(visible).map(n => n.getAttribute('alt') || n.getAttribute('aria-label') || n.textContent)
    .filter(Boolean).join(' ');
  // An icon without accessible text still has rendered asset/class hints. These
  // are descriptive, untrusted labels, never permission or invented navigation.
  const iconHint = el => {
    if (el.matches('a[href],[role="link"]')) return '';
    const icons = [el, ...[...el.querySelectorAll('img,svg,use')].slice(0, 8)];
    const tokens = icons.map(n => [n.getAttribute('class'),
      n.matches('img') ? n.getAttribute('src') : '',
      n.matches('svg,use') ? n.getAttribute('href') || n.getAttribute('xlink:href') : '']
      .filter(Boolean).join(' '))
      .join(' ').replace(/([a-z])([A-Z])/g, '$1 $2').toLowerCase();
    for (const word of ['search','submit','close','menu','next','previous','back','send','add','remove','delete']) {
      if (new RegExp('(?:^|[^a-z])' + word + '(?=$|[^a-z])').test(tokens))
        return word + ' (icon control)';
    }
    return '';
  };
  const controls = [...document.querySelectorAll(selector)].map((el, index) => {
    if (!visible(el) || (modal.length && !modal.some(m => m.contains(el)))) return null;
    if (!el.matches(nativeSelector)) {
      if (el.closest(nativeSelector) || el.querySelector(nativeSelector)) return null;
      const explicit = el.hasAttribute('onclick') || el.tabIndex >= 0;
      if (!explicit && getComputedStyle(el).cursor !== 'pointer') return null;
      for (let parent = el.parentElement; parent; parent = parent.parentElement) {
        if (custom.has(parent)) return null;
      }
      if (getComputedStyle(el).pointerEvents === 'none') return null;
      custom.add(el);
    }
    const tag = el.tagName.toLowerCase();
    const labelText = label => {
      const texts = [], walker = document.createTreeWalker(label, NodeFilter.SHOW_TEXT);
      while(walker.nextNode()) {
        const n = walker.currentNode;
        if (visible(n.parentElement) && !n.parentElement.closest('select,textarea,input')) texts.push(n.textContent.trim());
      }
      return texts.filter(Boolean).join(' ');
    };
    const labels = el.labels ? [...el.labels].filter(visible).map(labelText).join(' ') : '';
    const labelled = (el.getAttribute('aria-labelledby') || '').split(/\s+/)
      .map(id => document.getElementById(id)).filter(x => x && visible(x)).map(labelText).join(' ');
    const name = labelled || el.getAttribute('aria-label') || labels || labelText(el) ||
      el.getAttribute('alt') || iconName(el) || el.getAttribute('title') ||
      el.getAttribute('placeholder') || iconHint(el) || '';
    const inputRoles = {checkbox:'checkbox',radio:'radio',button:'button',submit:'button',reset:'button',range:'slider',file:'button'};
    const role = el.getAttribute('role') || (tag === 'input' ? (inputRoles[el.type] || 'textbox') :
      ({a:'link',button:'button',summary:'button',textarea:'textbox',select:'combobox'}[tag] ||
       (custom.has(el) ? 'button' : 'textbox')));
    const editable = !el.readOnly && (tag === 'textarea' || el.isContentEditable ||
      (tag === 'input' && ['text','search','email','url','tel','password','number'].includes(el.type)));
    const row = el.closest('tr,[role="row"],fieldset,form');
    return {index, role, name:name.trim(), value:el.type === 'password' ? '[redacted]' : (el.value || ''),
      enabled:!el.matches(':disabled') && !el.closest('[aria-disabled="true"]'),
      editable, selectable:tag === 'select',
      checked: ['checkbox','radio'].includes(role) ? (el.checked ?? el.getAttribute('aria-checked') === 'true') : null,
      context:row ? labelText(row) : '', href:tag === 'a' ? el.href : null,
      options:tag === 'select' ? [...el.options].filter(x => !x.disabled && !x.hidden).map(x => x.value) : []};
  }).filter(Boolean);
  const nodes = document.querySelectorAll(selector);
  for (const c of controls) {
    if (c.role !== 'button' || !/^(search(?: \(icon control\))?|搜索|搜尋)$/i.test(c.name)) continue;
    for (let scope = nodes[c.index].parentElement; scope && scope !== document.body; scope = scope.parentElement) {
      const fields = controls.filter(f => f.editable && scope.contains(nodes[f.index]));
      if (fields.length > 1) break;
      if (fields.length === 1) {
        const field = nodes[fields[0].index];
        if (field.type === 'password') break;
        c.search_query = fields[0].value;
        c.search_scope = field.id || field.getAttribute('name') || fields[0].name;
        break;
      }
    }
  }
  return {url:location.href,title:document.title,text:lines.join('\n'),controls,
    visible_frames:[...document.querySelectorAll('iframe,frame')].filter(visible).length,
    dialogs:modal.map(x => x.innerText),loading:document.readyState === 'loading' ||
      [...document.querySelectorAll('[aria-busy="true"]')].some(visible)};
}""".replace('__NATIVE_SELECTOR__', json.dumps(NATIVE_SELECTOR))

# Capture rendered state and the corresponding DOM nodes in one browser turn.
# Keep one remote object, resolving only the node actually chosen for an action.
BOUND_SNAPSHOT = """selector => {
  const raw = (""" + SNAPSHOT + """)(selector);
  const nodes = document.querySelectorAll(selector), search = {}, bound = {};
  for (const c of raw.controls) {
    const ref = 'e' + c.index, node = nodes[c.index];
    bound[ref] = node;
    search[ref] = !!(node.matches('input[type="search"]') || node.closest('[role="search"]'));
  }
  return {raw, search, ...bound};
}"""


class PlaywrightBackend:
    """Public Playwright API. Exactly one action per execute() call."""

    def __init__(
        self,
        task: Task,
        *,
        headless: bool = True,
        timeout_ms: int = 3000,
        output: Path | None = None,
        live_preview: bool = False,
    ):
        self.task, self.headless, self.timeout_ms, self.output = task, headless, timeout_ms, output
        self.errors: list[str] = []
        self.pages = {}
        self.handles = {}
        self.last: Observation | None = None
        self.browser_version = ""
        self.live_preview = live_preview
        self.preview = None
        self.navigation_status = {}
        self.snapshot_handle = None
        self.snapshot_raw = None
        self.preview_binding = None
        self.search_refs = {}
        self.observer = None

    async def __aenter__(self):
        self.pw = await async_playwright().start()
        try:
            self.browser = await self.pw.chromium.launch(headless=self.headless)
            self.browser_version = self.browser.version
            self.context = await self.browser.new_context(
                viewport={"width": 1280, "height": 900},
                service_workers="block",
                accept_downloads=False,
            )
            self.context.set_default_timeout(self.timeout_ms)
            await self.context.route("**/*", self._route)
            self.context.on("page", self._register)
            if self.output:
                self.output.mkdir(parents=True, exist_ok=True)
                await self.context.tracing.start(screenshots=True, snapshots=True, sources=False)
            self.page = await self.context.new_page()
            if self.live_preview and self.output:
                from .preview import LivePreview

                self.preview = LivePreview(self.output, self.capture_preview, archive=True)
                self.preview.start()
            if self.task.start_url != "about:blank":
                if not allowed_url(self.task.start_url, self.task):
                    raise ValueError("start URL is outside allowed origins")
                await self.page.goto(self.task.start_url, wait_until="domcontentloaded")
            return self
        except BaseException:
            if self.preview:
                await self.preview.close()
            if hasattr(self, "browser"):
                await self.browser.close()
            await self.pw.stop()
            raise

    async def _route(self, route):
        if allowed_url(route.request.url, self.task) and not self.task.sandbox:
            await route.continue_()
        else:
            self.errors.append(f"blocked_request:{route.request.url}")
            await route.abort("blockedbyclient")

    def _register(self, page):
        if page in self.pages.values():
            return
        self.pages[f"tab-{len(self.pages)}"] = page
        page.on("response", self._response)
        page.on("pageerror", lambda error: self.errors.append(f"page_error:{str(error)[:300]}"))
        page.on("dialog", self._dialog)

    def _response(self, response):
        request = response.request
        if request.is_navigation_request() and request.frame == request.frame.page.main_frame:
            self.navigation_status[request.frame.page] = (response.url, response.status)

    async def _dialog(self, dialog):
        self.errors.append(f"native_dialog_dismissed:{dialog.type}:{dialog.message[:200]}")
        await dialog.dismiss()

    async def load_html(self, html: str):
        if not self.task.sandbox:
            raise ValueError("inline fixtures require sandbox mode")
        await self.page.set_content(html, wait_until="domcontentloaded")

    async def capture_preview(self):
        page, binding = self.page, self.preview_binding
        before = await self._preview_geometry(binding)
        image = await page.screenshot(type="jpeg", quality=65, timeout=2000)
        after = await self._preview_geometry(binding)
        # Never paint boxes from another document or from a moving layout onto a frame.
        if before != after or page != self.page or binding != self.preview_binding:
            after = {"overlays": []}
        viewport = page.viewport_size or {"width": 1280, "height": 900}
        return image, page.url, {**viewport, **after}

    async def _preview_geometry(self, binding):
        if not binding:
            return {"overlays": []}
        handle, obs = binding
        try:
            geometry = await handle.evaluate("""s => {
              if (s.raw.url !== location.href) return {overlays:[]};
              const overlays = [];
              for (const c of s.raw.controls) {
                const id = 'e' + c.index, node = s[id];
                if (!node?.isConnected || !node.checkVisibility({checkOpacity:true,
                    checkVisibilityCSS:true}) || node.closest('[aria-hidden="true"],[inert]')) continue;
                const r = node.getBoundingClientRect();
                if (r.width <= 0 || r.height <= 0 || r.bottom <= 0 || r.right <= 0 ||
                    r.top >= innerHeight || r.left >= innerWidth) continue;
                overlays.push({id, label:c.name, role:c.role, editable:c.editable,
                  rect:{x:r.x,y:r.y,w:r.width,h:r.height}});
              }
              return {url:location.href,width:innerWidth,height:innerHeight,
                scroll_x:scrollX,scroll_y:scrollY,overlays};
            }""")
            return {**geometry, "observation_id": obs.observation_id, "tab_id": obs.tab_id}
        except Error:
            return {"overlays": []}  # Navigation/disposal races are normal during preview.

    async def observe(self) -> Observation:
        for attempt in range(3):
            try:
                await self._measure("browser.wait_dom", lambda: self.page.wait_for_load_state(
                    "domcontentloaded", timeout=10000))
                return await self._observe_once()
            except Error as exc:
                if attempt == 2 or not any(message in str(exc) for message in (
                    "Execution context was destroyed", "Cannot find context")):
                    raise
                # A link can commit between the two snapshot reads. Re-observe the
                # new document without replaying the action that initiated navigation.
                await self.page.wait_for_load_state("domcontentloaded", timeout=10000)
                await asyncio.sleep(0.05)

    async def _observe_once(self) -> Observation:
        await self._measure("browser.release_handles", self._release_handles)
        self.snapshot_handle = await self._measure(
            "browser.snapshot", self.page.evaluate_handle, BOUND_SNAPSHOT, SELECTOR)
        captured = await self._measure("browser.snapshot_read", self.snapshot_handle.evaluate,
                                       "s => ({raw:s.raw, search:s.search})")
        raw = self.snapshot_raw = captured["raw"]
        self.search_refs = captured["search"]
        elements = [Element(id=f"e{c['index']}", **{k: v for k, v in c.items() if k != "index"})
                    for c in raw["controls"]]
        obs = self._observation(raw, elements)
        self.preview_binding = (self.snapshot_handle, obs)
        return obs

    async def _measure(self, name, function, *args, **kwargs):
        if self.observer:
            return await self.observer.measure(name, function, *args, **kwargs)
        return await function(*args, **kwargs)

    async def _release_handles(self):
        self.preview_binding = None
        old_handles, snapshot = self.handles, self.snapshot_handle
        self.handles, self.snapshot_handle = {}, None
        for old in old_handles.values():
            if old:
                await old.dispose()
        if snapshot:
            await snapshot.dispose()

    def _observation(self, raw, elements):
        tab_id = next(k for k, p in self.pages.items() if p == self.page)
        text = raw["text"].casefold()
        obs = Observation(
            observation_id=uuid.uuid4().hex,
            tab_id=tab_id,
            document_version=digest(raw),
            url=raw["url"],
            title=raw["title"],
            text=raw["text"],
            elements=elements,
            dialogs=raw["dialogs"],
            loading=raw["loading"],
            http_status=self.navigation_status.get(self.page, (None, None))[1],
            errors=self.errors[-10:],
            challenge=any(s in text for s in ("verify you are human", "captcha", "access denied")),
            tabs={k: p.url for k, p in self.pages.items() if not p.is_closed()},
        )
        if raw["visible_frames"]:
            obs.errors.append("unsupported_iframe")
        self.last = obs
        return obs

    async def execute(self, action: Action) -> Receipt:
        started = time.monotonic()

        def receipt(status, detail=""):
            return Receipt(
                action_id=action.id,
                status=status,
                detail=detail,
                duration_s=time.monotonic() - started,
                write_key=action.write_key,
            )

        if not self.last or (
            action.observation_id,
            action.document_version,
            action.tab_id,
            action.frame_id,
        ) != (self.last.observation_id, self.last.document_version, self.last.tab_id, "main"):
            return receipt("stale", "action is not grounded in the current observation")
        current = await self.page.evaluate(SNAPSHOT, SELECTOR)
        changed = digest(current) != action.document_version
        # Only search interactions can tolerate unrelated feed changes. Other
        # writes retain the full-document check. WAIT is an inert read operation.
        search = (self.task.control_mode == "dynamic" and
                  self.search_refs.get(action.element_ref, False) and
                  action.operation in {Operation.FILL, Operation.CLICK})
        if changed:
            if self.task.control_mode == "dynamic" and action.operation == Operation.WAIT:
                pass
            elif not search or any(current[k] != self.snapshot_raw[k]
                                   for k in ("url", "title", "dialogs")):
                return receipt("stale", "document changed since observation")
        handle = None
        if action.element_ref:
            if action.element_ref not in self.handles:
                self.handles[action.element_ref] = (
                    await self.snapshot_handle.get_property(action.element_ref)).as_element()
            handle = self.handles[action.element_ref]
            if not handle or not await handle.evaluate(
                "(el, arg) => el.isConnected && document.querySelectorAll(arg.selector)[arg.index] === el",
                {"selector": SELECTOR, "index": int(action.element_ref[1:])},
            ):
                return receipt("stale", "element was detached or replaced")
            if changed and search:
                if current["visible_frames"] or any(
                    marker in current["text"].casefold()
                    for marker in ("verify you are human", "captcha", "access denied")
                ):
                    return receipt("stale", "search page requires a fresh safety observation")
                index = int(action.element_ref[1:])
                before = next((c for c in self.snapshot_raw["controls"] if c["index"] == index), None)
                after = next((c for c in current["controls"] if c["index"] == index), None)
                if before != after or not await handle.evaluate(
                    "el => !!(el.matches('input[type=search]') || el.closest('[role=search]'))"
                ):
                    return receipt("stale", "search target or its context changed")
        try:
            op = action.operation
            if op == Operation.CLICK:
                # The controller observes the destination and loading state next.
                # Do not wait for every image/resource before returning a link receipt.
                element = next((e for e in self.last.elements if e.id == action.element_ref), None)
                await handle.click(timeout=self.timeout_ms, no_wait_after=bool(
                    element and element.role == "link" and element.href))
            elif op == Operation.FILL:
                await handle.fill(action.bound_value, timeout=self.timeout_ms)
            elif op == Operation.SELECT:
                await handle.select_option(action.bound_value, timeout=self.timeout_ms)
            elif op == Operation.SCROLL:
                await self.page.mouse.wheel(0, 650 if action.bound_value == "down" else -650)
                await self.page.wait_for_timeout(100)
            elif op == Operation.WAIT:
                await self.page.wait_for_timeout(200)
            elif op == Operation.BACK:
                await self.page.go_back(wait_until="domcontentloaded")
            elif op == Operation.SWITCH_TAB:
                target = self.pages.get(action.bound_value)
                if target is None or target.is_closed() or not allowed_url(target.url, self.task):
                    return receipt("rejected", "unknown, closed or unauthorized tab")
                self.page = target
                await target.bring_to_front()
            elif op == Operation.OPEN_URL:
                if not allowed_url(action.bound_value or "", self.task):
                    return receipt("rejected", "unauthorized URL")
                await self.page.goto(action.bound_value, wait_until="domcontentloaded")
            elif op != Operation.EXTRACT:
                return receipt("rejected", "controller-only operation")
            return receipt("ok")
        except TimeoutError:
            return receipt(
                "unknown" if action.effect != "read" else "timeout", "Playwright action timed out"
            )
        except Error as exc:
            return receipt("unknown" if action.effect != "read" else "error", str(exc)[:300])

    async def __aexit__(self, *_):
        artifact_errors = []

        async def capture(name, operation):
            try:
                await asyncio.wait_for(operation(), timeout=5)
            except Exception as exc:
                artifact_errors.append({"artifact": name, "error": type(exc).__name__})

        try:
            if self.preview:
                await capture("preview", self.preview.close)
            if self.output:
                if not self.page.is_closed():
                    await capture("final.png", lambda: self.page.screenshot(
                        path=str(self.output / "final.png"), timeout=2000))
                await capture("trace.zip", lambda: self.context.tracing.stop(
                    path=str(self.output / "trace.zip")))
                if artifact_errors:
                    try:
                        (self.output / "artifact-errors.json").write_text(json.dumps(artifact_errors))
                    except OSError:
                        pass
        finally:
            try:
                await capture("browser.close", self.browser.close)
            finally:
                await capture("playwright.stop", self.pw.stop)
