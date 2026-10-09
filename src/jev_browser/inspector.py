"""Local trace inspector, adapted from Jev Ultrafast's stdlib HTTP inspector.

Static UI and local-request protection retain the upstream MIT attribution.
Run artifacts are read-only; launch endpoints create isolated local runs.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

STATIC = Path(__file__).parent / "static"


def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def events(path):
    """A writer may be halfway through its last JSONL line."""
    rows = []
    try:
        with path.open() as stream:
            for line in stream:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return rows


def stamp(value):
    try:
        return datetime.fromisoformat(value).timestamp()
    except (ValueError, TypeError):
        return 0


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.frame_cache = {}
        self.navigation_cache = {}
        self.lock = threading.Lock()
        self.child = None
        self.child_id = None

    def launch_status(self):
        from .run_status import activities

        active = activities(self.root)
        with self.lock:
            code = self.child.poll() if self.child else None
            return {
                "id": self.child_id,
                "running": bool((self.child and code is None) or active),
                "exit_code": code,
                "activities": active,
            }

    def launch_prompt(self, body):
        if not isinstance(body, dict):
            raise ValueError("请求必须是 JSON 对象")
        goal = body.get("prompt", "")
        if not isinstance(goal, str) or not 1 <= len(goal.strip()) <= 8000:
            raise ValueError("请输入 1–8000 字的 prompt")
        scenario = body.get("scenario", "catalog")
        if not isinstance(scenario, str) or scenario not in {"catalog", "web"}:
            raise ValueError("请选择目录样例或自定义网页")
        brain = body.get("brain", "api")
        if not isinstance(brain, str) or brain not in {"codex", "api"}:
            raise ValueError("请选择 Codex 或已配置的模型 API")
        if brain == "api":
            from .config import use_text_model_for_planner

            use_text_model_for_planner()
            if not os.environ.get("PLANNER_MODEL") or not os.environ.get("PLANNER_ENDPOINT"):
                raise ValueError("未配置模型 API，请在服务端配置 PLANNER_* 或 TEXT_MODEL_*")
        records = body.get("records", 12)
        if type(records) is not int or not 1 <= records <= 100:
            raise ValueError("样例记录数须为 1–100")
        if scenario == "web":
            url = body.get("url", "")
            if not isinstance(url, str) or len(url) > 4096:
                raise ValueError("请输入有效的 http/https 起始网址")
            url = url.strip()
            if url:
                from .start_page import validate_url

                url = validate_url(url)
            command = ["browse", *([url] if url else []), "--goal", goal.strip(),
                       "--backend", "chrome"]
        else:
            command = ["demo", "--records", str(records), "--goal", goal.strip()]
        if not os.environ.get("TYPESAFE_API_KEY"):
            raise ValueError("未配置 Jev 密钥；请用 --env-file 指定模型配置后重启服务")
        with self.lock:
            if self.child and self.child.poll() is None:
                raise RuntimeError("已有任务正在运行，请等待完成")
            run_id = f"ui-prompt-{time.time_ns()}"
            output = self.root / run_id
            output.mkdir(parents=True)
            with (output / "console.log").open("w") as stream:
                self.child = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "jev_browser",
                        *command,
                        "--mode",
                        "dynamic",
                        "--policy",
                        "jev",
                        "--planner",
                        "llm",
                        "--brain",
                        brain,
                        "--live-preview",
                        "--max-seconds",
                        "300",
                        "--max-actions",
                        "150",
                        "--max-feedback-calls",
                        "40",
                        "--output",
                        str(output / "task"),
                    ],
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                )
            self.child_id = run_id + "/task"
            return {"id": self.child_id}

    def paths(self):
        found = {}
        for path in self.root.rglob("manifest.json"):
            resolved = path.parent.resolve()
            if resolved.is_relative_to(self.root):
                found[resolved.relative_to(self.root).as_posix() or "."] = resolved
        return found

    def path(self, run_id):
        path = self.paths().get(run_id)
        if path is None:
            raise ValueError("运行不存在")
        return path

    def file(self, path, name):
        target = (path / name).resolve()
        if not target.is_relative_to(path) or not target.is_relative_to(self.root):
            raise ValueError("无效文件路径")
        return target

    def listing(self):
        result = []
        for run_id, path in self.paths().items():
            manifest = read_json(self.file(path, "manifest.json"), {})
            report = read_json(self.file(path, "report.json"), {})
            outcome = report.get("result") or read_json(self.file(path, "result.json"), {})
            entry = {
                    "id": run_id,
                    "task_id": manifest.get("task_id", run_id),
                    "started_at": manifest.get("started_at", ""),
                    "policy": manifest.get("policy", "unknown"),
                    "status": outcome.get("status", "unfinished"),
                    "strict_success": outcome.get("strict_success"),
                    "actions": outcome.get("actions"),
                }
            if manifest.get("suite") == "saas-bench":
                grade = report.get("grade", {})
                entry["benchmark"] = {
                    "data_valid": grade.get("data_valid"),
                    "strict_success": outcome.get("strict_success"),
                    "earned": grade.get("earned"), "total": grade.get("total"),
                }
            result.append(entry)
        return sorted(result, key=lambda r: r["started_at"], reverse=True)

    def frames(self, path):
        recorded = self.file(path, "frames.jsonl")
        if recorded.exists():
            return events(recorded)
        trace = self.file(path, "trace.zip")
        if not trace.exists():
            return []
        key = (str(trace), trace.stat().st_mtime_ns, trace.stat().st_size)
        with self.lock:
            if key in self.frame_cache:
                return self.frame_cache[key]
            frames = []
            try:
                with zipfile.ZipFile(trace) as archive:
                    members = set(archive.namelist())
                    for name in members:
                        if (
                            not name.endswith(".trace")
                            or archive.getinfo(name).file_size > 64_000_000
                        ):
                            continue
                        context = {}
                        for line in archive.read(name).decode().splitlines():
                            try:
                                row = json.loads(line)
                            except ValueError:
                                continue
                            if row.get("type") == "context-options":
                                context = row
                            if row.get("type") != "screencast-frame":
                                continue
                            resource = row.get("file") or "resources/" + row.get("sha1", "")
                            if resource not in members or not resource.endswith(
                                (".jpeg", ".jpg", ".png")
                            ):
                                continue
                            wall = row.get("frameSwapWallTime")
                            if wall is None and context.get("wallTime") is not None:
                                wall = (
                                    context["wallTime"]
                                    + row["timestamp"]
                                    - context["monotonicTime"]
                                )
                            if wall is not None:
                                frames.append(
                                    {
                                        "time": wall / 1000,
                                        "resource": resource,
                                        "width": row["width"],
                                        "height": row["height"],
                                    }
                                )
            except (OSError, zipfile.BadZipFile):
                return []  # trace.zip may still be being finalized
            frames.sort(key=lambda f: f["time"])
            if len(self.frame_cache) > 12:
                self.frame_cache.clear()
            self.frame_cache[key] = frames
            return frames

    def navigations(self, path):
        """Recover main document statuses from historical Playwright traces."""
        trace = self.file(path, "trace.zip")
        if not trace.exists():
            return []
        key = (str(trace), trace.stat().st_mtime_ns, trace.stat().st_size)
        with self.lock:
            if key in self.navigation_cache:
                return self.navigation_cache[key]
            result = []
            try:
                with zipfile.ZipFile(trace) as archive:
                    for info in archive.infolist():
                        if not info.filename.endswith(".network") or info.file_size > 64_000_000:
                            continue
                        for line in archive.read(info).decode().splitlines():
                            try:
                                row = json.loads(line).get("snapshot", {})
                                if row.get("_resourceType") != "document":
                                    continue
                                result.append(
                                    {
                                        "time": stamp(row.get("startedDateTime")),
                                        "url": row["request"]["url"],
                                        "status": row["response"]["status"],
                                    }
                                )
                            except (ValueError, KeyError, TypeError):
                                continue
            except (OSError, zipfile.BadZipFile):
                return []
            result.sort(key=lambda r: r["time"])
            if len(self.navigation_cache) > 12:
                self.navigation_cache.clear()
            self.navigation_cache[key] = result
            return result

    def data(self, run_id):
        path = self.path(run_id)
        rows = events(self.file(path, "trajectory.jsonl"))
        for index, row in enumerate(rows):
            row["index"], row["at"] = index, stamp(row.get("time"))
        return {
            "id": run_id,
            "manifest": read_json(self.file(path, "manifest.json"), {}),
            "task": read_json(self.file(path, "task.json"), {}),
            "report": read_json(self.file(path, "report.json")),
            "result": read_json(self.file(path, "result.json")),
            "events": rows,
            "process_scores": events(self.file(path, "process-scores.jsonl")),
            "frames": [{k: v for k, v in f.items() if k != "resource"} for f in self.frames(path)],
            "has_final": self.file(path, "final.png").exists(),
            "navigations": self.navigations(path),
        }

    def live(self, run_id):
        path = self.path(run_id)
        live = read_json(self.file(path, "live.json"), {})
        age = time.time() - live.get("time", 0)
        return {
            **live,
            "active": bool(
                live.get("active") and age < 5 and not self.file(path, "result.json").exists()
            ),
            "age_s": round(age, 1) if live else None,
        }

    def image(self, run_id, frame):
        path = self.path(run_id)
        if frame in {"live", "final"}:
            name = "live.jpg" if frame == "live" else "final.png"
            return self.file(
                path, name
            ).read_bytes(), "image/jpeg" if frame == "live" else "image/png"
        index = int(frame)
        frames = self.frames(path)
        if not 0 <= index < len(frames):
            raise ValueError("截图不存在")
        if self.file(path, "frames.jsonl").exists():
            name = frames[index]["resource"]
            if not name.startswith("preview/") or not name.endswith(".jpg"):
                raise ValueError("无效截图路径")
            target = self.file(path, name)
            if target.stat().st_size > 8_000_000:
                raise ValueError("截图过大")
            return target.read_bytes(), "image/jpeg"
        with zipfile.ZipFile(self.file(path, "trace.zip")) as archive:
            name = frames[index]["resource"]
            if archive.getinfo(name).file_size > 8_000_000:
                raise ValueError("截图过大")
            return archive.read(name), "image/png" if name.endswith(".png") else "image/jpeg"

    def launch_demo(self):
        with self.lock:
            if self.child and self.child.poll() is None:
                raise RuntimeError("已有任务正在运行，请等待完成")
            run_id = f"ui-demo-{time.time_ns()}"
            output = self.root / run_id
            output.mkdir(parents=True)
            with (output / "console.log").open("w") as stream:
                self.child = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "jev_browser",
                        "demo",
                        "--records",
                        "12",
                        "--policy",
                        "rule",
                        "--planner",
                        "rule",
                        "--live-preview",
                        "--max-seconds",
                        "90",
                        "--output",
                        str(output / "catalog-12"),
                    ],
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                )
            self.child_id = run_id + "/catalog-12"
            return {"id": self.child_id}


def make_server(root, port=8768):
    store, token = Store(root), secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def send(self, status, content, mime="application/json", etag=None):
            content = content if isinstance(content, bytes) else content.encode()
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; frame-ancestors 'none'",
            )
            if etag:
                self.send_header("ETag", etag)
            self.end_headers()
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def local(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def do_GET(self):
            if not self.local():
                return self.send(403, "Forbidden", "text/plain")
            url = urlparse(self.path)
            args = parse_qs(url.query)
            run_id = args.get("id", [""])[0]
            try:
                if url.path == "/api/diagnostics":
                    from .diagnostics import Diagnostics, encode

                    allowed = {"view", "max_bytes", "cursor", "limit", "cycle", "kind", "file",
                               "pointer", "line", "baseline", "call_id", "attempt_id", "span_id",
                               "observation_id"}
                    if set(args) - allowed - {"id"}:
                        return self.send(400, json.dumps({"error": "unknown diagnostics parameter"}))
                    try:
                        result = Diagnostics(store.root, run_id).query(
                            **{key: value[0] for key, value in args.items() if key in allowed})
                    except (ValueError, KeyError, IndexError, OSError):
                        return self.send(400, json.dumps({"error": "invalid or unavailable diagnostics query"}))
                    return self.send(200, encode(result))
                if url.path == "/api/runs":
                    return self.send(200, json.dumps(store.listing()))
                if url.path.startswith("/api/ultrafast/"):
                    from . import ultrafast

                    if url.path == "/api/ultrafast/config":
                        return self.send(200, json.dumps(ultrafast.configuration()))
                    if url.path == "/api/ultrafast/runs":
                        return self.send(200, json.dumps(ultrafast.listing(store)))
                    if url.path == "/api/ultrafast/run":
                        return self.send(200, json.dumps(ultrafast.data(store, run_id)))
                    if url.path == "/api/ultrafast/decision":
                        return self.send(200, json.dumps(ultrafast.decision_data(
                            store, run_id, args.get("index", [""])[0])))
                    if url.path == "/api/ultrafast/image":
                        path = ultrafast.image_path(store, run_id, args.get("frame", ["latest"])[0])
                        return self.send(200, path.read_bytes(), "image/jpeg")
                if url.path == "/api/studies":
                    from .research_ui import studies

                    return self.send(200, json.dumps(studies(store)))
                if url.path == "/api/study":
                    from .research_ui import study_data

                    return self.send(200, json.dumps(study_data(store, run_id)))
                if url.path == "/api/launcher":
                    return self.send(200, json.dumps(store.launch_status()))
                if url.path == "/api/live":
                    return self.send(200, json.dumps(store.live(run_id)))
                if url.path == "/api/run":
                    path = store.path(run_id)
                    versions = []
                    for name in ("trajectory.jsonl", "report.json", "result.json", "trace.zip", "frames.jsonl",
                                 "manifest.json", "task.json", "process-scores.jsonl"):
                        file = store.file(path, name)
                        versions.append(str(file.stat().st_mtime_ns) if file.exists() else "0")
                    etag = '"' + "-".join(versions) + '"'
                    if self.headers.get("If-None-Match") == etag:
                        return self.send(304, b"", etag=etag)
                    return self.send(200, json.dumps(store.data(run_id)), etag=etag)
                if url.path == "/api/image":
                    content, mime = store.image(run_id, args.get("frame", ["final"])[0])
                    return self.send(200, content, mime)
                if url.path == "/api/download":
                    name = args.get("file", ["trajectory.jsonl"])[0]
                    if name not in {"trajectory.jsonl", "report.json", "trace.zip"}:
                        raise ValueError("文件不可下载")
                    return self.send(
                        200,
                        store.file(store.path(run_id), name).read_bytes(),
                        "application/zip" if name.endswith(".zip") else "application/json",
                    )
                files = {
                    "/": ("studio.html", "text/html"),
                    "/ultrafast": ("studio.html", "text/html"),
                    "/research": ("studio.html", "text/html"),
                    "/views/longseq": ("index.html", "text/html"),
                    "/views/ultrafast": ("original.html", "text/html"),
                    "/views/research": ("research.html", "text/html"),
                    "/studio.js": ("studio.js", "text/javascript"),
                    "/studio.css": ("studio.css", "text/css"),
                    "/studio-view.css": ("studio-view.css", "text/css"),
                    "/original.js": ("original.js", "text/javascript"),
                    "/benchmark-ui.js": ("benchmark-ui.js", "text/javascript"),
                    "/original.css": ("original.css", "text/css"),
                    "/research.js": ("research.js", "text/javascript"),
                    "/research.css": ("research.css", "text/css"),
                    "/app.js": ("app.js", "text/javascript"),
                    "/style.css": ("style.css", "text/css"),
                    "/ultrafast.css": ("ultrafast.css", "text/css"),
                }
                if url.path not in files:
                    return self.send(404, "Not found", "text/plain")
                name, mime = files[url.path]
                content = (STATIC / name).read_text()
                if name == "studio.html":
                    view = {"/": "index.html", "/ultrafast": "original.html", "/research": "research.html"}[url.path]
                    template = (STATIC / view).read_text()
                    # Inert template: each view mounts once under the shared navigation.
                    content = content.replace("__VIEW_PATH__", url.path).replace("__VIEW_CONTENT__", template)
                self.send(
                    200,
                    content.replace("__TOKEN__", token),
                    mime + "; charset=utf-8",
                )
            except (ValueError, OSError, KeyError, zipfile.BadZipFile):
                self.send(404, json.dumps({"error": "运行或资源暂不可用"}))

        def do_POST(self):
            origin = f"http://127.0.0.1:{self.server.server_port}"
            if (
                not self.local()
                or self.headers.get("X-Demo-Token") != token
                or self.headers.get("Origin") not in (None, origin)
            ):
                return self.send(403, json.dumps({"error": "请刷新本地页面后重试"}))
            if self.path not in {"/api/demo", "/api/launch", "/api/research",
                                 "/api/ultrafast/launch", "/api/ultrafast/stop"}:
                return self.send(404, json.dumps({"error": "未知操作"}))
            try:
                if self.path in {"/api/launch", "/api/research",
                                 "/api/ultrafast/launch", "/api/ultrafast/stop"}:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 40000:
                        raise ValueError("请求内容为空或过长")
                    body = json.loads(self.rfile.read(length))
                    if self.path == "/api/ultrafast/stop":
                        from .ultrafast import stop

                        result = stop(store, body)
                    elif self.path == "/api/ultrafast/launch":
                        from .ultrafast import launch

                        result = launch(store, body)
                    elif self.path == "/api/research":
                        from .research_ui import launch_research

                        result = launch_research(store, body)
                    else:
                        result = store.launch_prompt(body)
                else:
                    result = store.launch_demo()
                self.send(200, json.dumps(result))
            except (ValueError, UnicodeError) as exc:
                self.send(400, json.dumps({"error": str(exc)}))
            except RuntimeError as exc:
                self.send(409, json.dumps({"error": str(exc)}))
            except OSError:
                self.send(500, json.dumps({"error": "任务进程启动失败，请检查本地环境"}))

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.store = store
    return server


def main():
    parser = argparse.ArgumentParser(description="Jev LongSeq trace inspector")
    parser.add_argument("--runs", default="runs")
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--env-file", help="Model configuration; credentials stay server-side")
    parser.add_argument("--ultrafast-root", help="Optional original jev-ultrafast checkout")
    args = parser.parse_args()
    if args.ultrafast_root:
        os.environ["JEV_ULTRAFAST_ROOT"] = str(Path(args.ultrafast_root).expanduser().resolve())
    if args.env_file:
        from .config import load_env_file

        load_env_file(args.env_file)
    server = make_server(args.runs, args.port)
    print(f"Jev LongSeq Trace: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        # Only tasks launched from this inspector are owned by it.
        if server.store.child and server.store.child.poll() is None:
            server.store.child.terminate()
            server.store.child.wait(timeout=10)


if __name__ == "__main__":
    main()
