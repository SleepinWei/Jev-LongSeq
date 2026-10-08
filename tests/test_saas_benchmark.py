import json
from types import SimpleNamespace

import pytest

from jev_browser import saas_benchmark as saas
from jev_browser.evaluation import summarize


def verification(passed=False):
    return {
        "status": "PASS" if passed else "FAIL",
        "all_pass": passed,
        "returncode": 0 if passed else 1,
        "score": int(passed),
        "earned": int(passed),
        "total": 1,
        "checks": [{"label": "hidden oracle", "passed": passed, "weight": 1}],
    }


@pytest.mark.parametrize("status", ["ERROR", "SKIP"])
def test_unavailable_verifier_is_ungraded(status):
    grade = saas.grade_result({"status": status, "score": 0})
    assert grade["strict_success"] is None and grade["data_valid"] is False


def test_partial_credit_and_nonzero_exit_cannot_be_strict_success():
    assert saas.grade_result(verification())["strict_success"] is False
    assert saas.grade_result(verification(True))["strict_success"] is True
    bad = {**verification(True), "returncode": 1}
    assert saas.grade_result(bad)["strict_success"] is False
    assert saas.grade_result({**bad, "returncode": -9})["data_valid"] is False


def test_sql_exception_disguised_as_fail_is_ungraded():
    result = verification()
    result["checks"][0]["detail"] = "exception: bigcapital mysql error: Unknown column 'PUBLISHED'"
    grade = saas.grade_result(result)
    assert grade["data_valid"] is False
    assert grade["strict_success"] is None
    assert grade["score"] == 0  # preserve raw upstream output for audit
    assert grade["verifier_errors"] == result["checks"]


@pytest.fixture
def environment(tmp_path, monkeypatch):
    root = tmp_path / "upstream"
    root.mkdir()
    verify_file = root / "verify.py"
    verify_file.write_text("# secret oracle, never agent input\n")
    task = {
        "task_id": "business_023",
        "description_md": "Create the requested records",
        "meta": {"meta_data": {"sites": ["hrms", "twenty"]}},
        "verify_py_path": str(verify_file),
    }
    events = []

    class Slot:
        def __init__(self, apps, slot_id):
            self.apps = apps

        def get_port_map(self, sites):
            return {site: 31000 + i for i, site in enumerate(sites)}

        def start_apps(self, sites, hostname):
            events.append("start")

        def stop_apps(self, sites):
            events.append("stop")

    def verify(*args):
        events.append("verify")
        return verification(True)

    monkeypatch.setattr(saas, "checkout", lambda args: root)
    monkeypatch.setattr(saas, "selected_tasks", lambda args, root: [task])
    monkeypatch.setattr(saas, "configuration", lambda root: {
        "hrms": {"startup_wait": 600}, "twenty": {"startup_wait": 360},
    })
    monkeypatch.setattr(saas, "images_for", lambda root, apps, sites: ["fixture:latest"])
    monkeypatch.setattr(saas, "command", lambda *args: SimpleNamespace(stdout="revision"))
    monkeypatch.setattr(
        saas,
        "upstream",
        lambda root: (
            SimpleNamespace(
                build_prompt=lambda task, ports, host: (task["description_md"], "", [])
            ),
            SimpleNamespace(SlotManager=Slot, _SLOT_PREFIX="jevsaas"),
            SimpleNamespace(run_verify=verify),
        ),
    )
    args = SimpleNamespace(saas_slot=0, environment_only=False)

    async def agent(args, *, output):
        events.append("agent")
        assert not list(output.iterdir())  # Match the real LongSeq output guard.
        assert "secret oracle" not in args._benchmark_task.objective
        assert not hasattr(args, "verify_py_path")
        assert args._benchmark_urls == ["http://localhost:31001"]
        report = saas.empty_report(
            args._benchmark_task, args._benchmark_manifest, "success", "completed"
        )
        report["result"].update(status="success")
        return report

    monkeypatch.setattr("jev_browser.cli.run_trial", agent)
    return args, events, Slot, tmp_path / "output"


async def test_agent_then_official_verifier_then_cleanup_and_durable_report(environment):
    args, events, _, output = environment
    report = await saas.run_saas(args, {"id": "business_023"}, output)
    assert events == ["start", "agent", "verify", "stop"]
    assert report["result"]["strict_success"] is True
    assert summarize([report])["strict_successes"] == 1
    assert json.loads((output / "report.json").read_text())["grade"]["score"] == 1


async def test_original_agent_uses_same_official_grading_and_cleanup(environment, monkeypatch):
    args, events, _, output = environment
    args.saas_agent = "jev-ultrafast"

    async def original(args, task, manifest, output):
        events.append("original")
        assert "secret oracle" not in task.objective
        report = saas.empty_report(task, manifest, "jev-ultrafast-original", "done")
        report["result"].update(status="success", finish_requests=1)
        return report

    monkeypatch.setattr("jev_browser.saas_ultrafast.run_original", original)
    report = await saas.run_saas(args, {"id": "business_023"}, output)
    assert events == ["start", "original", "verify", "stop"]
    assert report["result"]["strict_success"] is True


async def test_business_031_uses_versioned_patch_and_records_effective_hash(environment, monkeypatch):
    from pathlib import Path

    from jev_browser.protocol import digest
    from jev_browser.saas_verifier import BUSINESS_031_PATCH

    args, events, _, output = environment
    task = saas.selected_tasks(args, saas.checkout(args))[0]
    upstream = Path(__file__).parent / "fixtures/saas_bench/business_031_verify.py"
    task.update(task_id="business_031", verify_py_path=str(upstream))
    original = upstream.read_text()
    loader, slots, _ = saas.upstream(saas.checkout(args))

    def verify(effective_task, *args):
        events.append("verify")
        assert effective_task is not task
        source = Path(effective_task["verify_py_path"]).read_text()
        assert "(PUBLISHED_AT IS NOT NULL) AS PUBLISHED" in source
        assert "OR LOWER(DISPLAY_NAME)" in source
        return verification(True)

    monkeypatch.setattr(saas, "upstream", lambda root: (loader, slots, SimpleNamespace(run_verify=verify)))
    report = await saas.run_saas(args, {"id": "business_031"}, output)
    assert events == ["start", "agent", "verify", "stop"]
    assert upstream.read_text() == original and task["verify_py_path"] == str(upstream)
    manifest = report["manifest"]
    assert manifest["verifier_patch"] == BUSINESS_031_PATCH
    assert manifest["upstream_verifier_hash"] == digest(original)
    assert manifest["verifier_hash"] == digest((output / "verifier.compat.py").read_text())
    assert manifest["upstream_verifier_hash"] != manifest["verifier_hash"]
    assert report["grade"]["verifier_patch"] == BUSINESS_031_PATCH


async def test_partial_start_failure_still_cleans_owned_containers(environment):
    args, events, slot, output = environment

    def fail(self, sites, hostname):
        events.append("start")
        raise RuntimeError("readiness failed")

    slot.start_apps = fail
    report = await saas.run_saas(args, {"id": "business_023"}, output)
    assert events == ["start", "stop"]
    assert report["result"]["strict_success"] is None
    assert report["grade"]["data_valid"] is False
    assert summarize([report])["graded_runs"] == 0
    assert report["environment"]["setup_s"] > 0


async def test_v11_uses_official_oracle_without_compatibility_copy(environment, monkeypatch):
    from pathlib import Path

    args, events, _, output = environment
    task = saas.selected_tasks(args, saas.checkout(args))[0]
    official = Path(__file__).parent / "fixtures/saas_bench/business_031_v11_verify.py"
    task.update(task_id="business_031", verify_py_path=str(official))
    loader, slots, _ = saas.upstream(saas.checkout(args))

    def verify(effective_task, *args):
        events.append("verify")
        assert effective_task is task
        assert effective_task["verify_py_path"] == str(official)
        return verification(True)

    monkeypatch.setattr(saas, "upstream", lambda root: (loader, slots, SimpleNamespace(run_verify=verify)))
    monkeypatch.setattr(saas, "command", lambda *args: SimpleNamespace(
        stdout="aaa042140b585f80ad531bc8c9af296581426873"))
    report = await saas.run_saas(args, {"id": "business_031"}, output)
    assert events == ["start", "agent", "verify", "stop"]
    manifest = report["manifest"]
    assert manifest["benchmark_version"] == "v1.1"
    assert manifest["verifier_patch"] is None
    assert manifest["verifier_hash"] == manifest["upstream_verifier_hash"]
    assert report["grade"]["source"] == "SaaS-Bench official verify.py"
    assert not (output / "verifier.compat.py").exists()


def test_startup_configuration_isolated_selected_override():
    apps = {"twenty": {"startup_wait": 360}, "hrms": {"startup_wait": 600}, "other": {}}
    default, waits = saas.startup_configuration(apps, ["twenty"])
    assert default == apps and default is not apps
    assert waits["apps"]["twenty"] == {"original_s": 360, "effective_s": 360}
    effective, waits = saas.startup_configuration(apps, ["twenty"], 900)
    assert effective["twenty"]["startup_wait"] == 900
    assert effective["hrms"] == apps["hrms"]
    assert apps["twenty"]["startup_wait"] == 360
    assert waits["timeout_override_s"] == 900
    _, implicit = saas.startup_configuration(apps, ["other"])
    assert implicit["apps"]["other"]["effective_s"] == 600


@pytest.mark.parametrize("timeout", [0, -1, 1.5, True])
def test_invalid_startup_timeout(timeout):
    with pytest.raises(ValueError, match="positive integer"):
        saas.startup_configuration({}, [], timeout)


async def test_startup_override_preserves_fixture_task_and_agent_budget(environment, monkeypatch):
    args, _, slot, output = environment
    args.max_seconds = 1800
    original = await saas.run_saas(args, {"id": "business_023"}, output)
    args.saas_startup_timeout = 900

    def start(self, sites, hostname):
        assert all(self.apps[site]["startup_wait"] == 900 for site in sites)

    slot.start_apps = start
    report = await saas.run_saas(args, {"id": "business_023"}, output.parent / "override")
    assert args.max_seconds == 1800
    for key in ("task_hash", "fixture_hash", "verifier_hash"):
        assert report["manifest"][key] == original["manifest"][key]
    assert report["environment"]["startup"] == report["manifest"]["startup"]
    assert report["manifest"]["startup"]["apps"]["twenty"] == {
        "original_s": 360, "effective_s": 900,
    }


async def test_environment_smoke_is_not_an_agent_score(environment):
    args, events, _, output = environment
    args.environment_only = True
    report = await saas.run_saas(args, {"id": "business_023"}, output)
    assert events == ["start", "verify", "stop"]
    assert report["result"]["strict_success"] is None
    assert report["model_calls"] == []
    assert summarize([report])["strict_success_rate"] is None


async def test_busy_slot_never_starts_or_cleans_another_run(environment, monkeypatch):
    args, events, _, output = environment

    def busy(*args):
        raise BlockingIOError("slot busy")

    monkeypatch.setattr(saas.fcntl, "flock", busy)
    report = await saas.run_saas(args, {"id": "business_023"}, output)
    assert events == []
    assert report["grade"]["data_valid"] is False


async def test_missing_checkout_blocks_before_models(tmp_path):
    args = SimpleNamespace(saas_root="", saas_slot=0)
    report = await saas.check_saas(args)
    assert report["status"] == "blocked"
    assert report["errors"] and not report["selected_tasks"]


async def test_paired_saas_analysis_accepts_string_ids_and_rejects_path_escape(environment):
    from jev_browser.observability import save_analysis
    from jev_browser.research_benchmark import aggregate

    args, _, _, output = environment
    report = await saas.run_saas(args, {"id": "business_023"}, output)
    combined = aggregate([report, report])
    assert len(save_analysis(output.parent, combined)["task_observations"]) == 2
    report["manifest"]["task_id"] = "../../outside"
    with pytest.raises(ValueError, match="invalid SaaS-Bench"):
        save_analysis(output.parent, combined)


def test_ui_launches_saas_pair_with_existing_budget(tmp_path, monkeypatch):
    from unittest.mock import Mock

    from jev_browser.inspector import Store
    from jev_browser.research_ui import launch_research

    spawn = Mock()
    monkeypatch.setattr("jev_browser.research_ui.subprocess.Popen", spawn)
    launch_research(Store(tmp_path), {"suite": "saas-bench"})
    cmd = spawn.call_args.args[0]
    assert cmd[cmd.index("--suite") + 1] == "saas-bench"
    assert cmd[cmd.index("--saas-task-ids") + 1 :] == saas.DEFAULT_TASKS
    assert cmd[cmd.index("--max-tokens") + 1] == "600000"
