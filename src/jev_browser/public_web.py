"""Small live public-web tasks with an independent, exact-decimal grader.

Only task goals reach the agent. The oracle uses public HTML, never model output.
These are WebArena-inspired tasks, not official WebArena results.
"""
from __future__ import annotations

import copy
import json
import os
import re
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from .observability import save_analysis, write_json
from .protocol import Budget, RunResult, Task, digest, now

BASE = "https://books.toscrape.com/"
CATEGORIES = {
    "Fiction": "catalogue/category/books/fiction_10/index.html",
    "Mystery": "catalogue/category/books/mystery_3/index.html",
    "Historical Fiction": "catalogue/category/books/historical-fiction_4/index.html",
    "Travel": "catalogue/category/books/travel_2/index.html",
}
TASKS = [
    {"id": 50, "name": "Fiction：跨页筛选与总价", "categories": ["Fiction"],
     "intent": 'On Books to Scrape, inspect every page of the Fiction category. Count the books priced at £40.00 or less and sum their displayed prices, counting each book once. Return only JSON: {"count": integer, "total_gbp": "0.00"}. Do not add anything to a basket.'},
    {"id": 332, "name": "三个类别：逐类数量与总价",
     "categories": ["Mystery", "Historical Fiction", "Travel"],
     "intent": 'On Books to Scrape, inspect every page in Mystery, Historical Fiction, and Travel. For each category, count all books and sum their displayed prices, counting each book once. Return only JSON keyed by the exact category names, each with {"count": integer, "total_gbp": "0.00"}. Do not add anything to a basket.'},
]


def safe_url(url):
    p = urlsplit(url)
    if p.scheme != "https" or p.netloc != "books.toscrape.com" or p.query or p.fragment:
        raise ValueError("oracle link outside the reviewed public site")
    return url


def parse_listing(html, url):
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for card in soup.select("article.product_pod"):
        link, price = card.select_one("h3 a"), card.select_one(".price_color")
        if link is None or price is None:
            raise ValueError("incomplete product card")
        value = price.get_text(strip=True)
        if not re.fullmatch(r"£\d+\.\d{2}", value):
            raise ValueError("unrecognized price")
        rows.append({"url": safe_url(urljoin(url, link["href"])),
                     "title": link.get("title") or link.get_text(strip=True),
                     "pence": int(Decimal(value[1:]) * 100)})
    total = soup.select_one("form.form-horizontal strong")
    if not rows or total is None or not total.get_text(strip=True).isdigit():
        raise ValueError("missing product count or listing")
    nxt = soup.select_one("li.next a")
    return rows, int(total.get_text(strip=True)), safe_url(urljoin(url, nxt["href"])) if nxt else None


async def snapshot(categories):
    started = time.monotonic()
    data, pages = {}, []
    async with httpx.AsyncClient(timeout=20, follow_redirects=False,
                                 transport=httpx.AsyncHTTPTransport(retries=1)) as client:
        for category in categories:
            url = urljoin(BASE, CATEGORIES[category])
            seen, rows, expected = set(), {}, None
            while url:
                if url in seen or len(seen) >= 10:
                    raise ValueError("pagination cycle or page limit")
                seen.add(url)
                response = await client.get(safe_url(url))
                response.raise_for_status()
                items, total, nxt = parse_listing(response.content.decode("utf-8"), url)
                if expected is not None and total != expected:
                    raise ValueError("category count changed during snapshot")
                expected = total
                for row in items:
                    if row["url"] in rows:
                        raise ValueError("duplicate product across pages")
                    rows[row["url"]] = row
                pages.append(url)
                if nxt and urlsplit(nxt).path.rsplit("/", 1)[0] != urlsplit(url).path.rsplit("/", 1)[0]:
                    raise ValueError("pagination left category")
                url = nxt
            if len(rows) != expected:
                raise ValueError("incomplete category snapshot")
            data[category] = sorted(rows.values(), key=lambda x: x["url"])
    return {"data": data, "fingerprint": digest(data), "pages": pages,
            "fetched_at": now(), "elapsed_s": time.monotonic() - started}


def expected_answer(selected, data):
    def total(rows):
        return {"count": len(rows), "total_gbp": f"{Decimal(sum(r['pence'] for r in rows)) / 100:.2f}"}
    if selected["id"] == 50:
        return total([r for r in data["Fiction"] if r["pence"] <= 4000])
    return {name: total(data[name]) for name in selected["categories"]}


def exact_grade(answer, expected):
    def unique(pairs):
        result = {}
        for k, v in pairs:
            if k in result:
                raise ValueError("duplicate JSON key")
            result[k] = v
        return result
    def match(actual, reference):
        if not isinstance(actual, dict) or actual.keys() != reference.keys():
            return False
        if "count" in reference:
            if type(actual["count"]) is not int or actual["count"] != reference["count"]:
                return False
            amount = actual["total_gbp"]
            if not isinstance(amount, str) or not re.fullmatch(r"\d+\.\d{2}", amount):
                return False
            return Decimal(amount) == Decimal(reference["total_gbp"])
        return all(match(actual[k], reference[k]) for k in reference)
    try:
        clean = answer.strip()
        if clean.startswith("```json\n") and clean.endswith("```"):
            clean = clean[8:-3].strip()
        return match(json.loads(clean, object_pairs_hook=unique), expected)
    except (ValueError, TypeError, InvalidOperation, AttributeError):
        return False


async def check_public_web(task_ids):
    if len(task_ids) != 2 or set(task_ids) != {50, 332}:
        raise ValueError("public-web pilot requires task types 50 and 332")
    selected = [copy.deepcopy(next(t for t in TASKS if t["id"] == i)) for i in task_ids]
    report = {"suite": "public-web", "checked_at": now(), "selected_tasks": selected,
              "scope": "WebArena-inspired public tasks; not official WebArena scores",
              "missing_environment": [], "unreachable_sites": [],
              "model_keys_present": {k: bool(os.environ.get(k)) for k in ("TYPESAFE_API_KEY", "PLANNER_API_KEY")}}
    try:
        snap = await snapshot(list(CATEGORIES))
        report["dataset"] = {k: v for k, v in snap.items() if k != "data"}
        report["dataset"]["category_counts"] = {k: len(v) for k, v in snap["data"].items()}
    except (httpx.HTTPError, ValueError) as exc:
        report["unreachable_sites"].append({"site": BASE, "error": type(exc).__name__})
    report["status"] = "blocked" if report["unreachable_sites"] or not all(report["model_keys_present"].values()) else "ready"
    return report


async def run_public_web(args, selected, output):
    from .cli import run_trial
    task = Task(id=f"public-web-{selected['id']}", control_mode="dynamic",
                objective=selected["intent"], start_url=urljoin(BASE, CATEGORIES[selected["categories"][0]]),
                allowed_origins=[BASE.rstrip("/")], requires_final_answer=True,
                constraints=["Read-only public catalogue inspection. Do not submit forms, add to basket or purchase."])
    oracle_started = time.monotonic()
    try:
        before = await snapshot(selected["categories"])
    except (httpx.HTTPError, ValueError) as exc:
        # A failed oracle must produce a durable failed task, not leave a trial
        # permanently marked running or start an ungradable paid agent episode.
        from .evaluation import efficiency_profile

        output.mkdir(parents=True, exist_ok=False)
        manifest = {"suite": "public-web", "task_id": selected["id"], "backend": "playwright",
                    "task_hash": digest(task.model_dump(mode="json")), "fixture_hash": None,
                    "code_hash": digest({p.name: digest(p.read_text())
                                         for p in Path(__file__).parent.glob("*.py")}),
                    "budget": Budget(max_actions=args.max_actions,
                                     max_seconds=args.max_seconds).model_dump()}
        result = RunResult(task_id=task.id, status="failed", strict_success=False,
                           reason=f"Public oracle unavailable: {type(exc).__name__}; no model calls",
                           actions=0, cycles=0, planner_calls=0, elapsed_s=0)
        report = {"manifest": manifest, "result": result.model_dump(), "final_answer": "",
                  "grade": {"strict_success": False, "data_valid": False,
                            "verification_error": type(exc).__name__,
                            "oracle_elapsed_s": time.monotonic() - oracle_started},
                  "model_calls": [], "efficiency": efficiency_profile([], actions=0, elapsed_s=0),
                  "end_to_end_s": 0, "total_cost_usd": None}
        for name, value in (("task", task.model_dump(mode="json")), ("manifest", manifest),
                            ("result", result.model_dump()), ("report", report)):
            write_json(output / f"{name}.json", value)
        save_analysis(output, report)
        return report
    trial_args = copy.copy(args)
    trial_args.command, trial_args.backend = "run", "playwright"
    trial_args._benchmark_task = task
    trial_args._benchmark_manifest = {"suite": "public-web", "task_id": selected["id"],
                                      "fixture_hash": before["fingerprint"], "seed": args.seed}
    report = await run_trial(trial_args, output=output)
    after, error = None, None
    try:
        after = await snapshot(selected["categories"])
    except (httpx.HTTPError, ValueError) as exc:
        error = type(exc).__name__
    stable = after is not None and before["fingerprint"] == after["fingerprint"]
    correct = exact_grade(report["final_answer"], expected_answer(selected, before["data"]))
    strict = stable and correct and report["result"]["status"] == "success" and not report["result"]["violations"]
    report["grade"] = {"strict_success": strict, "answer_correct": correct, "data_valid": stable,
                       "verification_error": error, "source": "independent public HTML / exact decimal",
                       "oracle_elapsed_s": before["elapsed_s"] + (after["elapsed_s"] if after else 0)}
    report["result"]["strict_success"] = strict
    if not strict and report["result"]["status"] == "success":
        report["result"].update(status="failed", reason="independent public-web grader rejected completion")
    report["evidence_scope"] = "Public WebArena-inspired task; not an official WebArena score"
    write_json(output / "oracle.json", {"before": before, "after": after})
    write_json(output / "report.json", report)
    write_json(output / "result.json", report["result"])
    save_analysis(output, report)
    return report
