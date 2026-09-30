"""Install/update the current user's loopback-only macOS Studio LaunchAgent."""

import argparse
import json
import os
import plistlib
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--ultrafast-root", type=Path, required=True)
    parser.add_argument("--saas-root", type=Path,
                        help="Enable SaaS-Bench using the dedicated Colima Docker context")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if not (root / ".venv/bin/python").exists():
        parser.error("Run uv sync --frozen --extra dev --extra chrome first")
    if not args.env_file.is_file() or not (args.ultrafast_root / "jev_ultrafast").is_dir():
        parser.error("Model env file and original Ultrafast source must exist")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/api/launcher", timeout=3) as response:
            status = json.load(response)
        if "running" not in status or status["running"]:
            parser.error("Port belongs to another service or Studio has an active task")
    except urllib.error.HTTPError:
        parser.error("Port is occupied by another service")
    except urllib.error.URLError as exc:
        if not isinstance(exc.reason, ConnectionRefusedError):
            raise
    label = "com.jev.longseq.studio"
    logs = root / "runs/service"
    logs.mkdir(parents=True, exist_ok=True)
    path = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": label,
        "ProgramArguments": [str(root / ".venv/bin/python"), "-m", "jev_browser.inspector",
                             "--runs", str(root / "runs"), "--port", str(args.port),
                             "--env-file", str(args.env_file.resolve()),
                             "--ultrafast-root", str(args.ultrafast_root.resolve())],
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {"PATH": f"{root}/.venv/bin:/opt/homebrew/bin:"
                                 f"{Path.home()}/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(logs / "studio.log"),
        "StandardErrorPath": str(logs / "studio.err.log"),
    }
    if args.saas_root:
        if not (args.saas_root / "saas_bench/apps.yaml").is_file():
            parser.error("SaaS-Bench checkout missing saas_bench/apps.yaml")
        payload["EnvironmentVariables"].update({
            "SAAS_BENCH_ROOT": str(args.saas_root.resolve()),
            "SAAS_SLOT_PREFIX": "jevsaas", "SAAS_BASE_PORT": "31000",
            "DOCKER_CONTEXT": "colima-saas-bench", "DOCKER_DEFAULT_PLATFORM": "linux/amd64",
        })
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "print", domain], check=True, stdout=subprocess.DEVNULL)
    existing = subprocess.run(["launchctl", "print", f"{domain}/{label}"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if existing.returncode == 0:
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], check=True)
    path.write_bytes(plistlib.dumps(payload))
    # bootout can return before launchd has released the previous service label.
    for attempt in range(3):
        registered = subprocess.run(["launchctl", "bootstrap", domain, str(path)],
                                    capture_output=True, text=True)
        if registered.returncode == 0:
            break
        if attempt == 2:
            raise RuntimeError(f"Studio bootstrap failed: {registered.stderr.strip()}")
        time.sleep(1)
    print(f"Installed {label}: http://127.0.0.1:{args.port}")


if __name__ == "__main__":
    main()
