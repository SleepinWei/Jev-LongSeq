import argparse
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jev_browser.browser import PlaywrightBackend
from jev_browser.cli import common, run_trial
from jev_browser.inspector import Store
from jev_browser.protocol import RunResult
from jev_browser.start_page import prompt_url, resolve_start_page, validate_url


@pytest.mark.parametrize("goal,url", [
    ('打开X.com依次搜索"Spark Labs"', "https://x.com/"),
    ('阅读 https://example.com/docs?q=one。', "https://example.com/docs?q=one"),
    ('先打开 example.com，再访问 x.com', "https://example.com/"),
    ('研究太阳能的发展', None),
    ('发送给 somebody@example.com', None),
])
def test_prompt_addresses(goal, url):
    assert prompt_url(goal) == url


@pytest.mark.parametrize("url", ['file:///tmp/file', 'javascript:alert(1)',
    'https://name:secret@example.com', 'https://example.com:invalid',
    'https://example.com\\@evil.com', 'https://exam\nple.com'])
def test_invalid_start_addresses(url):
    with pytest.raises(ValueError):
        validate_url(url)


class Brain:
    model = "test-model"

    def __init__(self, answer):
        self.answer, self.calls, self.ledger = answer, [], []

    async def post(self, payload, kind):
        self.calls.append((payload, kind))
        return {"choices": [{"message": {"content": json.dumps(self.answer)}}]}

    async def aclose(self):
        pass


async def test_explicit_start_overrides_prompt_without_model_call():
    brain = Brain({})
    result = await resolve_start_page('打开 x.com', 'https://example.com', brain)
    assert result['url'] == 'https://example.com/'
    assert result['source'] == 'user'
    assert not brain.calls
    result = await resolve_start_page('打开 x.com', '', brain)
    assert result['source'] == 'prompt'
    assert not brain.calls


async def test_model_selects_start_and_can_request_clarification():
    brain = Brain({'url': 'https://www.wikipedia.org/', 'reason': '使用维基百科首页'})
    result = await resolve_start_page('在维基百科查找太阳能', '', brain)
    assert result['source'] == 'model'
    assert brain.calls[0][1] == 'start_page'
    brain.answer = {'url': '', 'reason': '请说明当前页面属于哪个网站'}
    with pytest.raises(ValueError, match='无法判断起始网页'):
        await resolve_start_page('阅读当前页面', '', brain)
    brain.answer = {'url': 'file:///tmp/private', 'reason': 'invalid'}
    with pytest.raises(ValueError):
        await resolve_start_page('read', '', brain)


def test_inspector_allows_omitted_start_url(tmp_path, monkeypatch):
    monkeypatch.setenv('TYPESAFE_API_KEY', 'test-only')
    spawn = Mock()
    monkeypatch.setattr('jev_browser.inspector.subprocess.Popen', spawn)
    Store(tmp_path).launch_prompt({'scenario': 'web', 'brain': 'codex', 'prompt': '打开 x.com'})
    command = spawn.call_args.args[0]
    assert command[command.index('browse') + 1] == '--goal'


async def test_inferred_start_is_authorized_and_recorded_before_browsing(tmp_path, monkeypatch):
    parser = argparse.ArgumentParser()
    common(parser, demo=False)
    args = parser.parse_args([])
    args.command, args.url, args.goal, args.allow_origin = 'browse', '', '阅读示例网站', []
    args.output = str(tmp_path)
    brain = Brain({'url': 'https://example.com/', 'reason': '示例网站'})
    monkeypatch.setattr('jev_browser.cli.adapters',
                        lambda args: (None, SimpleNamespace(transport=brain), [brain]))

    class LocalPage(PlaywrightBackend):
        async def _route(self, route):
            await route.fulfill(status=200, content_type='text/html', body='<h1>Example</h1>')

    class Controller:
        final_answer = 'Example'

        def __init__(self, task, browser, policy, **kwargs):
            assert task.start_url == 'https://example.com/'
            assert task.allowed_origins == ['https://example.com']
            self.task = task

        async def run(self):
            return RunResult(task_id=self.task.id, status='success', reason='test',
                             actions=0, cycles=0, planner_calls=0, elapsed_s=0)

    monkeypatch.setattr('jev_browser.cli.PlaywrightBackend', LocalPage)
    monkeypatch.setattr('jev_browser.cli.DynamicController', Controller)
    report = await run_trial(args)
    assert report['result']['status'] == 'success'
    assert report['manifest']['start_selection']['source'] == 'model'
    assert json.loads((tmp_path / 'task.json').read_text())['start_url'] == 'https://example.com/'
