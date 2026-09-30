"""Observed execution history for a controlled Ultrafast context experiment."""
from __future__ import annotations

VERSION = "observed-history-v1"


def execution_history(state):
    turn = state.get("turns", [{}])[-1]
    history = state.get("history", [])[turn.get("action_start", 0):]
    rows, fields, navigations = [], {}, []
    stalled = 0
    for entry in history:
        observation = entry.get("observed_result", {})
        changed = entry.get("progress_changed")
        stalled = stalled + 1 if changed is False and entry.get("kind") != "wait" else 0
        row = {"step": entry.get("step"), "operation": entry.get("operation", entry.get("kind")),
               "target": entry.get("action"), "execution": "executed",
               "progress": "changed" if changed is True else "unchanged" if changed is False else "unknown",
               **observation}
        rows.append(row)
        if entry.get("kind") == "fill" and observation.get("value_after") is not None:
            key = (observation.get("page_url"), entry.get("action"))
            fields[key] = {"field": entry.get("action"), "value": observation["value_after"],
                           "page_url": observation.get("page_url"), "last_observed_step": entry.get("step")}
        if observation.get("navigated"):
            navigations.append({"step": entry.get("step"), "url": observation.get("page_url"),
                                "title": observation.get("page_title")})
    page = state.get("page", {})
    return {"version": VERSION, "executed_steps": len(history),
            "current_page": {"url": page.get("url"), "title": page.get("title")},
            "latest_observed_field_values": list(fields.values()),
            "observed_navigations": navigations,
            "recent_action_results": rows[-12:], "consecutive_no_progress": stalled,
            "interpretation": (
                "These are observations of executed actions, not assertions that task requirements are complete. "
                "Use current page values over historical values. A field already holding the requested value "
                "does not need another identical fill. Unchanged results mean the action made no observed progress. "
                "Use this record to identify what has already happened and what still needs visible confirmation."
            )}


class HistoryClient:
    """Inject only observed state into Jev requests; preserve choices and text helper."""

    def __init__(self, client):
        self.client = client
        self.state = {}

    def post(self, url, **kwargs):
        body = kwargs.get("json", {})
        if "questions" in body:
            # Mutate this per-call body so the native decision.request records
            # exactly what was sent. Each memory is a new immutable-by-use value.
            body.setdefault("state", {})["execution_history"] = execution_history(self.state)
        return self.client.post(url, **kwargs)


def history_agent(base_agent, client):
    class HistoryAgent(base_agent):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            client.state = self.state

        def command(self, name, body=None):
            before = self.state.get("page", {})
            count = len(self.state.get("history", []))
            decision = self.state.get("decision") or {}
            action = next((a for a in before.get("actions", [])
                           if a.get("id") == decision.get("choice")), {})
            try:
                return super().command(name, body)
            finally:
                if name == "act" and len(self.state.get("history", [])) > count:
                    after = self.state.get("page", {})
                    observation = {"page_url": after.get("url"), "page_title": after.get("title"),
                                   "navigated": before.get("url") != after.get("url")}
                    if action.get("kind") == "fill":
                        field_id = action.get("field_hint", {}).get("input_id")
                        updated = next((a for a in after.get("actions", []) if a.get("kind") == "fill"
                                        and ((field_id and a.get("field_hint", {}).get("input_id") == field_id)
                                             or (not field_id and a.get("node") == action.get("node")))), {})
                        hint = action.get("field_hint", {})
                        sensitive = "password" in " ".join(str(hint.get(k, "")) for k in
                                                           ("input_type", "input_id", "autocomplete")).lower()
                        observation.update(value_before="[redacted]" if sensitive else action.get("value"),
                                           value_after="[redacted]" if sensitive and updated else updated.get("value"))
                    self.state["history"][-1]["observed_result"] = observation

    return HistoryAgent
