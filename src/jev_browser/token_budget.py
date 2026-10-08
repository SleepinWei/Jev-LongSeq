"""Provider-aware preflight estimates; API usage remains the token source of truth."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from .context_budget import wire_json

DEFAULT_DS_MAX_TOKENS = 1_000_000
DEFAULT_JEV_MAX_TOKENS = 48_000
JEV_HEAD_MAX_TOKENS = 32_000
DEFAULT_OUTPUT_RESERVE = 16_384
DS_TOKENIZER_PATH = Path.home() / ".cache/jev-longseq/tokenizers/deepseek-v4.json"


@lru_cache(maxsize=4)
def load_tokenizer(provider, path):
    if provider == "deepseek-v4":
        from tokenizers import Tokenizer

        if not Path(path).is_file():
            raise ValueError("DeepSeek token budget requires the official V4 tokenizer data; "
                             "run scripts/install_context_tokenizers.py on the execution host")
        return Tokenizer.from_file(path)
    import tiktoken

    return tiktoken.get_encoding("o200k_base")


@dataclass(frozen=True)
class TokenBudget:
    provider: str
    maximum: int
    output_reserve: int = 0
    head_maximum: int | None = None
    tokenizer_path: str = ""

    def __post_init__(self):
        if self.maximum <= 0 or self.output_reserve < 0:
            raise ValueError("Context token limit must be positive; reserve must be nonnegative")

    def count(self, text):
        tokenizer = load_tokenizer(self.provider, self.tokenizer_path)
        if self.provider == "deepseek-v4":
            return len(tokenizer.encode(text, add_special_tokens=False).ids)
        # Jev publishes no local tokenizer. This is explicitly a proxy, with
        # 10% headroom, not a claim to know the supplier's exact token count.
        return math.ceil(len(tokenizer.encode(text, disallowed_special=())) * 1.10)

    def measure(self, payload):
        head = None
        if "questions" in payload:
            state = self.count(wire_json(payload["state"]))
            questions = [self.count(wire_json(q)) for q in payload["questions"].values()]
            tokens = state + sum(questions) + 256
            head = state + max(questions, default=0) + 256
            reserve = 0  # Decision outputs do not consume the published input limit.
        else:
            # Count model text, not UTF-8 bytes or escaped HTTP message strings.
            tokens = 128 + sum(self.count(m["content"]) + 8 for m in payload["messages"])
            tokens += sum(self.count(wire_json(payload[k])) for k in ("tools", "response_format")
                          if k in payload)
            reserve = max(self.output_reserve,
                          int(payload.get("max_completion_tokens", payload.get("max_tokens", 0))))
        return {"input_tokens_estimate": tokens, "max_tokens": self.maximum,
                "output_reserve_tokens": reserve, "tokenizer": self.provider,
                "token_count_is_estimate": True,
                "token_estimate_margin": 1.10 if self.provider == "jev-o200k-proxy" else 1.0,
                "head_tokens_estimate": head, "head_max_tokens": self.head_maximum}

    def fits(self, metrics):
        return (metrics["input_tokens_estimate"] + metrics["output_reserve_tokens"] <= self.maximum
                and (self.head_maximum is None
                     or metrics["head_tokens_estimate"] <= self.head_maximum))


def chat_token_budget(endpoint, model, purpose):
    ds = (urlsplit(endpoint).hostname == "api.deepseek.com"
          and model.startswith("deepseek-"))
    variable = ("POLICY_CONTEXT_MAX_TOKENS" if purpose == "llm_policy" else
                "BRAIN_FINISH_CONTEXT_MAX_TOKENS" if purpose == "dynamic_finish" else
                "BRAIN_CONTEXT_MAX_TOKENS")
    configured = os.environ.get(variable, os.environ.get("DS_CONTEXT_MAX_TOKENS") if ds else None)
    if configured is None and not ds:
        return None  # Preserve existing limits for unrelated providers.
    return TokenBudget("deepseek-v4" if ds else "jev-o200k-proxy",
                       int(configured or DEFAULT_DS_MAX_TOKENS),
                       int(os.environ.get("CONTEXT_OUTPUT_RESERVE_TOKENS", DEFAULT_OUTPUT_RESERVE)),
                       tokenizer_path=os.environ.get("DEEPSEEK_TOKENIZER_PATH", str(DS_TOKENIZER_PATH)))


def jev_token_budget():
    return TokenBudget("jev-o200k-proxy",
                       int(os.environ.get("JEV_CONTEXT_MAX_TOKENS", DEFAULT_JEV_MAX_TOKENS)),
                       head_maximum=JEV_HEAD_MAX_TOKENS)
