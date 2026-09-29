"""One recorded arm of the live Xiaohongshu DS versus Jev+DS comparison.

This uses the existing controller unchanged. Pure DS replaces only Jev's
candidate policy with JsonPolicy backed by the same DeepSeek model.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from measure_longseq import measured_post

from jev_browser import cli
from jev_browser.config import load_env_file, use_text_model_for_planner
from jev_browser.models import ModelTransport


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", choices=["pure-ds", "jev-ds"], required=True)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--goal", default="在小红书上查找总结最近适合staycation的地方")
    parser.add_argument("--optimized", action="store_true")
    args = parser.parse_args()
    load_env_file(args.env_file)
    use_text_model_for_planner()
    for suffix in ("API_KEY", "ENDPOINT", "MODEL"):
        os.environ[f"POLICY_{suffix}"] = os.environ[f"PLANNER_{suffix}"]
    os.environ["POLICY_TIMEOUT_SECONDS"] = os.environ.get("PLANNER_TIMEOUT_SECONDS", "90")
    ModelTransport.post = measured_post
    # Both arms use identical goal, observation, memory, budgets, and recorder.
    sys.argv = [
        "jev-browser", "browse", "https://www.xiaohongshu.com/explore",
        "--goal", args.goal, "--backend", "chrome", "--brain", "api",
        "--policy", "llm" if args.arm == "pure-ds" else "jev",
        "--brain-interval", "12", "--max-seconds", "480", "--max-actions", "60",
        "--max-feedback-calls", "40", "--live-preview", "--output", str(args.output),
    ]
    if args.optimized:
        sys.argv.append("--preconnect")
    result = cli.main()
    config = {
        "arm": args.arm, "goal": args.goal, "model": os.environ["PLANNER_MODEL"],
        "pure_ds_definition": "Same dynamic controller; DeepSeek replaces Jev candidate decisions",
        "frame_source": "Project live-preview archive of actual task-tab screenshots",
        "rate_date": "2026-09-27", "rates": "DeepSeek off-peak, TypeSafe published rates",
        "optimized": args.optimized,
    }
    (args.output / "comparison-arm.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    )
    return result


if __name__ == "__main__":
    raise SystemExit(main())
