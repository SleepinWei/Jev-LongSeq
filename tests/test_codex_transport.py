import argparse
import json
import sys

import pytest

from jev_browser.cli import adapters, common
from jev_browser.codex_transport import (
    CodexInvocationError,
    CodexTransport,
    failure_category,
    strict_schema,
)
from jev_browser.evaluation import efficiency_profile
from jev_browser.observability import Observer


def fake_codex(tmp_path, *, fail=False, delay=0):
    executable = tmp_path / "codex"
    executable.write_text(
        f"#!{sys.executable}\n"
        + f"""
import json, os, sys, time
args=sys.argv[1:]
assert '--ignore-user-config' in args and '--ephemeral' in args
assert 'forced_login_method="chatgpt"' in args
assert 'model_reasoning_effort="high"' in args
assert any('requires_openai_auth=true' in v and 'supports_websockets=false' in v for v in args)
assert args[args.index('--model')+1] == 'gpt-6-astra'
assert args[args.index('--sandbox')+1] == 'read-only'
assert not any(k.endswith('_API_KEY') for k in os.environ)
sys.stdin.read()
time.sleep({delay})
if {fail!r}: sys.exit(2)
with open(args[args.index('--output-schema')+1]) as f: schema=json.load(f)
assert schema['required'] == list(schema['properties'])
with open(args[args.index('--output-last-message')+1], 'w') as f: json.dump({{'value':'Ada'}}, f)
print(json.dumps({{'type':'turn.completed','usage':{{'input_tokens':30,'cached_input_tokens':15,'output_tokens':10}}}}))
"""
    )
    executable.chmod(0o755)
    return str(executable)


async def test_codex_cli_uses_chatgpt_high_and_accounts_subscription_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-child")
    monkeypatch.setenv("TYPESAFE_API_KEY", "also-must-not-reach-child")
    transport = CodexTransport(binary=fake_codex(tmp_path))
    transport.observer = Observer(tmp_path / "telemetry")
    result = await transport.post(
        {"messages": [{"role": "user", "content": "{}"}]}, "dynamic_input"
    )
    assert json.loads(result["choices"][0]["message"]["content"])["value"] == "Ada"
    record = transport.ledger[0]
    assert record["transport"] == "codex_cli" and record["input_tokens"] == 30
    assert record["cost_usd"] is None and record["cached_input_tokens"] == 15
    request = json.loads((tmp_path / "telemetry" / record["request_artifact"]["path"]).read_text())
    response = json.loads((tmp_path / "telemetry" / record["response_artifact"]["path"]).read_text())
    assert "USER:" in request["data"]["stdin_prompt"]
    assert request["data"]["output_schema"]["required"] == ["value"]
    assert json.loads(response["data"]["answer"])["value"] == "Ada"
    assert "turn.completed" in response["data"]["stdout"]
    profile = efficiency_profile(transport.ledger, actions=1, elapsed_s=1)
    assert profile["total"]["http_attempts"] == 0
    assert profile["total"]["codex_invocations"] == 1
    assert profile["total"]["successful_responses"] == 1


async def test_codex_failure_does_not_retry_or_fall_back_to_api(tmp_path):
    transport = CodexTransport(binary=fake_codex(tmp_path, fail=True))
    with pytest.raises(CodexInvocationError, match="no API fallback"):
        await transport.post({"messages": [{"role": "user", "content": "{}"}]}, "dynamic_feedback")
    assert len(transport.ledger) == 1 and transport.ledger[0]["input_tokens"] is None


async def test_codex_timeout_stops_child_and_keeps_unknown_usage(tmp_path):
    transport = CodexTransport(binary=fake_codex(tmp_path, delay=10), timeout_s=0.1)
    with pytest.raises(TimeoutError):
        await transport.post({"messages": [{"role": "user", "content": "{}"}]}, "dynamic_feedback")
    assert transport.ledger[0]["error"] == "TimeoutError"
    assert transport.ledger[0]["cost_usd"] is None


def test_strict_schema_preserves_constraints_and_requires_nested_fields():
    schema = strict_schema(
        {
            "type": "object",
            "properties": {
                "x": {"type": "string", "default": "", "maxLength": 40},
                "child": {"type": "object", "properties": {"ready": {"type": "boolean"}}},
            },
        }
    )
    assert schema["required"] == ["x", "child"]
    assert schema["properties"]["child"]["required"] == ["ready"]
    assert "default" not in schema["properties"]["x"]


async def test_explicit_codex_brain_needs_no_planner_api_configuration(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only-not-dispatched")
    for key in ("PLANNER_API_KEY", "PLANNER_MODEL", "PLANNER_ENDPOINT"):
        monkeypatch.delenv(key, raising=False)
    parser = argparse.ArgumentParser()
    common(parser, demo=True)
    args = parser.parse_args(["--mode", "dynamic", "--policy", "jev", "--brain", "codex"])
    policy, brain, transports = adapters(args)
    try:
        assert isinstance(brain.transport, CodexTransport)
        assert brain.transport.model == "gpt-6-astra" and brain.transport.effort == "high"
        assert brain.tuning.brain_interval == 12
        assert policy.transport is not brain.transport
    finally:
        for transport in transports:
            await transport.aclose()


async def test_default_longseq_uses_deepseek_and_jev_while_researcher_uses_codex(
    monkeypatch, tmp_path
):
    from jev_browser.models import ModelTransport
    from jev_browser.researcher import CodexResearcher

    for key in ("PLANNER_API_KEY", "PLANNER_MODEL", "PLANNER_ENDPOINT"):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("TYPESAFE_API_KEY", "jev-test-no-dispatch")
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "ds-test-no-dispatch")
    monkeypatch.setenv("TEXT_MODEL", "deepseek-flash")
    monkeypatch.setenv("TEXT_MODEL_BASE_URL", "https://deepseek.test/v1")
    parser = argparse.ArgumentParser()
    common(parser, demo=True)
    args = parser.parse_args(["--mode", "dynamic", "--policy", "jev"])
    args.metric = "tokens"
    policy, brain, transports = adapters(args)
    researcher = CodexResearcher(args, Observer(tmp_path))
    try:
        assert args.brain == "api"
        assert isinstance(brain.transport, ModelTransport)
        assert brain.transport.model == "deepseek-flash"
        assert brain.transport.endpoint == "https://deepseek.test/v1/chat/completions"
        assert brain.transport.api_key == "ds-test-no-dispatch"
        assert policy.transport.api_key == "jev-test-no-dispatch"
        assert isinstance(researcher.transport, CodexTransport)
        assert researcher.transport.model == "gpt-6-astra"
        assert researcher.transport.effort == "high"
        assert not researcher.transport.api_key
    finally:
        for transport in [*transports, researcher.transport]:
            await transport.aclose()


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("stream disconnected before completion: error sending request for url", "connection"),
        ("Your usage limit has been reached", "quota"),
        ("Invalid schema for response_format", "output_schema"),
        ("401 Unauthorized", "authentication"),
    ],
)
def test_error_diagnostics_classify_without_recording_service_text(message, expected):
    assert failure_category([message]) == expected
