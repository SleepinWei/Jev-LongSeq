import pytest
from pydantic import ValidationError

from jev_browser.candidates import allowed_url, generate, validate_plan
from jev_browser.fixture import demo_task
from jev_browser.memory import Memory, check
from jev_browser.protocol import Binding, Contract, Element, Observation, Operation, Plan, Predicate


def observation(text="", **kwargs):
    return Observation(
        observation_id="o1",
        document_version="v1",
        tab_id="tab-0",
        url="about:blank",
        title="test",
        text=text,
        **kwargs,
    )


def contract(key="a", dependencies=None):
    return Contract(
        id=key,
        objective="read",
        depends_on=dependencies or [],
        allowed_operations=[Operation.EXTRACT],
        success_predicates=[Predicate(kind="text", value="complete")],
    )


@pytest.mark.parametrize(
    "data",
    [
        {"kind": "python", "value": "__import__('os').system('x')"},
        {"kind": "all"},
        {"kind": "fact", "entity": "e"},
        {"kind": "not"},
        {"kind": "text", "value": "x", "script": "alert(1)"},
    ],
)
def test_dsl_rejects_code_and_malformed_predicates(data):
    with pytest.raises(ValidationError):
        Predicate.model_validate(data)


@pytest.mark.parametrize(
    "contracts",
    [
        [contract("a", ["b"]), contract("b", ["a"])],
        [contract("a", ["missing"])],
        [contract(), contract()],
    ],
)
def test_invalid_dags_rejected(contracts):
    with pytest.raises(ValidationError):
        Plan(subtasks=contracts)


def test_fact_provenance_conflicts_and_decimal_comparison():
    memory, task = Memory(), demo_task(1)
    obs = observation("Entity: item-001\nPrice: 59.99\nRating: 4\nSaved: no")
    memory.observe(obs)
    memory.extract(obs, task.extraction)
    assert check(
        Predicate(kind="fact", entity="item-001", field="Price", op="lt", value="60"), memory, obs
    )
    assert memory.facts["item-001"]["Price"].source.quote == "Price: 59.99"
    other = obs.model_copy(
        update={"url": "https://different.example", "text": obs.text.replace("59.99", "10")}
    )
    memory.observe(other)
    memory.extract(other, task.extraction)
    assert memory.get("item-001", "Price") is None
    assert len(memory.conflicts) == 1


def test_ambiguous_fields_are_not_extracted():
    task, memory = demo_task(1), Memory()
    obs = observation("Entity: item-001\nPrice: 10\nPrice: 20")
    memory.observe(obs)
    memory.extract(obs, task.extraction)
    assert memory.get("item-001", "Price") is None


def test_plan_cannot_forge_bindings_or_expand_permissions():
    task, memory = demo_task(1), Memory()
    c = contract()
    c.bindings = [Binding(name="key", value="invented", source="user")]
    with pytest.raises(ValueError, match="invented"):
        validate_plan(Plan(subtasks=[c]), task, memory)
    c.bindings = [Binding(name="key", value="invented", source="fact", entity="e", field="f")]
    with pytest.raises(ValueError, match="observed"):
        validate_plan(Plan(subtasks=[c]), task, memory)
    task.allowed_operations = [Operation.WAIT]
    c.bindings = []
    with pytest.raises(ValueError, match="expand"):
        validate_plan(Plan(subtasks=[c]), task, memory)


def test_candidates_keep_navigation_and_refuse_unauthorized_writes():
    task, memory = demo_task(1), Memory()
    elements = [
        Element(id=f"e{i}", role="button", name="Edit", context=f"item-{i:03}") for i in range(40)
    ]
    elements += [
        Element(id="next", role="button", name="Next"),
        Element(id="save", role="button", name="Save"),
    ]
    obs = observation("Entity: item-001", elements=elements)
    task.approved_writes = []
    candidates = generate(obs, task, memory, None, 16)
    assert len(candidates) == 16
    assert any(a.element_ref == "next" for a in candidates)
    assert not any(a.element_ref == "save" for a in candidates)
    assert any(a.operation == Operation.REPLAN for a in candidates)
    assert all(a.observation_id == "o1" for a in candidates)


def test_url_scope_is_exact_and_scheme_restricted():
    task = demo_task(1)
    task.allowed_origins = ["https://safe.example"]
    assert allowed_url("https://safe.example/path", task)
    for url in [
        "https://safe.example.evil/path",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "https://secret@safe.example/path",
    ]:
        assert not allowed_url(url, task)


def test_completed_contract_preserves_bound_final_answer():
    from jev_browser.controller import Controller

    task, memory = demo_task(1), Memory()
    task.requires_final_answer = True
    task.allow_generated_bindings = True
    obs = observation("complete")
    c = contract()
    c.bindings = [Binding(name="final_answer", value="Total: 42", source="planner")]
    controller = Controller(task, None, None, mode="flat")
    controller.plan = Plan(subtasks=[c])
    assert controller.current(obs) is None
    candidates = generate(obs, task, memory, None, final_answer=controller.final_answer)
    finish = next(a for a in candidates if a.operation == Operation.FINISH)
    assert finish.bound_value == "Total: 42"


async def test_two_page_cycle_stops_before_action_budget():
    from jev_browser.controller import Controller
    from jev_browser.protocol import Budget, Decision, Receipt

    class AlternatingPages:
        calls = 0

        async def observe(self):
            self.calls += 1
            obs = observation(f"page-{self.calls % 2}")
            obs.document_version = f"v{self.calls % 2}"
            return obs

        async def execute(self, action):
            return Receipt(action_id=action.id, status="ok")

    class KeepNavigating:
        async def choose(self, task, obs, memory, contract, candidates):
            return Decision(choice=next(a.id for a in candidates if a.operation == Operation.WAIT))

    result = await Controller(
        demo_task(1),
        AlternatingPages(),
        KeepNavigating(),
        mode="flat",
        budget=Budget(max_actions=100, no_progress_limit=2),
    ).run()
    assert result.status == "needs_attention"
    assert result.actions == 4
