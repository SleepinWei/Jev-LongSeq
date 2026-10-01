from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

from playwright.async_api import Error, TimeoutError, async_playwright

from .candidates import allowed_url
from .context_budget import error_view
from .protocol import Action, Element, Observation, Operation, Receipt, Task, digest

NATIVE_SELECTOR = ('a[href],button,input,textarea,select,summary,[contenteditable="true"],'
            '[role="button"],[role="link"],[role="textbox"],[role="combobox"],'
            '[role="checkbox"],[role="radio"],[role="tab"],[role="option"],[role="menuitem"]')
SELECTOR = NATIVE_SELECTOR + ',div,span,img,svg,kbd,[onclick],[tabindex],[aria-readonly="true"]'
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
  const modalCandidates = [...document.querySelectorAll('dialog[open],[role="dialog"]')].filter(visible);
  const zOrder = el => {
    let z = 0;
    for (let n = el; n; n = n.parentElement) {
      const value = Number.parseInt(getComputedStyle(n).zIndex, 10);
      if (Number.isFinite(value)) z = Math.max(z, value);
    }
    return z;
  };
  // Only the front dialog is interactive. A help dialog can cover a still
  // rendered command palette; exposing both mixes their controls and evidence.
  const front = modalCandidates.filter(m => !modalCandidates.some(n => n !== m && m.contains(n)))
    .map((m, order) => ({m, order, z:zOrder(m)}))
    .sort((a,b) => b.z-a.z || b.order-a.order)[0];
  const modal = front ? [front.m] : [];
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
    for (const word of ['help','question','info','clear','close','search','submit','menu','next','previous','back','send','add','remove','delete']) {
      if (new RegExp('(?:^|[^a-z])' + word + '(?=$|[^a-z])').test(tokens))
        return (word === 'question' ? 'help' : word) + ' (icon control)';
    }
    return '';
  };
  const controls = [...document.querySelectorAll(selector)].map((el, index) => {
    if (!visible(el) || (modal.length && !modal.some(m => m.contains(el)))) return null;
    const display = el.matches('.control-value.like-disabled-input,[aria-readonly="true"]:not(input):not(textarea):not(select)');
    if (!el.matches(nativeSelector) && !display) {
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
    // Some form libraries render a sibling label without `for`. Associate only
    // a visible label in a scope containing this single visible field.
    let implicitLabel = '';
    if (el.matches('input,textarea,select,[contenteditable="true"]') && !labels) {
      for (let scope = el.parentElement, depth = 0; scope && depth < 5; scope = scope.parentElement, depth++) {
        const fields = [...scope.querySelectorAll('input,textarea,select,[contenteditable="true"]')].filter(visible);
        if (fields.length > 1) break;
        const scopeLabels = [...scope.querySelectorAll('label')].filter(visible);
        if (fields.length === 1 && scopeLabels.length === 1) {
          implicitLabel = labelText(scopeLabels[0]);
          break;
        }
      }
    }
    if (display) {
      const scope = el.closest('.frappe-control');
      const label = scope && [...scope.querySelectorAll('label')].find(visible);
      if (label) implicitLabel = labelText(label);
    }
    const labelled = (el.getAttribute('aria-labelledby') || '').split(/\s+/)
      .map(id => document.getElementById(id)).filter(x => x && visible(x)).map(labelText).join(' ');
    const name = labelled || el.getAttribute('aria-label') || labels || implicitLabel || labelText(el) ||
      el.getAttribute('alt') || iconName(el) || el.getAttribute('title') ||
      el.getAttribute('placeholder') || iconHint(el) || '';
    const inputRoles = {checkbox:'checkbox',radio:'radio',button:'button',submit:'button',reset:'button',range:'slider',file:'button'};
    const role = display ? 'status' : el.getAttribute('role') || (tag === 'input' ? (inputRoles[el.type] || 'textbox') :
      ({a:'link',button:'button',summary:'button',textarea:'textbox',select:'combobox'}[tag] ||
       (custom.has(el) ? 'button' : 'textbox')));
    const readOnly = display || !!el.readOnly || el.getAttribute('aria-readonly') === 'true';
    const editable = !readOnly && (tag === 'textarea' || el.isContentEditable ||
      (tag === 'input' && ['text','search','email','url','tel','password','number'].includes(el.type)));
    const row = el.closest('.grid-row,tr,[role="row"],fieldset,form');
    const required = !!el.required || el.getAttribute('aria-required') === 'true' ||
      /\*\s*$/.test(labels || implicitLabel) ||
      [...(el.closest('.frappe-control') || el.parentElement).querySelectorAll('label')]
        .some(label => visible(label) && getComputedStyle(label, '::after').content.replace(/["']/g, '') === '*');
    return {index, role, name:name.trim().replace(/\s*\*$/, ''), value:el.type === 'password' ? (el.value ? '[redacted]' : '') : (display ? labelText(el) : el.value || ''),
      enabled:!display && !el.matches(':disabled') && !el.closest('[aria-disabled="true"]'),
      editable, read_only:readOnly, required, selectable:tag === 'select' && !readOnly,
      checked: ['checkbox','radio'].includes(role) ? (el.checked ?? el.getAttribute('aria-checked') === 'true') : null,
      context:row ? labelText(row) : '', href:tag === 'a' ? el.href : null,
      options:tag === 'select' ? [...el.options].filter(x => !x.disabled && !x.hidden).map(x => x.value) : []};
  }).filter(Boolean);
  const nodes = document.querySelectorAll(selector);
  if (front) {
    const fields = controls.filter(c => c.editable || c.selectable || ['checkbox','radio'].includes(c.role));
    const searchOnly = fields.length === 1 && fields[0].editable && fields[0].enabled &&
      (fields[0].role === 'combobox' || nodes[fields[0].index].type === 'search') &&
      /search|搜索|搜尋/i.test(fields[0].name);
    const confirmation = controls.some(c => c.role === 'button' &&
      /^(yes|no|ok)$|^(confirm|cancel|submit|save|publish|approve|delete)|是|否|确认|确定|取消|提交|保存|删除/i.test(c.name.replace(/\s+/g,'')));
    // Bind a rendered keyboard hint, not an invented general keyboard tool.
    // Never offer dismissal for an editable business form or confirmation.
    if (searchOnly && !confirmation) {
      const hints = [...nodes].filter(el => visible(el) && front.m.contains(el) &&
        el.matches('kbd,span') && /^(esc|escape)$/i.test(el.innerText.trim()) &&
        /\besc(?:ape)?\s+to\s+close\b/i.test(el.parentElement.innerText.trim()));
      const leaves = hints.filter(el => !hints.some(other => other !== el && el.contains(other)));
      if (leaves.length === 1) {
        const el = leaves[0], index = [...nodes].indexOf(el);
        const existing = controls.findIndex(c => c.index === index);
        if (existing >= 0) controls.splice(existing, 1);
        controls.push({index, role:'button', name:'Close dialog (Escape shortcut)', value:'',
          enabled:true, editable:false, read_only:false, required:false, selectable:false,
          checked:null, context:el.parentElement.innerText, href:null, options:[], activation_key:'Escape'});
      }
    }
  }
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
  // Visible table structure only: row/cell identity survives an inline editor
  // becoming display text. No application state, hidden data IDs or backend reads.
  const renderedText = el => {
    const texts = [], walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    while (walk.nextNode()) {
      const n = walk.currentNode;
      if (visible(n.parentElement) && !n.parentElement.closest('input,textarea,select'))
        texts.push(n.textContent.trim());
    }
    return texts.filter(Boolean).join(' ');
  };
  const gridScope = el => el.closest('table,[role="grid"],[role="table"],.grid-field') ||
    el.closest('.form-grid')?.closest('.frappe-control') || el.closest('.form-grid');
  const scopePath = el => {
    const parts = [];
    for (let n = el; n && n !== document.body; n = n.parentElement) {
      const siblings = [...n.parentElement.children].filter(x => x.tagName === n.tagName);
      parts.unshift(n.tagName.toLowerCase() + ':' + (siblings.indexOf(n) + 1));
    }
    return parts.join('/');
  };
  const gridRef = scope => {
    let a = 2166136261, b = 2246822519;
    for (const char of scopePath(scope)) {
      a = Math.imul(a ^ char.charCodeAt(0), 16777619);
      b = Math.imul(b ^ char.charCodeAt(0), 3266489917);
    }
    return 'g' + (a >>> 0).toString(16).padStart(8, '0') +
      (b >>> 0).toString(16).padStart(8, '0');
  };
  const rowSelector = '.grid-row,tr,[role="row"]';
  const rowNodes = [...document.querySelectorAll(rowSelector)].filter(row => visible(row) &&
    (!front || front.m.contains(row)) && !row.closest('thead,.grid-heading-row,.grid-header') &&
    !row.querySelector('th,[role="columnheader"]') && gridScope(row));
  const containers = [...document.querySelectorAll('table,[role="grid"],[role="table"],.grid-field,.form-grid')]
    .filter(el => visible(el) && (!front || front.m.contains(el))).map(el => gridScope(el) || el);
  const scopes = [...new Set([...rowNodes.map(gridScope), ...containers])], grids = [];
  for (const scope of scopes) {
    const id = gridRef(scope), rows = rowNodes.filter(row => gridScope(row) === scope);
    const header = scope.querySelector('thead tr,.grid-heading-row,[role="row"]:has([role="columnheader"])');
    const cellSelector = '.grid-static-col,td,[role="gridcell"],[role="cell"]';
    const headers = header ? [...header.querySelectorAll('.grid-static-col,th,[role="columnheader"]')]
      .filter(visible).map(renderedText) : [];
    const label = [...scope.querySelectorAll('caption,label,[role="heading"]')].find(visible);
    const grid = {id, name:label ? renderedText(label) : '', rows:[]};
    for (const [position, row] of rows.entries()) {
      const index = [...row.querySelectorAll('.row-index,.grid-row-index,[role="rowheader"]')].find(visible);
      const key = index ? renderedText(index) : String(position + 1);
      if (!key) continue;
      const cells = [...row.querySelectorAll(cellSelector)].filter(cell => visible(cell) &&
        cell.closest(rowSelector) === row && !cell.parentElement.closest(cellSelector));
      const values = cells.map((cell, column) => {
        const field = controls.find(c => (c.editable || c.selectable) && cell.contains(nodes[c.index]));
        const labels = [...cell.querySelectorAll('label')].filter(visible).map(renderedText);
        return {column:headers[column] || labels.join(' ') || 'column-' + (column + 1),
          value:field ? field.value : renderedText(cell)};
      });
      const refs = controls.filter(c => row.contains(nodes[c.index]));
      for (const c of refs) { c.grid_ref = id; c.row_ref = key; }
      grid.rows.push({key, cells:values, control_refs:refs.map(c => 'e' + c.index)});
    }
    for (const c of controls) {
      if (!c.grid_ref && scope.contains(nodes[c.index])) c.grid_ref = id;
    }
    grids.push(grid);
  }
  return {url:location.href,title:document.title,text:lines.join('\n'),controls,
    grids,
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
        self.recovered_error_count = 0
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
            grids=raw.get("grids", []),
            dialogs=raw["dialogs"],
            loading=raw["loading"],
            http_status=self.navigation_status.get(self.page, (None, None))[1],
            errors=error_view([e for i, e in enumerate(self.errors)
                               if i >= self.recovered_error_count or not e.startswith("page_error:")]),
            challenge=any(s in text for s in ("verify you are human", "captcha", "access denied")),
            tabs={k: p.url for k, p in self.pages.items() if not p.is_closed()},
        )
        if raw["visible_frames"]:
            obs.errors.append("unsupported_iframe")
        self.last = obs
        return obs

    def acknowledge_runtime_recovery(self, obs):
        """Retire only the already reconciled exceptions; the raw error archive stays intact."""
        if not self.last or self.last.observation_id != obs.observation_id:
            raise ValueError("runtime recovery must refer to the current observation")
        self.recovered_error_count = len(self.errors)

    async def execute(self, action: Action) -> Receipt:
        started = time.monotonic()
        try:
            return await self._execute_once(action)
        except Error as exc:
            if not any(message in str(exc) for message in (
                "Execution context was destroyed", "Cannot find context")):
                raise
            # Only preflight errors escape _execute_once: its dispatch block
            # already returns unknown for writes. No action has been dispatched
            # here, so obtain a fresh observation rather than abort or replay.
            return Receipt(action_id=action.id, status="stale",
                           detail="navigation changed the document during action preflight",
                           duration_s=time.monotonic() - started, write_key=action.write_key)

    async def blur_input(self, obs, element_ref):
        """One grounded native Tab for bounded draft recovery; never a hidden state write."""
        if not self.last or obs.observation_id != self.last.observation_id:
            return Receipt(action_id="resume-blur", status="stale")
        element = next((e for e in obs.elements if e.id == element_ref), None)
        if not element or not element.editable or not element.enabled:
            return Receipt(action_id="resume-blur", status="rejected")
        current = await self.page.evaluate(SNAPSHOT, SELECTOR)
        if digest(current) != obs.document_version:
            return Receipt(action_id="resume-blur", status="stale")
        handle = (await self.snapshot_handle.get_property(element_ref)).as_element()
        if not handle or not await handle.evaluate(
                "el => el.isConnected && document.activeElement === el"):
            return Receipt(action_id="resume-blur", status="rejected", detail="input is not focused")
        try:
            await handle.press("Tab", timeout=self.timeout_ms)
            return Receipt(action_id="resume-blur", status="ok")
        except Error as exc:
            return Receipt(action_id="resume-blur", status="unknown", detail=str(exc)[:300])

    async def _execute_once(self, action: Action) -> Receipt:
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
                if element and element.activation_key == "Escape":
                    # Bootstrap help can leave focus outside the palette. Its
                    # advertised shortcut belongs to the sole observed search
                    # field; native focus is part of activating that capability.
                    fields = [e for e in self.last.elements if e.editable and e.enabled]
                    if len(fields) != 1:
                        return receipt("rejected", "Escape shortcut lost its unique search field")
                    field = (await self.snapshot_handle.get_property(fields[0].id)).as_element()
                    if field is None:
                        return receipt("stale", "dialog search field was replaced")
                    await field.focus()
                    await self.page.keyboard.press("Escape")
                    return receipt("ok", "activated observed Escape dialog shortcut")
                await handle.click(timeout=self.timeout_ms, no_wait_after=bool(
                    element and element.role == "link" and element.href))
            elif op == Operation.FILL:
                await handle.fill(action.bound_value, timeout=self.timeout_ms)
                # Native text/date widgets often commit on change/blur. Leaving
                # focus inside them can let a datepicker restore the old value.
                # Link/autocomplete fields must retain focus for option selection.
                element = next((e for e in self.last.elements if e.id == action.element_ref), None)
                if (self.task.control_mode == "dynamic" and element.role != "combobox"
                        and await handle.evaluate(
                            "el => el instanceof HTMLInputElement && el.type !== 'search' "
                            "&& document.activeElement === el")):
                    await handle.press("Tab", timeout=self.timeout_ms)
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
