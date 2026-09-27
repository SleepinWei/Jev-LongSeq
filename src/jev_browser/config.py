"""Explicit dotenv loading without shell evaluation or secret logging."""

from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: str) -> list[str]:
    loaded = []
    for number, raw in enumerate(Path(path).expanduser().read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        if "=" not in line:
            raise ValueError(f"invalid environment assignment at line {number}")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key.isidentifier():
            raise ValueError(f"invalid environment name at line {number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        os.environ.setdefault(key, value)
        loaded.append(key)
    return loaded


def use_text_model_for_planner() -> None:
    """Use existing TEXT_MODEL settings as defaults, preserving explicit planner settings."""
    for target, source in [
        ("PLANNER_API_KEY", "TEXT_MODEL_API_KEY"),
        ("PLANNER_MODEL", "TEXT_MODEL"),
    ]:
        if os.environ.get(source) and not os.environ.get(target):
            os.environ[target] = os.environ[source]
    base = os.environ.get("TEXT_MODEL_BASE_URL", "").rstrip("/")
    if base and not os.environ.get("PLANNER_ENDPOINT"):
        os.environ["PLANNER_ENDPOINT"] = base + "/chat/completions"
