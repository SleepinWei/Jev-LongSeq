import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from playwright.async_api import Error

from jev_browser.browser import PlaywrightBackend
from jev_browser.dynamic import generate_dynamic
from jev_browser.protocol import Operation, Task


def task():
    return Task(id='navigation', control_mode='dynamic', objective='Read pages',
                start_url='https://nav.test/', allowed_origins=['https://nav.test'])


async def test_observation_retries_only_navigation_context_loss():
    backend = PlaywrightBackend(task())
    sentinel = object()
    backend._observe_once = AsyncMock(side_effect=[Error('Execution context was destroyed'), sentinel])
    backend.page = Mock(wait_for_load_state=AsyncMock())
    assert await backend.observe() is sentinel
    assert backend._observe_once.await_count == 2
    backend._observe_once = AsyncMock(side_effect=Error('Target closed'))
    with pytest.raises(Error, match='Target closed'):
        await backend.observe()
    assert backend._observe_once.await_count == 1


async def test_link_navigation_can_be_observed_while_images_are_loading():
    class Site(PlaywrightBackend):
        async def _route(self, route):
            path = route.request.url.removeprefix('https://nav.test')
            if path == '/slow.png':
                await asyncio.sleep(1)
                await route.fulfill(status=404, body='')
            elif path == '/next':
                await route.fulfill(content_type='text/html',
                                    body='<h1>Destination</h1><img src="/slow.png">')
            else:
                await route.fulfill(content_type='text/html', body='<a href="/next">Continue</a>')
    async with Site(task()) as browser:
        obs = await browser.observe()
        action = next(a for a in generate_dynamic(obs, task()) if a.operation == Operation.CLICK)
        receipt = await browser.execute(action)
        assert receipt.status == 'ok'
        await browser.page.wait_for_url('https://nav.test/next', wait_until='domcontentloaded')
        obs = await browser.observe()
        assert obs.url == 'https://nav.test/next' and 'Destination' in obs.text
