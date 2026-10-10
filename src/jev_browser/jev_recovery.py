"""Bound repeated interventions by observed work state, not planner prose/evidence IDs."""

from .protocol import digest


def work_signature(obs):
    """Popup toggling and DOM handle churn are information, not new task progress.

    Novel field values, visible grid contents or routes start a new allowance.
    Returning to an old state retains its allowance, so A/B oscillation cannot reset it.
    This is a local scheduling signal, never proof of a saved business record.
    """
    grids = {g.id: g.name for g in obs.grids}
    fields = sorted((e.role, e.name, e.value, str(e.checked), grids.get(e.grid_ref, ""), e.row_ref or "")
                    for e in obs.elements if e.editable or e.selectable or e.read_only
                    or e.role in {"checkbox", "radio"})
    rows = [(g.name, [(r.key, [(c.column, c.value) for c in r.cells]) for r in g.rows])
            for g in obs.grids]
    return digest([obs.tab_id, obs.url, fields, rows])


def probe_key(action, obs):
    element = next((e for e in obs.elements if e.id == action.element_ref), None)
    owner = next((e for e in obs.elements if element and e.id == element.option_owner), None)
    def identity(e):
        return ([e.role, e.name, e.value, e.context, e.grid_ref, e.row_ref,
                 e.editable, e.selectable, e.read_only, e.popup_kind, e.popup_open] if e else None)
    return digest([work_signature(obs), action.operation, action.bound_value,
                   identity(element), identity(owner)])


class StallGuard:
    def __init__(self, interventions, probes):
        self.intervention_limit = interventions
        self.probe_limit = probes
        self.states = {}
        self.last_choice = {}

    def current(self, obs):
        key = work_signature(obs)
        # Bounded storage; old states fall out only after 128 distinct work states.
        if key not in self.states:
            if len(self.states) >= 128:
                self.states.pop(next(iter(self.states)))
            self.states[key] = {"interventions": 0, "probes": 0, "tried": set()}
        return self.states[key]

    def context(self, obs):
        record = self.current(obs)
        return {"work_state": work_signature(obs),
                "interventions": record["interventions"], "intervention_limit": self.intervention_limit,
                "probes": record["probes"], "probe_limit": self.probe_limit,
                "last_choice": self.last_choice,
                "scope": "Local work-state changes only; DS notes/new evidence IDs are not progress. "
                         "Popup exploration does not reset this allowance or confirm business persistence."}

    def note_choice(self, obs, decision, candidates):
        descriptions = {a.id: {"operation": a.operation.value, "description": a.description[:300]}
                        for a in candidates}
        ranking = sorted(decision.probabilities.items(), key=lambda x: x[1], reverse=True)[:4]
        self.last_choice = {"work_state": work_signature(obs), "choice": decision.choice,
                            "confidence": decision.confidence,
                            "alternatives": [{"choice": key, "probability": value,
                                              **descriptions[key]} for key, value in ranking if key in descriptions],
                            "meaning": "Reported choice probabilities are ambiguity hints, not permissions."}
