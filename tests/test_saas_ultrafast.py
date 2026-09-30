import json
from types import SimpleNamespace

import pytest

from jev_browser import saas_ultrafast as original


def test_http_attempts_record_retries_errors_and_unknown_cost_without_secrets(tmp_path):
    responses = [SimpleNamespace(status_code=503, json=lambda: {}),
                 SimpleNamespace(status_code=200, json=lambda: {"usage": {"prompt_tokens": 20, "completion_tokens": 3}}),
                 RuntimeError("secret provider response")]

    class Client:
        def post(self, url, **kwargs):
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

    client = original.MeteredClient(Client(), tmp_path)
    for _ in range(2):
        client.post("https://example.test", json={"model": "jev", "questions": {}},
                    headers={"Authorization": "secret key"})
    with pytest.raises(RuntimeError):
        client.post("https://example.test", json={"model": "helper"})
    calls = original.ledger(tmp_path)
    assert len(calls) == 3
    assert [c["status"] for c in calls] == [503, 200, 0]
    assert calls[1]["input_tokens"] == 20 and calls[1]["output_tokens"] == 3
    assert all(c["cost_usd"] is None for c in calls)
    assert "secret" not in (tmp_path / "model-calls.jsonl").read_text()
    assert calls[2]["error"] == "RuntimeError"


def test_killed_request_still_counts(tmp_path):
    (tmp_path / "model-request-starts.jsonl").write_text(json.dumps({"id": "1", "kind": "jev", "input_tokens": None}) + "\n")
    assert original.ledger(tmp_path)[0]["error"] == "InterruptedRequest"


async def test_child_termination_waits_and_escalates_to_kill():
    import asyncio

    events = []

    class Child:
        returncode = None

        def terminate(self):
            events.append("terminate")

        def kill(self):
            events.append("kill")

        async def wait(self):
            events.append("wait")
            if "kill" not in events:
                raise asyncio.TimeoutError

    await original.stop_child(Child())
    assert events == ["terminate", "wait", "kill", "wait"]
