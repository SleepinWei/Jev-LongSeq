import copy
import json

import pytest
from test_autoresearch import args_for, report, select

from jev_browser.public_web import (
    TASKS,
    exact_grade,
    expected_answer,
    parse_listing,
    run_public_web,
)


def test_exact_decimal_grading_rejects_ambiguous_or_partial_answers():
    expected = {"count": 2, "total_gbp": "0.30"}
    assert exact_grade('{"count":2,"total_gbp":"0.30"}', expected)
    for answer in ['{"count":true,"total_gbp":"0.30"}',
                   '{"count":2,"total_gbp":"0.30","count":3}',
                   '{"count":2,"total_gbp":"0.31"}', '{}', 'NaN']:
        assert not exact_grade(answer, expected)
    grouped = {"Travel": expected, "Mystery": expected}
    assert not exact_grade(json.dumps({"Travel": expected}), grouped)
    data = {"Fiction": [{"pence": 10}, {"pence": 20}, {"pence": 4001}]}
    assert expected_answer(TASKS[0], data) == expected


def test_listing_requires_valid_prices_counts_and_safe_links():
    html = '''<form class="form-horizontal"><strong>2</strong></form>
    <article class="product_pod"><h3><a href="/catalogue/a/index.html" title="A">A</a></h3>
    <p class="price_color">£1.23</p></article><li class="next"><a href="page-2.html">next</a></li>'''
    rows, count, nxt = parse_listing(html, 'https://books.toscrape.com/category/index.html')
    assert rows[0]['pence'] == 123 and count == 2 and nxt.endswith('/category/page-2.html')
    with pytest.raises(ValueError):
        parse_listing(html.replace('page-2.html', 'https://evil.test/'), 'https://books.toscrape.com/')
    with pytest.raises(ValueError):
        parse_listing(html.replace('£1.23', '1.23'), 'https://books.toscrape.com/')


async def test_changed_live_data_is_ungradable_and_oracle_is_not_agent_input(tmp_path, monkeypatch):
    samples = iter([
        {"data": {"Fiction": [{"pence": 100}]}, "fingerprint": "a", "elapsed_s": 1},
        {"data": {"Fiction": [{"pence": 200}]}, "fingerprint": "b", "elapsed_s": 1},
    ])
    async def snap(categories):
        return next(samples)
    async def execute(args, output):
        output.mkdir()
        assert args._benchmark_task.rules == []
        assert '100' not in args._benchmark_task.objective
        assert not hasattr(args, '_oracle')
        value = report()
        value['final_answer'] = '{"count":1,"total_gbp":"1.00"}'
        return value
    monkeypatch.setattr('jev_browser.public_web.snapshot', snap)
    monkeypatch.setattr('jev_browser.cli.run_trial', execute)
    args = args_for(tmp_path)
    args.seed = 0
    value = await run_public_web(args, TASKS[0], tmp_path / 'task')
    assert value['grade']['answer_correct']
    assert value['grade']['data_valid'] is False
    assert value['result']['strict_success'] is False
    assert select(report(), value)['decision'] == 'inconclusive'


def test_changed_baseline_cannot_be_promoted_as_efficiency_gain():
    baseline = report()
    baseline['grade']['data_valid'] = False
    candidate = copy.deepcopy(report(tokens=50))
    assert select(baseline, candidate)['decision'] == 'inconclusive'


async def test_artifact_failure_does_not_mask_agent_result_or_skip_cleanup(tmp_path):
    from unittest.mock import AsyncMock, Mock

    from playwright.async_api import TimeoutError

    from jev_browser.browser import PlaywrightBackend
    from jev_browser.protocol import Task

    backend = PlaywrightBackend(Task(id='test', objective='read only', control_mode='dynamic'), output=tmp_path)
    backend.page = Mock()
    backend.page.is_closed.return_value = False
    backend.page.screenshot = AsyncMock(side_effect=TimeoutError('render busy'))
    backend.context = Mock()
    backend.context.tracing.stop = AsyncMock()
    backend.browser = Mock(close=AsyncMock())
    backend.pw = Mock(stop=AsyncMock())
    await backend.__aexit__(None, None, None)
    backend.context.tracing.stop.assert_awaited_once()
    backend.browser.close.assert_awaited_once()
    backend.pw.stop.assert_awaited_once()
    assert json.loads((tmp_path / 'artifact-errors.json').read_text())[0]['artifact'] == 'final.png'


async def test_unavailable_oracle_returns_durable_zero_model_call_failure(tmp_path, monkeypatch):
    import httpx
    async def unavailable(categories):
        raise httpx.ConnectError('offline')
    async def forbidden(*args, **kwargs):
        raise AssertionError('must not dispatch an agent without an oracle')
    monkeypatch.setattr('jev_browser.public_web.snapshot', unavailable)
    monkeypatch.setattr('jev_browser.cli.run_trial', forbidden)
    args = args_for(tmp_path)
    result = await run_public_web(args, TASKS[0], tmp_path / 'unavailable')
    assert result['model_calls'] == [] and result['grade']['data_valid'] is False
    assert (tmp_path / 'unavailable/report.json').exists()
    assert result['result']['strict_success'] is False
