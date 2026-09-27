import json

import httpx
import pytest

from jev_browser.models import ModelTransport
from jev_browser.observability import Observer, analyze, payload_sizes, read_jsonl, save_analysis


async def test_attempts_are_durable_correlated_and_do_not_copy_prompts(tmp_path):
    attempts = 0

    def respond(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("temporary network failure", request=request)
        return httpx.Response(200, json={"usage": {"input_tokens": 25, "output_tokens": 4}})

    observer = Observer(tmp_path)
    observer.context.update(cycle=7, observation_id="obs-7")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = ModelTransport("https://model.test", "private-api-key", "test", client=client)
        transport.observer = observer
        await observer.measure(
            "policy.choose", transport.post, {"state": {"goal": "private goal text"}}, "jev"
        )
    calls = read_jsonl(tmp_path / "model-calls.jsonl")
    spans = read_jsonl(tmp_path / "spans.jsonl")
    assert len(calls) == 2 and len(spans) == 1
    assert calls[0]["call_id"] == calls[1]["call_id"]
    assert calls[0]["attempt_id"] != calls[1]["attempt_id"]
    assert all(c["cycle"] == 7 and c["span_id"] == spans[0]["span_id"] for c in calls)
    assert calls[0]["input_tokens"] is None
    assert calls[1]["input_tokens"] == 25
    stored = (tmp_path / "model-calls.jsonl").read_text()
    assert "private-api-key" not in stored and "private goal text" not in stored
    assert observer.context == {"cycle": 7, "observation_id": "obs-7"}


async def test_failed_nested_spans_keep_parent_and_restore_context(tmp_path):
    observer = Observer(tmp_path)
    with pytest.raises(RuntimeError):
        with observer.span("outer") as outer:
            with observer.span("inner") as inner:
                assert inner["parent_id"] == outer["span_id"]
                raise RuntimeError("failure")
    assert not observer.context
    assert all(s["status"] == "error" for s in read_jsonl(tmp_path / "spans.jsonl"))


def test_diagnostics_do_not_confuse_environment_success_with_agent_success(tmp_path):
    report = {
        "result": {"status": "failed", "strict_success": False, "actions": 10},
        "grade": {"strict_success": True},
        "end_to_end_s": 20,
        "model_calls": [
            {
                "kind": "dynamic_feedback",
                "status": 200,
                "latency_s": 12,
                "input_tokens": 100,
                "output_tokens": 20,
            },
            {"kind": "jev", "error": "ConnectError", "latency_s": 2},
        ],
    }
    analysis = analyze(report, [{"kind": "invalid_feedback"}])
    assert analysis["quality"]["strict_success"] is False
    assert {f["code"] for f in analysis["findings"]} == {
        "transport_instability",
        "incomplete_handoff",
        "schema_repair",
        "brain_latency",
    }
    assert analysis["efficiency"]["total"]["unknown_usage_attempts"] == 1
    (tmp_path / "trajectory.jsonl").write_text('{"kind":"invalid_feedback"}\n{"partial":')
    assert len(read_jsonl(tmp_path / "trajectory.jsonl")) == 1
    save_analysis(tmp_path, report)
    assert (tmp_path / "observability.md").exists()


def test_payload_sizes_are_utf8_bytes_not_estimated_tokens():
    value = {
        "messages": [{"role": "user", "content": json.dumps({"目标": "中文"}, ensure_ascii=False)}]
    }
    sizes = payload_sizes(value)
    assert sizes["total_bytes"] == len(json.dumps(value, ensure_ascii=False).encode())
    assert sizes["state_sections_bytes"]["目标"] == len('"中文"'.encode())
    assert "tokens" not in sizes


def test_interrupted_codex_invocation_is_not_labeled_http(tmp_path):
    observer = Observer(tmp_path)
    observer.request_started({"attempt_id": "interrupted", "transport": "codex_cli"})
    result = save_analysis(tmp_path)
    assert result["inflight_attempts"][0]["transport"] == "codex_cli"
    assert "Codex CLI" in (tmp_path / "observability.md").read_text()
