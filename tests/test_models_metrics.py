import json

import httpx
import pytest
from test_protocol import observation

from jev_browser.candidates import generate
from jev_browser.evaluation import (
    calibration,
    candidate_recall,
    paired_bootstrap,
    pass_four,
    summarize,
)
from jev_browser.fixture import demo_task
from jev_browser.memory import Memory
from jev_browser.models import JevPolicy, JsonPlanner, JsonPolicy, ModelTransport, Pricing


async def test_json_planner_and_policy_wire_contracts():
    def respond(request):
        body = json.loads(request.content)
        content = json.loads(body["messages"][1]["content"])
        assert body["response_format"] == {"type": "json_object"}
        if "plan_schema" in content:
            assert content["trigger"] == "no_progress"
            value = {
                "subtasks": [
                    {
                        "id": "inspect",
                        "objective": "read visible page",
                        "allowed_operations": ["extract_visible"],
                        "success_predicates": [{"kind": "text", "value": "ready"}],
                    }
                ]
            }
        else:
            assert "untrusted_observation" in content
            value = {"choice": content["candidates"][0]["id"]}
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(value)}}],
                "usage": {"prompt_tokens": 200, "completion_tokens": 30},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = ModelTransport(
            "https://example.test/v1/chat/completions", "test", "test", client=client
        )
        task, obs, memory = demo_task(1), observation(), Memory()
        plan = await JsonPlanner(transport).plan(task, obs, memory, "no_progress")
        choice = await JsonPolicy(transport).choose(
            task, obs, memory, plan.subtasks[0], generate(obs, task, memory, None)
        )
    assert plan.subtasks[0].id == "inspect"
    assert choice.choice == "a0"
    assert [record["kind"] for record in transport.ledger] == ["planner", "llm_policy"]
    assert all(record["input_tokens"] == 200 for record in transport.ledger)


async def test_jev_wire_contract_and_metering():
    def respond(request):
        body = json.loads(request.content)
        assert request.url.path == "/v1/systemone"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert body["questions"]["action"]["type"] == "choice"
        assert "criteria" in body["questions"]["action"]
        return httpx.Response(
            200,
            json={
                "model": "jev-pinned-test",
                "answers": {
                    "action": {
                        "type": "choice",
                        "choice": "a0",
                        "confidence": 0.8,
                        "probabilities": {"a0": 0.9, "a1": 0.1},
                    }
                },
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = ModelTransport(
            "https://api.typesafe.ai/v1/systemone",
            "test-key",
            "jev-test",
            client=client,
            pricing=Pricing(1, 2),
        )
        task, obs, memory = demo_task(1), observation(), Memory()
        decision = await JevPolicy(transport).choose(
            task, obs, memory, None, generate(obs, task, memory, None)
        )
    assert decision.choice == "a0"
    assert transport.ledger[0]["cost_usd"] == pytest.approx(0.00014)
    assert transport.ledger[0]["resolved_model"] == "jev-pinned-test"
    assert "test-key" not in json.dumps(transport.ledger)


async def test_failed_attempts_stay_in_cost_ledger():
    calls = 0

    def respond(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            429 if calls == 1 else 200, json={"usage": {"input_tokens": 1, "output_tokens": 1}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = ModelTransport("https://example.test", "test-key", "test", client=client)
        await transport.post({}, "jev")
    assert len(transport.ledger) == 2
    assert transport.ledger[0]["status"] == 429
    assert transport.ledger[0]["cost_usd"] is None


async def test_transient_transport_failure_retries_and_preserves_unknown_cost():
    attempts = 0

    def respond(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("connection reset", request=request)
        return httpx.Response(200, json={"usage": {"input_tokens": 7, "output_tokens": 3}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = ModelTransport("https://example.test", "test", "test", client=client)
        await transport.post({}, "jev")
        await transport.post({}, "jev")
        await transport.aclose()
        assert not client.is_closed  # injected client remains owned by the caller
    assert len(transport.ledger) == 3
    assert transport.ledger[0]["error"] == "ConnectError"
    assert transport.ledger[0]["cost_usd"] is None
    assert transport.ledger[-1]["input_tokens"] == 7


async def test_unknown_model_choice_rejected():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                json={
                    "answers": {"action": {"type": "choice", "choice": "invented", "confidence": 1}}
                },
            )
        )
    ) as client:
        transport = ModelTransport("https://example.test", "test", "test", client=client)
        task, obs, memory = demo_task(1), observation(), Memory()
        with pytest.raises(ValueError, match="unknown choice"):
            await JevPolicy(transport).choose(
                task, obs, memory, None, generate(obs, task, memory, None)
            )


def sample(success, cost):
    return {
        "total_cost_usd": cost,
        "result": {
            "strict_success": success,
            "status": "success" if success else "failed",
            "finish_requests": 1,
            "false_completions": 0,
            "violations": [],
            "elapsed_s": 2,
            "actions": 5,
            "planner_calls": 1,
        },
    }


def test_costs_include_failures_and_unknown_cost_is_not_zero():
    result = summarize([sample(True, 2), sample(False, 3)])
    assert result["strict_success_rate"] == 0.5
    assert result["cost_per_success_usd"] == 5
    assert summarize([sample(True, 2), sample(False, None)])["cost_per_success_usd"] is None
    assert summarize([sample(False, 0)])["cost_per_success_usd"] is None


def test_independent_calibration_paired_statistics_and_candidate_recall():
    report = calibration(
        [{"confidence": 0.8, "success": True}, {"confidence": 0.4, "success": False}]
    )
    assert report["brier"] == pytest.approx(0.1)
    assert report["risk_coverage"][-2]["risk"] == 0
    paired = paired_bootstrap({"t1": [True] * 3}, {"t1": [False] * 3}, samples=50)
    assert paired["ci95"] == [1, 1]
    assert candidate_recall([{"a", "b"}, {"a"}], [{"b", "c"}, {"b"}]) == 0.5
    assert pass_four({"t1": [True] * 4, "t2": [False] * 4}) == 0.5
    with pytest.raises(ValueError):
        pass_four({"t1": [True] * 3})
