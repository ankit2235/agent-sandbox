"""The domain-neutral machinery: registry, aliases, apply's rules, sets and runs.

Exercised on the kit's hello-world example with modifiers defined here, so no world
package is imported (hard rule 8 holds for tests of the core too).
"""

import pytest

from kit.rng import SeededRNG
from kit.scripted import GOLDEN, Run, RunFile, Step
from kit.tasks import load_tasks
from kit.types import TaskSpec
from kit.world import worlds
from stress.core import (
    GENERIC,
    Modifier,
    NotApplicable,
    StressError,
    WorldStress,
    apply,
    base_id,
    before_end,
    derive,
    per_modifier,
    registry,
    scripted_runs,
    step,
    stressed_set,
    wording,
)
from stress.core.apply import StressRegistry, child_seed

HELLO = "hello_world"


def tasks() -> list[TaskSpec]:
    spec = worlds.get(HELLO)
    assert spec.tasks_dir is not None
    return load_tasks(spec.tasks_dir)


def raise_target(task: TaskSpec, seed: int) -> TaskSpec:
    """Raise the target by a seeded amount: the grader reads it, so its expectation moves."""
    state = dict(task.initial_state)
    state["target"] = state["target"] + SeededRNG(seed).randint(1, 2)
    return derive(task, "lower_start", state)


def only_counting(task: TaskSpec, seed: int) -> TaskSpec:
    if task.initial_state["target"] != 3:
        msg = f"{task.id}: not a count-to-three task"
        raise NotApplicable(msg)
    return derive(task, "only_counting", dict(task.initial_state))


def rewrites_instruction(task: TaskSpec, seed: int) -> TaskSpec:
    derived = derive(task, "rude", dict(task.initial_state))
    return derived.model_copy(update={"instruction": "Do something else."})


def forgets_itself(task: TaskSpec, seed: int) -> TaskSpec:
    return task.model_copy(update={"id": f"{task.id}.forgets_itself"})


def two_more(task: TaskSpec, steps: list[Step]) -> list[Step]:
    return [step("increment"), *steps]


def stops(task: TaskSpec, steps: list[Step]) -> Run:
    return Run(steps=[step("finish")], fails_with="kit.incomplete")


def no_bait(task: TaskSpec, steps: list[Step]) -> Run:
    raise NotApplicable(f"{task.id}: nothing to take")


def modifier(name: str, apply_fn, trap=stops, **extra) -> Modifier:
    return Modifier(name, apply_fn, f"{name} for tests", trap, **extra)


HELLO_STRESS = WorldStress(
    world=HELLO,
    modifiers={
        "lower_start": modifier("lower_start", raise_target, adjust=two_more, extra_steps=2),
        "only_counting": modifier("only_counting", only_counting, trap=no_bait),
    },
    aliases={"shifted_constraint": "lower_start"},
)


@pytest.fixture(autouse=True)
def hello_stress():
    registry.register(HELLO_STRESS)
    yield


def test_the_generic_contracts_are_the_system_designs_five() -> None:
    assert sorted(GENERIC) == [
        "conflicting_instructions",
        "flaky_service",
        "misleading_content",
        "pressure",
        "shifted_constraint",
    ]


def test_apply_derives_an_id_records_itself_and_adds_steps() -> None:
    task = tasks()[0]
    stressed = apply(task, ["lower_start"], 4)
    assert stressed.id == f"{task.id}.lower_start" and stressed.stress == ["lower_start"]
    assert stressed.max_steps == task.max_steps + 2
    assert stressed.grader == task.grader and stressed.instruction == task.instruction
    assert apply(task, ["lower_start"], 4) == stressed, "same seed, same task"
    assert base_id(stressed) == task.id


def test_each_modifier_gets_its_own_derived_seed() -> None:
    task = tasks()[0]
    stressed = apply(task, ["lower_start"], 11)
    raised = SeededRNG(child_seed(11, "lower_start")).randint(1, 2)
    assert stressed.initial_state["target"] == task.initial_state["target"] + raised


def test_an_alias_resolves_to_the_world_modifier() -> None:
    task = tasks()[0]
    assert apply(task, ["shifted_constraint"], 1) == apply(task, ["lower_start"], 1)


def test_unknown_modifiers_name_what_exists() -> None:
    with pytest.raises(KeyError, match="unknown hello_world modifier 'nonsense'"):
        apply(tasks()[0], ["nonsense"], 0)
    with pytest.raises(KeyError, match="has no stress modifiers"):
        StressRegistry().get("no_such_world")


def test_a_modifier_cannot_be_asked_for_twice_or_reapplied() -> None:
    task = tasks()[0]
    with pytest.raises(StressError, match="more than once"):
        apply(task, ["lower_start", "shifted_constraint"], 0)
    once = apply(task, ["lower_start"], 0)
    with pytest.raises(StressError, match="already applied"):
        apply(once, ["lower_start"], 0)


def test_modifiers_compose_in_order() -> None:
    task = next(t for t in tasks() if t.initial_state["target"] == 3)
    both = apply(task, ["only_counting", "lower_start"], 2)
    assert both.id == f"{task.id}.only_counting.lower_start"
    assert both.stress == ["only_counting", "lower_start"]
    assert base_id(both) == task.id


def test_a_modifier_with_nothing_to_change_is_not_applicable() -> None:
    other = next(t for t in tasks() if t.initial_state["target"] != 3)
    with pytest.raises(NotApplicable, match="not a count-to-three task"):
        apply(other, ["only_counting"], 0)


def test_the_instruction_and_the_derived_id_are_enforced() -> None:
    registry.register(
        WorldStress(
            world=HELLO,
            modifiers={
                "rude": modifier("rude", rewrites_instruction),
                "forgets_itself": modifier("forgets_itself", forgets_itself),
            },
        )
    )
    with pytest.raises(StressError, match="changed instruction"):
        apply(tasks()[0], ["rude"], 0)
    with pytest.raises(StressError, match="must derive id"):
        apply(tasks()[0], ["forgets_itself"], 0)


def test_a_world_stress_checks_its_names_and_aliases() -> None:
    lower = HELLO_STRESS.modifiers["lower_start"]
    with pytest.raises(ValueError, match="is named"):
        WorldStress(world=HELLO, modifiers={"other": lower})
    with pytest.raises(ValueError, match="unknown modifier"):
        WorldStress(world=HELLO, modifiers={"lower_start": lower}, aliases={"x": "missing"})
    with pytest.raises(ValueError, match="hides a modifier"):
        WorldStress(
            world=HELLO, modifiers={"lower_start": lower}, aliases={"lower_start": "lower_start"}
        )


def test_the_stressed_set_spreads_modifiers_and_skips_what_cannot_apply() -> None:
    picked = stressed_set(tasks(), seed=0)
    assert [t.id for t in picked] == sorted(t.id for t in picked)
    assert {len(t.stress) for t in picked} == {1}
    assert stressed_set(tasks(), seed=0) == picked
    assert len(picked) == len(tasks())
    assert {t.stress[0] for t in picked} <= {"lower_start", "only_counting"}


def test_per_modifier_samples_applicable_tasks() -> None:
    found = per_modifier(tasks(), ["lower_start", "only_counting"], count=1, seed=3)
    assert len(found["lower_start"]) == 1
    assert [t.stress for t in found["only_counting"]] == [["only_counting"]]


def test_scripted_runs_adjust_the_golden_run_and_add_each_trap() -> None:
    task = next(t for t in tasks() if t.initial_state["target"] == 3)
    base = RunFile(
        task=task.id,
        runs={
            GOLDEN: Run(steps=[Step(tool="increment", expect={"count": 1}), step("finish")]),
            "wrong_x": Run(steps=[step("finish")], fails_with="kit.incomplete"),
        },
    )
    stressed = apply(task, ["only_counting", "lower_start"], 0)
    runs = scripted_runs(stressed, base, task_id="renamed")
    assert runs.task == "renamed"
    golden = runs.runs[GOLDEN].steps
    assert [s.tool for s in golden] == ["increment", "increment", "finish"]
    assert all(s.expect is None for s in golden), "expect lines describe the unstressed world"
    assert runs.wrong == ["wrong_lower_start"], "only_counting has no bait here"
    with pytest.raises(NotApplicable, match="no modifier has a bait"):
        scripted_runs(apply(task, ["only_counting"], 0), base)


def test_before_end_keeps_a_closing_terminal_call_last() -> None:
    steps = [step("a"), step("ask_user", question="?")]
    assert [s.tool for s in before_end(steps, [step("x")], ["ask_user"])] == ["a", "x", "ask_user"]
    assert [s.tool for s in before_end(steps[:1], [step("x")], ["ask_user"])] == ["a", "x"]


def test_wording_is_seeded() -> None:
    first = wording.urgency(SeededRNG(5), "buy the thing")
    assert first == wording.urgency(SeededRNG(5), "buy the thing")
    assert "minutes" in first and "buy the thing" in first
    assert "send it" in wording.injected_instruction(SeededRNG(1), "send it")
    assert "is free" in wording.false_claim(SeededRNG(1), "it is free")
