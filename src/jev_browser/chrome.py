"""An owned task tab in the user's existing Chrome profile, via Browser Harness discovery."""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from .browser import PlaywrightBackend
from .candidates import allowed_url
from .preview import LivePreview


class ChromeBackend(PlaywrightBackend):
    """Reuse login state without copying cookies or tracing unrelated user tabs."""

    async def __aenter__(self):
        if self.task.sandbox:
            raise ValueError("本地样例请使用 Playwright；Chrome 会话仅用于网页任务")
        try:
            from browser_harness.daemon import get_ws_url
        except ImportError as exc:
            raise ValueError("Chrome 连接组件未安装，请运行 uv sync --extra chrome") from exc
        if not allowed_url(self.task.start_url, self.task):
            raise ValueError("start URL is outside allowed origins")
        # Same DevToolsActivePort / chrome://inspect discovery used by Browser Harness.
        endpoint = await asyncio.to_thread(get_ws_url)
        if urlsplit(endpoint).hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Chrome 模式需要本机 Chrome 连接，不能使用远程浏览器")
        self.pw = await async_playwright().start()
        self.page = None
        try:
            self.browser = await self.pw.chromium.connect_over_cdp(endpoint, timeout=30000)
            self.browser_version = self.browser.version
            if not self.browser.contexts:
                raise ValueError("Chrome 没有可用的已登录浏览器配置")
            self.context = self.browser.contexts[0]
            self.page = await self.context.new_page()
            await self._own_page(self.page)
            if self.output:
                self.output.mkdir(parents=True, exist_ok=True)
            if self.live_preview and self.output:
                self.preview = LivePreview(self.output, self.capture_preview, archive=True)
                self.preview.start()
            await self.page.goto(self.task.start_url, wait_until="domcontentloaded", timeout=30000)
            return self
        except BaseException:
            if self.preview:
                await self.preview.close()
            if self.page and not self.page.is_closed():
                await self.page.close()
            await self.pw.stop()
            raise

    async def _own_page(self, page):
        self._register(page)
        page.set_default_timeout(self.timeout_ms)
        await page.set_viewport_size({"width": 1280, "height": 900})
        await page.route("**/*", self._route)
        page.on("popup", self._own_page)

    async def _route(self, route):
        request = route.request
        # Normal site assets/API requests need their CDNs. Only task-tab document
        # navigations are origin-restricted; no routes are installed on user tabs.
        navigation = request.is_navigation_request() and request.frame == request.frame.page.main_frame
        if urlsplit(request.url).scheme in {"http", "https"} and (
            not navigation or allowed_url(request.url, self.task)
        ):
            await route.continue_()
        else:
            self.errors.append(f"blocked_request:{request.url}")
            await route.abort("blockedbyclient")

    async def __aexit__(self, *_):
        if self.preview:
            await self.preview.close()
        try:
            if self.output and self.page and not self.page.is_closed():
                await self.page.screenshot(path=str(self.output / "final.png"), timeout=5000)
        finally:
            # Remove only our tab-specific listeners/routes. Keep the task page for
            # inspection and never close the user's browser or their other tabs.
            try:
                await self._release_handles()
                for page in self.pages.values():
                    if not page.is_closed():
                        await page.unroute_all(behavior="ignoreErrors")
                        page.remove_listener("popup", self._own_page)
                        page.remove_listener("response", self._response)
                        page.remove_listener("dialog", self._dialog)
            finally:
                await self.pw.stop()
