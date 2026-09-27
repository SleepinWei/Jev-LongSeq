"""BrowserGym adapter: public BrowserEnv, page and HighLevelActionSet APIs only.

The synchronous Gym/Playwright objects are confined to one dedicated worker thread.
Only visible DOM observations cross into the controller; rewards stay in the runner.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .browser import SELECTOR, SNAPSHOT
from .candidates import allowed_url
from .protocol import Action, Element, Observation, Operation, Receipt, Task, digest

# BrowserGym owns a process-global sync Playwright instance. Every episode must use
# the same thread; a new executor per episode would violate greenlet ownership.
_GYM_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="browsergym")


def action_string(action: Action) -> str:
    """Serialize bound arguments, never execute a model-produced action/program string."""
    bid, value = repr(action.element_ref), repr(action.bound_value)
    if action.operation == Operation.CLICK:
        return f"click({bid})"
    if action.operation == Operation.FILL:
        return f"fill({bid}, {value})"
    if action.operation == Operation.SELECT:
        return f"select_option({bid}, {value})"
    if action.operation == Operation.SCROLL:
        return f"scroll(0, {650 if action.bound_value == 'down' else -650})"
    if action.operation == Operation.WAIT:
        return "noop(200)"
    if action.operation == Operation.BACK:
        return "go_back()"
    if action.operation == Operation.OPEN_URL:
        return f"goto({value})"
    if action.operation == Operation.SWITCH_TAB:
        return f"tab_focus({int(action.bound_value.removeprefix('tab-'))})"
    raise ValueError("unsupported BrowserGym operation")


def catalog_task_class():
    from browsergym.core.task import AbstractBrowserTask

    from .fixture import catalog_html

    class CatalogTask(AbstractBrowserTask):
        def __init__(self, seed, records=32, popup=True, injection=True):
            super().__init__(seed)
            self.records, self.popup, self.injection = records, popup, injection
            self.viewport = {"width": 1280, "height": 900}
            self.slow_mo = 0
            self.timeout = 3000

        def setup(self, page):
            from .fixture import demo_task

            page.context.route("**/*", lambda route: route.abort())
            page.set_content(catalog_html(self.records, popup=self.popup, injection=self.injection))
            return demo_task(self.records).objective, {}

        def validate(self, page, chat_messages):
            # Defer hidden grading until the agent has stopped. Never expose progress truth.
            if not chat_messages or chat_messages[-1]["role"] != "assistant":
                return 0.0, False, "", {}
            grade = page.evaluate("window.__grade()")
            return float(grade["strict_success"]), True, "", {"grade": grade}

    return CatalogTask


class BrowserGymBackend:
    def __init__(
        self,
        task: Task,
        *,
        output: Path | None = None,
        headless=True,
        gym_id: str | None = None,
        records=32,
        seed=0,
        popup=True,
        injection=True,
        max_env_steps=400,
        live_preview=False,
    ):
        self.task, self.output, self.headless = task, output, headless
        self.gym_id, self.records, self.seed = gym_id, records, seed
        self.popup, self.injection = popup, injection
        self.max_env_steps = max_env_steps
        self.pool = _GYM_EXECUTOR
        self.env = None
        self.last = None
        self.handles = {}
        self.env_steps = self.refresh_steps = 0
        self.reward = 0.0
        self.terminated = self.truncated = False
        self.grade_info = {}
        self.browser_version = ""
        self.events = []
        self.live_preview, self.preview = live_preview, None

    async def _call(self, function, *args):
        return await asyncio.get_running_loop().run_in_executor(self.pool, function, *args)

    def _start(self):
        from browsergym.core.action.highlevel import HighLevelActionSet
        from browsergym.core.env import BrowserEnv

        action_set = HighLevelActionSet(
            subsets=["bid", "nav", "tab", "chat"],
            multiaction=False,
            strict=True,
            retry_with_force=False,
        )
        options = dict(headless=self.headless, action_mapping=action_set.to_python_code)
        if self.gym_id:
            if not self.gym_id.startswith("browsergym/webarena."):
                raise ValueError("only registered WebArena tasks are supported by this runner")
            import browsergym.webarena  # noqa: F401
            import gymnasium

            self.env = gymnasium.make(self.gym_id, **options)
        else:
            self.env = BrowserEnv(
                catalog_task_class(),
                task_kwargs={
                    "records": self.records,
                    "popup": self.popup,
                    "injection": self.injection,
                },
                **options,
            )
        raw, _ = self.env.reset(seed=self.seed)
        self.raw = raw
        public = self.env.unwrapped
        self.browser_version = public.browser.version
        if self.output:
            self.output.mkdir(parents=True, exist_ok=True)
            public.context.tracing.start(screenshots=True, snapshots=True)
        return raw["goal"]

    async def __aenter__(self):
        try:
            self.goal = await self._call(self._start)
            if self.live_preview and self.output:
                from .preview import LivePreview

                self.preview = LivePreview(self.output, self.capture_preview)
                self.preview.start()
            return self
        except BaseException:
            await self._call(self._close)
            raise

    def _step(self, code):
        if self.env_steps >= self.max_env_steps:
            raise RuntimeError("BrowserGym environment step budget exhausted")
        if self.terminated or self.truncated:
            raise RuntimeError("BrowserGym episode already terminated")
        started = time.monotonic()
        raw, reward, terminated, truncated, info = self.env.step(code)
        self.env_steps += 1
        self.raw = raw
        self.reward, self.terminated, self.truncated = (
            float(reward),
            bool(terminated),
            bool(truncated),
        )
        self.grade_info = info.get("task_info", {})
        self.events.append(
            {
                "step": self.env_steps,
                "action": code,
                "elapsed_s": time.monotonic() - started,
                "error": raw.get("last_action_error", ""),
            }
        )
        return raw

    def _observe(self):
        public = self.env.unwrapped
        page = public.page
        for handle in self.handles.values():
            handle.dispose()
        self.handles = {}
        raw = page.evaluate(SNAPSHOT, SELECTOR)
        handles = page.locator(SELECTOR).element_handles()
        # Late rerenders can create unmarked nodes. Refresh using a public, budgeted noop.
        if any(not handles[e["index"]].get_attribute("bid") for e in raw["controls"]):
            for handle in handles:
                handle.dispose()
            self._step("noop(0)")
            self.refresh_steps += 1
            page = public.page
            raw = page.evaluate(SNAPSHOT, SELECTOR)
            handles = page.locator(SELECTOR).element_handles()
        elements = []
        for control in raw["controls"]:
            handle = handles[control["index"]]
            bid = handle.get_attribute("bid")
            if not bid:
                continue
            self.handles[bid] = handle
            elements.append(Element(id=bid, **{k: v for k, v in control.items() if k != "index"}))
        for handle in handles:
            if handle not in self.handles.values():
                handle.dispose()
        pages = public.context.pages
        tab_id = f"tab-{pages.index(page)}"
        self.last = Observation(
            observation_id=uuid.uuid4().hex,
            document_version=digest(raw),
            tab_id=tab_id,
            url=raw["url"],
            title=raw["title"],
            text=raw["text"],
            elements=elements,
            dialogs=raw["dialogs"],
            loading=raw["loading"],
            errors=(["unsupported_iframe"] if len(page.frames) > 1 else [])
            + ([self.raw["last_action_error"]] if self.raw.get("last_action_error") else []),
            challenge=any(
                s in raw["text"].casefold()
                for s in ("captcha", "verify you are human", "access denied")
            ),
            tabs={f"tab-{i}": p.url for i, p in enumerate(pages)},
        )
        return self.last

    async def observe(self):
        return await self._call(self._observe)

    def _capture_preview(self):
        page = self.env.unwrapped.page
        return page.screenshot(type="jpeg", quality=65, timeout=2000), page.url

    async def capture_preview(self):
        return await self._call(self._capture_preview)

    def _execute(self, action):
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
            return receipt("stale", "action references an obsolete observation")
        page = self.env.unwrapped.page
        if digest(page.evaluate(SNAPSHOT, SELECTOR)) != action.document_version:
            return receipt("stale", "document changed before dispatch")
        if action.element_ref:
            handle = self.handles.get(action.element_ref)
            if not handle or not handle.evaluate("el => el.isConnected"):
                return receipt("stale", "element detached")
            if handle.get_attribute("bid") != action.element_ref:
                return receipt("stale", "BrowserGym id changed")
        if action.operation == Operation.EXTRACT:
            return receipt("ok")
        if action.operation == Operation.OPEN_URL and not allowed_url(
            action.bound_value, self.task
        ):
            return receipt("rejected", "unauthorized URL")
        raw = self._step(action_string(action))
        if raw.get("last_action_error"):
            return receipt(
                "unknown" if action.effect != "read" else "error", raw["last_action_error"][:300]
            )
        return receipt("ok")

    async def execute(self, action):
        return await self._call(self._execute, action)

    async def finish(self, answer=""):
        if not self.terminated and not self.truncated:
            await self._call(self._step, f"send_msg_to_user({answer!r})")
        return {
            "reward": self.reward,
            "terminated": self.terminated,
            "truncated": self.truncated,
            "info": self.grade_info,
            "env_steps": self.env_steps,
            "refresh_steps": self.refresh_steps,
        }

    def _close(self):
        if self.env:
            try:
                if self.output and self.env.unwrapped.context:
                    self.env.unwrapped.page.screenshot(path=str(self.output / "final.png"))
                    self.env.unwrapped.context.tracing.stop(path=str(self.output / "trace.zip"))
            finally:
                self.env.close()

    async def __aexit__(self, *_):
        if self.preview:
            await self.preview.close()
        await self._call(self._close)
