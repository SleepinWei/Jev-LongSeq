import copy
from types import SimpleNamespace

from jev_browser.ultrafast_history import HistoryClient, execution_history, history_agent


def test_memory_retains_earlier_observed_effects_and_reports_stall():
    history = [{"step": 1, "kind": "fill", "action": "Email", "progress_changed": True,
                "observed_result": {"value_before": "", "value_after": "Administrator", "page_url": "login"}},
               {"step": 2, "kind": "click", "action": "Login", "progress_changed": True,
                "observed_result": {"navigated": True, "page_url": "desk", "page_title": "Desk"}}]
    history += [{"step": i, "kind": "click", "action": "Button", "progress_changed": False}
                for i in range(3, 17)]
    memory = execution_history({"history": history, "page": {"url": "desk", "title": "Desk"}})
    assert memory["executed_steps"] == 16 and memory["consecutive_no_progress"] == 14
    assert memory["recent_action_results"][0]["step"] == 5
    assert memory["latest_observed_field_values"][0]["value"] == "Administrator"
    assert memory["observed_navigations"] == [{"step": 2, "url": "desk", "title": "Desk"}]
    assert "complete" not in memory  # Executed Login is not an assertion of task completion.
    history[0]["observed_result"]["value_after"] = "changed later"
    assert memory["latest_observed_field_values"][0]["value"] == "Administrator"


def test_memory_scopes_to_current_turn_and_does_not_call_unknown_progress_success():
    memory = execution_history({"history": [{"step": 1, "progress_changed": True},
                                             {"step": 2, "kind": "click"}],
                                "turns": [{"action_start": 1}]})
    assert memory["executed_steps"] == 1
    assert memory["recent_action_results"][0]["progress"] == "unknown"


def test_client_records_actual_context_without_changing_goals_choices_or_text_helper():
    requests = []
    client = HistoryClient(SimpleNamespace(post=lambda url, **kwargs: requests.append(kwargs["json"])))
    client.state = {"history": []}
    questions = {"operation": {"instructions": {"goal": "Original task"}, "criteria": {"CLICK": "Click"}}}
    body = {"questions": copy.deepcopy(questions), "state": {"recent_actions": []}}
    client.post("jev", json=body)
    assert body["questions"] == questions and body["state"]["recent_actions"] == []
    assert requests[0]["state"]["execution_history"]["executed_steps"] == 0
    helper = {"messages": [{"role": "user", "content": "original field context"}]}
    client.post("helper", json=helper)
    assert requests[-1] == helper and "state" not in helper


def test_agent_captures_actual_field_values_and_masks_password_observations():
    class Base:
        def __init__(self, field_id="login_email"):
            self.state = {"history": [], "page": {"url": "login", "title": "Login", "actions": [
                {"id": "e1", "node": 1, "kind": "fill", "label": "Field", "value": "",
                 "field_hint": {"input_id": field_id}}]}, "decision": {"choice": "e1"}}

        def command(self, name, body=None):
            self.state["page"] = copy.deepcopy(self.state["page"])
            self.state["page"]["actions"][0]["value"] = "test-value"
            self.state["history"].append({"kind": "fill", "action": "Field", "progress_changed": True})
            return {"history": self.state["history"]}

    client = HistoryClient(None)
    agent = history_agent(Base, client)()
    snapshot = agent.command("act")
    outcome = snapshot["history"][0]["observed_result"]
    assert outcome["value_before"] == "" and outcome["value_after"] == "test-value"
    assert outcome["navigated"] is False and client.state is agent.state
    secret_agent = history_agent(Base, client)("login_password")
    secret_agent.command("act")
    outcome = secret_agent.state["history"][0]["observed_result"]
    assert outcome["value_before"] == outcome["value_after"] == "[redacted]"
