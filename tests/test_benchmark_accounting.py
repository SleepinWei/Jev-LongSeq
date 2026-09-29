import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "measure_longseq", Path(__file__).resolve().parents[1] / "examples" / "measure_longseq.py"
)
measure_longseq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(measure_longseq)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,model,usage,expected",
    [
        ("jev", "jev-latest", {}, 0.042),
        ("dynamic_feedback", "deepseek-flash", {"prompt_cache_hit_tokens": 800_000}, 0.0924),
        ("dynamic_feedback", "deepseek-flash", {}, None),
    ],
)
async def test_usage_accounting(monkeypatch, kind, model, usage, expected):
    async def fake_post(self, payload, kind):
        self.ledger.append({"input_tokens": 1_000_000, "output_tokens": 100_000})
        return {"usage": usage}

    monkeypatch.setattr(measure_longseq, "original_post", fake_post)
    transport = SimpleNamespace(model=model, ledger=[])
    result = await measure_longseq.measured_post(transport, {}, kind)
    assert result == {"usage": usage}
    if expected is None:
        assert transport.ledger[-1]["cost_usd"] is None
    else:
        assert transport.ledger[-1]["cost_usd"] == pytest.approx(expected)
