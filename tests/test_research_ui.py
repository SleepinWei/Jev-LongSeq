import re
import threading
from unittest.mock import Mock

import httpx
import pytest

from jev_browser.inspector import Store, make_server
from jev_browser.observability import Observer, write_json
from jev_browser.research_ui import launch_research, studies, study_data


def study(root):
    path = root / "study-one"
    path.mkdir()
    write_json(
        path / "study.json",
        {"status": "blocked", "phase": "preflight", "trials": [], "config": {"suite": "webarena"}},
    )
    return path


def test_research_projection_has_live_usage_and_no_path_escape(tmp_path):
    root = study(tmp_path)
    observer = Observer(root / "researcher")
    observer.model_call(
        {
            "kind": "research_analysis",
            "transport": "codex_cli",
            "input_tokens": 12,
            "output_tokens": 5,
            "cost_usd": None,
        }
    )
    store = Store(tmp_path)
    value = study_data(store, "study-one")
    assert value["research_usage"]["codex_invocations"] == 1
    assert value["research_usage"]["cost_usd"] is None
    assert studies(store)[0]["status"] == "blocked"
    with pytest.raises(ValueError):
        study_data(store, "../study-one")
    (root / "budget.json").symlink_to(tmp_path.parent / "outside.json")
    with pytest.raises(ValueError):
        study_data(store, "study-one")


def test_launch_is_bounded_and_keeps_ds_jev_separate_from_codex(tmp_path, monkeypatch):
    child = Mock()
    child.poll.return_value = None
    spawn = Mock(return_value=child)
    monkeypatch.setattr("jev_browser.research_ui.subprocess.Popen", spawn)
    store = Store(tmp_path)
    result = launch_research(store, {"mode": "new"})
    command = spawn.call_args.args[0]
    assert command[command.index("--brain") + 1] == "api"
    assert command[command.index("--task-ids") + 1 : command.index("--task-ids") + 3] == [
        "50",
        "332",
    ]
    assert command[command.index("--max-trials") + 1] == "2"
    assert command[command.index("--codex-effort") + 1] == "high"
    assert not (tmp_path / result["id"]).exists()  # runner creates an empty study directory
    with pytest.raises(RuntimeError):
        launch_research(store, {})


def test_research_http_routes_preserve_local_post_protection(tmp_path, monkeypatch):
    study(tmp_path)
    server = make_server(tmp_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(
        "jev_browser.research_ui.launch_research", lambda store, body: {"id": "test-only"}
    )
    try:
        with httpx.Client(base_url=base, trust_env=False) as client:
            page = client.get("/research")
            assert page.status_code == 200 and "每次改进" in page.text
            token = re.search(r'name="demo-token" content="([^"]+)"', page.text)[1]
            assert client.get("/research.js").status_code == 200
            assert client.get("/api/studies").json()[0]["id"] == "study-one"
            assert (
                client.get("/api/study", params={"id": "study-one"}).json()["status"] == "blocked"
            )
            assert client.post("/api/research", json={}).status_code == 403
            assert (
                client.post(
                    "/api/research",
                    json={},
                    headers={"X-Demo-Token": token, "Origin": "https://other.test"},
                ).status_code
                == 403
            )
            assert (
                client.post("/api/research", json={}, headers={"X-Demo-Token": token}).json()["id"]
                == "test-only"
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_source_snapshot_pins_study_code_outside_trial_output(tmp_path):
    from pathlib import Path

    from jev_browser.research_ui import frozen_source
    first = frozen_source(tmp_path)
    assert (first / 'jev_browser' / '__main__.py').exists()
    assert first == frozen_source(tmp_path)
    import jev_browser.research_ui as module
    assert (first / 'jev_browser' / 'research_ui.py').read_text() == Path(module.__file__).read_text()
