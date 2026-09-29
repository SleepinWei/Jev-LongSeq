"""Run the existing CLI with extra response-usage accounting, without policy changes.

Prices apply to the September 27, 2026 weekend/off-peak pilot only. API usage
estimates are not invoices. Raw usage is retained; unknown cache usage stays unknown.
"""

from jev_browser import cli
from jev_browser.models import ModelTransport

original_post = ModelTransport.post


async def measured_post(self, payload, kind):
    response = await original_post(self, payload, kind)
    record = self.ledger[-1]
    usage = response.get("usage", {})
    record["raw_usage"] = usage
    incoming = record.get("input_tokens")
    outgoing = record.get("output_tokens")
    if kind == "jev" and incoming is not None:
        record["cost_usd"] = incoming * 0.042 / 1_000_000
        record["cost_basis"] = "TypeSafe published input price $0.042/M; output free"
    elif self.model == "deepseek-flash" and incoming is not None and outgoing is not None:
        cached = usage.get("prompt_cache_hit_tokens")
        record["cached_input_tokens"] = cached
        if cached is not None:
            record["cost_usd"] = (
                (incoming - cached) * 0.15 + cached * 0.003 + outgoing * 0.6
            ) / 1_000_000
            record["cost_basis"] = "DeepSeek weekend/off-peak published prices; actual cache usage"
        else:
            record["cost_usd"] = None
            record["cost_basis"] = "Cache split unknown; no exact cost inferred"
    return response


if __name__ == "__main__":
    ModelTransport.post = measured_post
    raise SystemExit(cli.main())
