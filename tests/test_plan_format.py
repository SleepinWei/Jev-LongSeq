"""A redundant format marker must not cause a second, different action plan."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from jev_browser.dynamic import (
    DynamicController,
    Feedback,
    FinishReview,
    InvalidFeedbackOutput,
    JsonFeedback,
    StageGuidance,
    validated_feedback,
)
from jev_browser.models import ModelTransport
from jev_browser.protocol import Element, Observation, Task


def plan():
    return {"type": "json_object", "next_goal": "Fill User, then observe fresh options",
            "working_memory": "Derived field remains unresolved; no Save",
            "stage_controls": [{"element_ref": "user", "operations": ["fill"]}],
            "stage_entry": {"intent": "act", "operation": "fill", "element_ref": "user"},
            "inputs": [{"name": "User", "value": "Rajesh Kumar", "grid_ref": "activities", "row_ref": "1"}]}


@pytest.mark.parametrize("schema", [Feedback, StageGuidance])
def test_exact_top_level_format_marker_is_removed_without_changing_plan(schema):
    raw = plan()
    expected = schema.model_validate({k: v for k, v in raw.items() if k != "type"})
    result = validated_feedback(json.dumps(raw), schema)
    assert all(result.model_dump()[key] == value for key, value in expected.model_dump().items())
    assert result._format_diagnostics == [{"loc": ["type"], "type": "redundant_json_format_marker_removed"}]
    assert raw["type"] == "json_object"


@pytest.mark.parametrize("case", ["other_marker", "other_extra", "nested_extra", "completion", "finish"])
def test_unknown_extras_action_fields_and_completion_remain_strict(case):
    raw, schema = plan(), StageGuidance
    if case == "other_marker":
        raw["type"] = "save_anyway"
    elif case == "other_extra":
        raw["override_required"] = True
    elif case == "nested_extra":
        raw["stage_entry"]["type"] = "json_object"
    elif case == "completion":
        schema = Feedback
        raw["complete"] = True
    else:
        schema = FinishReview
        raw = {"type": "json_object", "next_goal": "Verify"}
    with pytest.raises(ValidationError):
        validated_feedback(json.dumps(raw), schema)


@pytest.mark.parametrize("operation", ["fill", "click", "select"])
async def test_actual_model_response_skips_repair_only_for_valid_action(operation):
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        raw = plan()
        raw["stage_entry"]["operation"] = operation
        raw["stage_controls"][0]["operations"] = [operation]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(raw)}}]})

    task = Task(id="format", sandbox=True, control_mode="dynamic", objective="Assign Rajesh Kumar")
    obs = Observation(observation_id="now", document_version="v1", tab_id="tab", url="about:blank",
        title="Draft", text="Draft", elements=[Element(id="user", role="combobox", name="User", editable=True,
                                        grid_ref="activities", row_ref="1")])
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        brain = JsonFeedback(ModelTransport("https://model.test", "test-key", "test", client=client))
        controller = DynamicController(task, AsyncMock(), AsyncMock(), feedback=brain)
        if operation in {"fill", "click"}:
            result = await controller.review(obs, phase="ui_checkpoint")
            assert result.stage_entry.operation == operation and len(calls) == 1
            assert result.inputs[0].value == "Rajesh Kumar"
            assert any(e["kind"] == "feedback_metadata_normalized" for e in controller.events)
            assert not any(e["kind"] == "invalid_feedback" for e in controller.events)
        else:
            with pytest.raises(InvalidFeedbackOutput):
                await controller.review(obs, phase="ui_checkpoint")
            assert len(calls) == 2
            diagnostic = json.loads(calls[-1]["messages"][-1]["content"])["schema_error"][0]
            assert diagnostic["type"] == "control_operation_unavailable"
            assert diagnostic["requested_operations"] == ["select"]
            assert diagnostic["available_operations"] == ["click", "fill"]
        controller.backend.execute.assert_not_awaited()
        assert not controller.pending and not controller.memory.pending_writes
