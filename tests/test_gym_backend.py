import ast

import pytest

from jev_browser.config import load_env_file, use_text_model_for_planner
from jev_browser.controller import Controller
from jev_browser.fixture import RulePlanner, RulePolicy, demo_task
from jev_browser.protocol import Action, Operation


def test_dotenv_no_shell_execution(tmp_path, monkeypatch):
    path = tmp_path / "settings.env"
    path.write_text(
        'JEV_TEST_VALUE="$(touch /tmp/should-not-exist)"\nTEXT_MODEL_BASE_URL=https://example.test/v1\nTEXT_MODEL=test\nTEXT_MODEL_API_KEY=test-secret\n'
    )
    for key in (
        "JEV_TEST_VALUE",
        "TEXT_MODEL_BASE_URL",
        "TEXT_MODEL",
        "TEXT_MODEL_API_KEY",
        "PLANNER_ENDPOINT",
        "PLANNER_MODEL",
        "PLANNER_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    load_env_file(str(path))
    use_text_model_for_planner()
    import os

    assert os.environ["JEV_TEST_VALUE"] == "$(touch /tmp/should-not-exist)"
    assert os.environ["PLANNER_ENDPOINT"] == "https://example.test/v1/chat/completions"


def test_action_serialization_cannot_inject_code():
    from jev_browser.gym_backend import action_string

    value = "'); import os; os.remove('file'); #"
    action = Action(
        id="a0",
        operation=Operation.FILL,
        observation_id="o",
        document_version="v",
        tab_id="tab-0",
        element_ref="12",
        bound_value=value,
    )
    tree = ast.parse(action_string(action))
    assert len(tree.body) == 1
    assert tree.body[0].value.func.id == "fill"
    assert tree.body[0].value.args[1].value == value


async def test_two_gym_episodes_grade_only_after_stop(tmp_path):
    pytest.importorskip("browsergym.core")
    from jev_browser.gym_backend import BrowserGymBackend

    for episode in range(2):
        task = demo_task(1)
        async with BrowserGymBackend(task, records=1, output=tmp_path / str(episode)) as backend:
            controller = Controller(task, backend, RulePolicy(), planner=RulePlanner())
            result = await controller.run()
            assert result.status == "success", result.reason
            assert backend.reward == 0
            assert not backend.grade_info
            grade = await backend.finish("Done")
            assert grade["reward"] == 1
            assert grade["info"]["grade"]["duplicates"] == 0
            assert grade["terminated"]
        assert (tmp_path / str(episode) / "trace.zip").exists()


def test_public_preflight_does_not_expose_reference_answers(monkeypatch):
    pytest.importorskip("browsergym.core")
    from jev_browser.benchmark import webarena_preflight

    monkeypatch.delenv("WA_SHOPPING", raising=False)
    report = webarena_preflight([50, 332])
    assert "WA_SHOPPING" in report["missing_environment"]
    assert all(set(t) == {"id", "intent", "sites"} for t in report["selected_tasks"])
    with pytest.raises(ValueError, match="one or two"):
        webarena_preflight([47, 48, 49])
