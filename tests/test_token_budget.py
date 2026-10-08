import copy
import json

import httpx
import pytest
from test_context_budget import Capture, sample

from jev_browser.context_budget import ContextBudgetExceeded, project_chat_request, project_request
from jev_browser.dynamic import generate_dynamic
from jev_browser.models import JevPolicy, ModelTransport
from jev_browser.token_budget import (
    DS_TOKENIZER_PATH,
    TokenBudget,
    chat_token_budget,
    jev_token_budget,
)


def ds_budget(maximum=1_000_000, reserve=16384):
    return TokenBudget("deepseek-v4", maximum, reserve, tokenizer_path=str(DS_TOKENIZER_PATH))


def chat_payload():
    task, obs, memory = sample()
    # Protected task text must survive a byte soft target; Chinese bytes != tokens.
    return {"model": "deepseek-flash", "messages": [
        {"role": "system", "content": "Inspect current evidence."},
        {"role": "user", "content": json.dumps({
            "trusted_goal": "关键节点" * 6000, "hard_constraints": ["No replay"],
            "untrusted_observation": obs.model_dump(), "untrusted_memory": memory.context()},
            ensure_ascii=False)}]}


def test_provider_defaults_and_unrelated_provider_legacy_limits(monkeypatch):
    for name in ("DS_CONTEXT_MAX_TOKENS", "POLICY_CONTEXT_MAX_TOKENS", "JEV_CONTEXT_MAX_TOKENS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("POLICY_CONTEXT_MAX_BYTES", "48000")
    budget = chat_token_budget("https://api.deepseek.com/chat/completions", "deepseek-flash", "llm_policy")
    assert budget.maximum == 1_000_000
    assert jev_token_budget().maximum == 48_000
    assert jev_token_budget().head_maximum == 32_000
    assert chat_token_budget("https://model.test/chat", "test", "llm_policy") is None
    monkeypatch.setenv("POLICY_CONTEXT_MAX_TOKENS", "8000")
    assert chat_token_budget("https://api.deepseek.com/chat", "deepseek-flash", "llm_policy").maximum == 8000


@pytest.mark.parametrize("purpose", ["llm_policy", "dynamic_feedback", "dynamic_finish"])
def test_ds_protected_context_exceeding_byte_soft_target_is_sent_losslessly(purpose):
    payload = chat_payload()
    saved = copy.deepcopy(payload)
    projected, metrics = project_chat_request(payload, max_bytes=48000, purpose=purpose,
                                             token_budget=ds_budget())
    assert metrics["after_bytes"] > 48_000
    assert metrics["max_bytes"] is None and metrics["max_tokens"] == 1_000_000
    assert metrics["input_tokens_estimate"] < metrics["after_bytes"]
    assert json.loads(projected["messages"][-1]["content"])["trusted_goal"] == (
        json.loads(payload["messages"][-1]["content"])["trusted_goal"])
    assert payload == saved
    assert metrics["memory_sections_bytes"]["pending_writes"] > 0


def test_output_reserve_counts_against_token_window():
    payload = {"messages": [{"role": "user", "content": "Inspect this."}], "max_tokens": 2000}
    budget = ds_budget(2000, reserve=0)
    metrics = budget.measure(payload)
    assert metrics["output_reserve_tokens"] == 2000 and not budget.fits(metrics)
    payload["max_tokens"] = 10
    assert budget.fits(budget.measure(payload))


def test_exact_scoped_readback_uses_token_budget_and_never_excerpts_evidence():
    content = {"trusted_goal": "Keep original", "readback_evidence": {"v1": "关键" * 12000}}
    payload = {"messages": [{"role": "user", "content": json.dumps(content, ensure_ascii=False)}]}
    projected, metrics = project_chat_request(payload, max_bytes=12000, purpose="dynamic_readback",
                                             token_budget=ds_budget())
    assert json.loads(projected["messages"][-1]["content"]) == content
    assert metrics["after_bytes"] > 12000 and metrics["max_bytes"] is None
    with pytest.raises(ContextBudgetExceeded):
        project_chat_request(payload, max_bytes=12000, purpose="dynamic_readback",
                             token_budget=ds_budget(2000))


async def test_ds_hard_token_overflow_stops_before_http(monkeypatch):
    monkeypatch.setenv("DS_CONTEXT_MAX_TOKENS", "1000")

    def respond(request):
        pytest.fail("A token overflow must never dispatch")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = ModelTransport("https://api.deepseek.com/chat/completions", "test", "deepseek-flash",
                                   client=client)
        with pytest.raises(ContextBudgetExceeded) as caught:
            await transport.post(chat_payload(), "llm_policy")
    assert caught.value.metrics["max_tokens"] == 1000
    assert not transport.ledger


async def test_ds_static_planner_also_checks_its_token_window(monkeypatch):
    monkeypatch.setenv("BRAIN_CONTEXT_MAX_TOKENS", "1000")

    def respond(request):
        pytest.fail("Static planning must also obey DS token limits")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = ModelTransport("https://api.deepseek.com/chat/completions", "test", "deepseek-flash",
                                   client=client)
        with pytest.raises(ContextBudgetExceeded):
            await transport.post({"messages": [{"role": "user", "content": "Plan this task"}]}, "planner")
    assert not transport.ledger


async def test_jev_default_accepts_token_sized_context_previously_blocked_by_bytes():
    task, obs, memory = sample()
    memory.key_nodes["large"] = {"source": {"quote": "关键" * 10000}}
    transport = Capture()
    policy = JevPolicy(transport)
    await policy.choose(task, obs, memory, None, generate_dynamic(obs, task))
    assert policy.last_context_projection["after_bytes"] > 48000
    assert policy.last_context_projection["max_tokens"] == 48000
    assert policy.last_context_projection["head_max_tokens"] == 32000
    assert policy.last_context_projection["token_count_is_estimate"]
    assert transport.requests[0]["state"]["untrusted_memory"]["key_nodes"][-1]["source"]["quote"] == "关键" * 10000


def test_jev_per_head_limit_is_checked_even_when_total_is_below_48k():
    task, obs, memory = sample()
    payload = {"state": {"trusted_goal": "关键" * 29500, "hard_constraints": [],
                         "untrusted_observation": obs.model_dump(), "untrusted_memory": memory.context()},
               "questions": {"action": {"instructions": "Inspect"}}}
    with pytest.raises(ContextBudgetExceeded) as caught:
        project_request(payload, token_budget=jev_token_budget())
    metrics = caught.value.metrics
    assert metrics["head_tokens_estimate"] > 32000
    assert metrics["input_tokens_estimate"] < 48000


def test_missing_official_tokenizer_is_explicit_and_never_uses_a_byte_ratio():
    with pytest.raises(ValueError, match="official V4 tokenizer"):
        TokenBudget("deepseek-v4", 1_000_000, tokenizer_path="/tmp/no-such-jev-tokenizer.json").count("Hi")
