"""Capture actual model inputs/responses without changing transport behavior."""
import copy
import json
import stat

import httpx
import pytest

from jev_browser.diagnostics import Diagnostics
from jev_browser.models import ModelTransport
from jev_browser.observability import Observer


async def test_projected_request_and_failed_response_are_captured_per_attempt(tmp_path, monkeypatch):
    monkeypatch.setenv('POLICY_CONTEXT_MAX_BYTES', '48000')
    original = {'model': 'ds', 'messages': [{'role': 'user', 'content': json.dumps({
        'trusted_goal': 'original goal', 'untrusted_memory': {'recent': 'ready'},
        'untrusted_observation': {'elements': [], 'text': 'current page'},
        'api_key': 'private-wire-key'})}]}
    payload = copy.deepcopy(original)
    wires = []

    def respond(request):
        wires.append(json.loads(request.content))
        if len(wires) == 1:
            return httpx.Response(503, text='Bearer private-wire-key')
        return httpx.Response(200, json={'choices': [{'finish_reason': 'length',
                            'message': {'content': '', 'reasoning_content': 'private-wire-key'}}],
                            'usage': {'prompt_tokens': 21, 'completion_tokens': 4096}})

    observer = Observer(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = ModelTransport('https://model.test', 'private-wire-key', 'ds', client=client)
        transport.observer = observer
        await transport.post(payload, 'llm_policy')
    assert payload == original
    assert len(transport.ledger) == 2
    identifiers = set()
    for index, row in enumerate(transport.ledger):
        identifiers.add(row['attempt_id'])
        captured = json.loads((tmp_path / row['request_artifact']['path']).read_text())['data']
        expected = copy.deepcopy(wires[index])
        expected['messages'][0]['content'] = json.dumps({**json.loads(expected['messages'][0]['content']),
                                                       'api_key': '[redacted]'}, ensure_ascii=False)
        assert captured == expected
        target = tmp_path / row['response_artifact']['path']
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert 'private-wire-key' not in target.read_text()
    assert len(identifiers) == 2
    assert transport.ledger[-1]['finish_reason'] == 'length'
    assert transport.ledger[-1]['response_content_chars'] == 0
    assert 'original goal' not in (tmp_path / 'model-calls.jsonl').read_text()
    diagnostics = Diagnostics(tmp_path.parent, tmp_path.name)
    field = diagnostics.query(view='artifact', file=transport.ledger[-1]['response_artifact']['path'],
                              pointer='/data/body/choices/0/message/content')['data']
    assert field['text'] == ''


async def test_invalid_json_retained_and_capture_failure_does_not_break_response(tmp_path, monkeypatch):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text='{"partial":'))) as client:
        transport = ModelTransport('https://model.test', 'credential', 'ds', client=client)
        transport.observer = Observer(tmp_path)
        with pytest.raises(ValueError):
            await transport.post({'state': {'goal': 'ready'}}, 'jev')
        artifact = transport.ledger[0]['response_artifact']
        assert json.loads((tmp_path / artifact['path']).read_text())['data']['body'] == '{"partial":'
    from jev_browser import model_artifacts

    def fail(*args, **kwargs):
        raise OSError('disk full with potentially private text')

    monkeypatch.setattr(model_artifacts, 'capture', fail)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={'ok': True}))) as client:
        transport = ModelTransport('https://model.test', 'credential', 'ds', client=client)
        transport.observer = Observer(tmp_path)
        assert await transport.post({}, 'jev') == {'ok': True}
        row = transport.ledger[0]
        assert row['request_artifact']['error'] == 'OSError'
        assert 'disk full' not in json.dumps(row)


async def test_network_failure_and_disabled_capture_are_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv('JEV_CAPTURE_MODEL_ARTIFACTS', '0')

    def fail(request):
        raise httpx.ConnectError('offline', request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        transport = ModelTransport('https://model.test', 'credential', 'ds', client=client, retries=0)
        transport.observer = Observer(tmp_path)
        with pytest.raises(httpx.ConnectError):
            await transport.post({}, 'jev')
    row = transport.ledger[0]
    assert row['request_artifact']['reason'] == 'capture_disabled'
    assert row['response_artifact']['reason'] == 'no_response_received'
    assert not (tmp_path / 'model-artifacts').exists()


def test_redaction_of_encoded_keys_and_configured_credentials(tmp_path, monkeypatch):
    from jev_browser.model_artifacts import capture

    monkeypatch.setenv('DS_API_KEY', 'configured-secret')
    ref = capture(tmp_path, 'a' * 32, 'request', {'password': 'never-store', 'content': json.dumps({
        'nested': {'Authorization': 'Bearer arbitrary-secret'}, 'goal': 'configured-secret'})})
    stored = (tmp_path / ref['path']).read_text()
    assert not any(secret in stored for secret in ('never-store', 'arbitrary-secret', 'configured-secret'))
    outside = tmp_path.parent / (tmp_path.name + '-outside')
    outside.mkdir()
    (tmp_path / 'model-artifacts').rename(tmp_path / 'old-artifacts')
    (tmp_path / 'model-artifacts').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        capture(tmp_path, 'b' * 32, 'request', {})
