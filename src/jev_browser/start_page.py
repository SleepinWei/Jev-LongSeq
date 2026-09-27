"""Choose a starting page from the user's goal before observing any web content."""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field


class StartPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(max_length=4096)
    reason: str = Field(min_length=1, max_length=500)


def validate_url(value):
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError("起始网址必须是有效的 http/https 地址")
    if re.search(r"[\s\\\x00-\x1f]", value):
        raise ValueError("起始网址包含无效字符")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password):
        raise ValueError("起始网址必须是有效的 http/https 地址，且不能包含登录凭据")
    _ = parsed.port  # Validate malformed/out-of-range ports before starting a browser.
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path or "/",
                       parsed.query, parsed.fragment))


def prompt_url(goal):
    explicit = re.search(r"https?://[^\s<>\"'`，。；！？（）【】]+", goal, re.I)
    if explicit:
        return validate_url(explicit.group().rstrip(".,;!?)]}"))
    domain = re.search(
        r"(?<![a-z0-9_@./-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+"
        r"(?:com|org|net|edu|gov|io|ai|app|dev|cn|co|me|info)"
        r"(?![a-z0-9_.-])(?:/[^\s<>\"'`，。；！？（）【】]*)?", goal, re.I,
    )
    if domain:
        return validate_url("https://" + domain.group().rstrip(".,;!?)]}"))
    return None


async def resolve_start_page(goal, supplied, transport):
    if supplied:
        return {"url": validate_url(supplied), "source": "user", "reason": "使用指定网址"}
    url = prompt_url(goal)
    if url:
        return {"url": url, "source": "prompt", "reason": "使用 prompt 中的首个网址"}
    data = await transport.post({
        "model": transport.model,
        "messages": [
            {"role": "system", "content": (
                "Choose one public HTTP(S) starting page for the user's browser task. "
                "Use the official homepage of an explicitly named website. For open-ended "
                "web research/search without a named site, choose a public search engine. "
                "Do not fabricate private URLs, account-specific paths, credentials or results. "
                "Do not execute the task. If the goal refers to 'this/current page' without "
                "identifying any website, return an empty url and explain what is missing. "
                "Return only JSON matching the provided schema; give a brief Chinese reason."
            )},
            {"role": "user", "content": json.dumps(
                {"goal": goal, "schema": StartPage.model_json_schema()}, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
    }, "start_page")
    try:
        choice = StartPage.model_validate_json(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError("无法解析起始网页，请补充网址或重试") from exc
    if not choice.url:
        raise ValueError(f"无法判断起始网页：{choice.reason}")
    return {"url": validate_url(choice.url), "source": "model", "reason": choice.reason}
