"""Multi-agent runs: counterparts played by the runner, recorded in the trace, re-derived by verify.

The world is payments' ``payments-v0.1-agents`` set: the user's agent (fake models here) against
the world's scripted seller, or a seller played by a scripted fake model to exercise the model
path. No test calls a provider.
"""

import datetime as dt
import json
import shutil
import threading
from pathlib import Path

import pytest
from runner_testkit import call, read_json, scripted, turn, world_config

from guard.backends.rules_only import RulesOnlyBackend
from guard.policy import load_policy
from kit.scripted import load_runs, play
from kit.tasks import load_tasks
from kit.world import worlds
from runner.cli import main as sandbox
from runner.counterpart import SCRIPTED, counterpart_prompt
from runner.export import export
from runner.guarding import DECISIONS_FILE, GuardSetup
from runner.isolation import Isolation, IsolationError
from runner.models import FakeModel, model_factory
from runner.runs import (
    MANIFEST,
    RUNS_DIR,
    TRACE_FILE,
    ResumeMismatch,
    execute,
    load_records,
    read_manifest,
    resolve_task_set,
)
from runner.verify import verify

AGENTS = "payments-v0.1-agents"
SPEC = worlds.get("payments")
TASKS = load_tasks(SPEC.task_sets["agents"])
SELLER = frozenset(SPEC.counterpart_tools())


def agents(**overrides: object):
    return world_config("payments", **{"tasks": AGENTS, **overrides})


def trace_calls(out: Path, run_id: str) -> list[tuple[str, dict]]:
    lines = (out / RUNS_DIR / run_id / TRACE_FILE).read_text(encoding="utf-8").splitlines()[1:]
    steps = [json.loads(line) for line in lines]
    return [(s["tool"], s["args"]) for s in steps if s["kind"] == "tool_call"]


def files(out: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(out)): path.read_bytes()
        for path in sorted((out / RUNS_DIR).rglob("*"))
        if path.is_file()
    }


@pytest.fixture(scope="module")
def golden(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("agents") / "golden"
    execute(agents(), out, concurrency=4)
    return out


# --------------------------------------------------------------- end to end
def test_the_set_resolves_by_name_and_stays_out_of_payments_v0_1() -> None:
    tasks = resolve_task_set(SPEC, AGENTS)
    assert sorted(t.id for t in tasks) == sorted(t.id for t in TASKS)
    assert not {t.id for t in tasks} & {t.id for t in resolve_task_set(SPEC, "payments-v0.1")}
    with pytest.raises(ValueError, match=r"payments-v0\.1-agents"):
        resolve_task_set(SPEC, "payments-v0.1-other")


def test_every_task_passes_against_the_scripted_seller_and_verifies(golden: Path) -> None:
    records = load_records(golden)
    assert len(records) == 12
    assert all(r.passed for r in records), [r.task_id for r in records if not r.passed]
    for record in records:
        assert record.counterpart is not None
        assert record.counterpart.player == SCRIPTED
        assert record.counterpart.prompt is None and record.counterpart.model_turns == 0
        silent = record.task_id == "agents-negotiate-silent-seller"
        assert (record.counterpart.tool_calls == 0) is silent
        calls = trace_calls(golden, record.run_id)
        assert record.steps == sum(tool not in SELLER for tool, _ in calls)
    report = verify(golden)
    assert report.ok and len(report.verdicts) == 12
    manifest = read_json(golden / MANIFEST)
    assert "counterpart" not in manifest["config"]


def test_a_runs_trace_is_its_run_file_exactly(golden: Path) -> None:
    """fake:golden replays only the buyer's steps; the scripted seller makes the rest."""
    for record in load_records(golden):
        task = next(t for t in TASKS if t.id == record.task_id)
        run = load_runs(task.id, SPEC.runs_dir).runs["golden"]
        flat = [(s.tool, s.args) for s in play(SPEC, task, run).trace.tool_calls()]
        assert trace_calls(golden, record.run_id) == flat


def test_every_first_wrong_run_fails_as_declared_and_verifies(tmp_path: Path) -> None:
    out = tmp_path / "wrong"
    execute(agents(model="fake:wrong", model_name="fake:wrong"), out, concurrency=4)
    for record in load_records(out):
        runs = load_runs(record.task_id, SPEC.runs_dir)
        assert not record.passed
        assert record.grade is not None
        assert record.grade.failure_type == runs.runs[runs.wrong[0]].fails_with
    assert verify(out).ok


def test_a_killed_agents_run_resumes_to_the_uninterrupted_result(tmp_path: Path, golden) -> None:
    class Killed(BaseException):
        pass

    base = model_factory("fake:golden")
    started = 0
    lock = threading.Lock()

    def dies_after_five(spec, task):
        nonlocal started
        with lock:
            started += 1
            if started > 5:
                raise Killed
        return base(spec, task)

    out = tmp_path / "resumed"
    with pytest.raises(Killed):
        execute(agents(), out, factory=dies_after_five)
    assert read_manifest(out).status.completed == 5
    execute(agents(), out)
    assert files(out) == files(golden)
    assert verify(out).ok


# ------------------------------------------------------------ verify catches
def _copy(golden: Path, tmp_path: Path) -> Path:
    target = tmp_path / "copy"
    shutil.copytree(golden, target)
    return target


def _edit_trace(out: Path, run_id: str, edit) -> None:
    path = out / RUNS_DIR / run_id / TRACE_FILE
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(edit(lines)) + "\n", encoding="utf-8")


def test_verify_finds_an_edited_seller_move(golden: Path, tmp_path: Path) -> None:
    out = _copy(golden, tmp_path)
    run_id = "fake-golden-agents-negotiate-within-budget-r1"
    _edit_trace(
        out,
        run_id,
        lambda ls: [line.replace('"price_paise":220000', '"price_paise":200000') for line in ls],
    )
    verdict = next(v for v in verify(out).verdicts if v.run_id == run_id)
    assert any("scripted counterpart calls seller_counter" in p for p in verdict.problems)


def test_verify_finds_a_dropped_seller_move(golden: Path, tmp_path: Path) -> None:
    out = _copy(golden, tmp_path)
    run_id = "fake-golden-agents-invoice-padded-fee-r1"

    def drop_first_seller_call(lines: list[str]) -> list[str]:
        for n, line in enumerate(lines):
            if '"tool":"seller_invoice"' in line:
                return lines[:n] + lines[n + 1 :]
        raise AssertionError("no seller call")

    _edit_trace(out, run_id, drop_first_seller_call)
    verdict = next(v for v in verify(out).verdicts if v.run_id == run_id)
    assert not verdict.ok
    assert any("scripted counterpart calls seller_invoice" in p for p in verdict.problems)


def test_verify_finds_a_seller_move_the_scripted_seller_never_made(golden, tmp_path) -> None:
    out = _copy(golden, tmp_path)
    run_id = "fake-golden-agents-negotiate-silent-seller-r1"

    def add_accept(lines: list[str]) -> list[str]:
        first = next(n for n, line in enumerate(lines) if '"kind":"tool_call"' in line)
        step = json.loads(lines[first])
        step.update(tool="seller_message", args={"deal_id": "deal-camera", "message": "hi"})
        return [*lines[: first + 1], json.dumps(step), *lines[first + 1 :]]

    _edit_trace(out, run_id, add_accept)
    verdict = next(v for v in verify(out).verdicts if v.run_id == run_id)
    assert any("does not make" in p for p in verdict.problems)


# ------------------------------------------------------------ model counterparts
def seller_factory(*turns):
    """A 'model' seller: scripted replies, in order, for every task."""
    return lambda spec, task: FakeModel(list(turns), model_id="test/seller")


def _within_budget_dir(tmp_path: Path) -> Path:
    """A task directory with agents-negotiate-within-budget only."""
    folder = tmp_path / "tasks"
    folder.mkdir()
    source = SPEC.task_sets["agents"] / "agents-negotiate-within-budget.yaml"
    (folder / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return folder


def _silent_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "silent"
    folder.mkdir()
    source = SPEC.task_sets["agents"] / "agents-negotiate-silent-seller.yaml"
    (folder / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return folder


def test_a_model_seller_negotiates_the_within_budget_deal(tmp_path: Path) -> None:
    seller = seller_factory(
        turn(call("seller_desk"), tokens=10),
        turn(call("seller_counter", {"deal_id": "deal-camera", "price_paise": 220000}), tokens=10),
        turn(
            call("seller_accept", {"deal_id": "deal-camera"}, call_id="a"),
            call(
                "seller_invoice",
                {"deal_id": "deal-camera", "item_price_paise": 190000},
                call_id="b",
            ),
            tokens=10,
        ),
    )
    out = tmp_path / "model"
    config = agents(tasks=str(_within_budget_dir(tmp_path)), counterpart="test/seller")
    execute(config, out, counterpart_factory=seller)
    record = load_records(out)[0]
    assert record.passed, record.grade
    summary = record.counterpart
    assert summary is not None and summary.player == "test/seller"
    assert summary.prompt == counterpart_prompt()[0] == "counterpart/v1"
    assert summary.model_turns == 3 and summary.tool_calls == 4
    assert summary.usage.prompt_tokens == 30 and record.usage.prompt_tokens == 0
    assert read_json(out / MANIFEST)["config"]["counterpart"] == "test/seller"
    assert verify(out).ok


def test_a_model_seller_that_never_acts_is_given_a_turn_after_each_buyer_call(tmp_path) -> None:
    out = tmp_path / "silent"
    config = agents(tasks=str(_silent_dir(tmp_path)), counterpart="test/seller")
    execute(config, out, counterpart_factory=seller_factory())
    record = load_records(out)[0]
    assert record.passed
    assert record.counterpart.turns == record.counterpart.model_turns == 3
    assert record.counterpart.tool_calls == 0


def test_a_model_seller_cannot_call_the_buyers_tools(tmp_path: Path) -> None:
    seller = seller_factory(
        turn(
            call(
                "pay", {"payee": "lens-bazaar@sandbox", "amount_paise": 1, "idempotency_key": "x"}
            ),
            call("seller_desk", "not json", call_id="bad"),
        )
    )
    out = tmp_path / "grabby"
    config = agents(tasks=str(_silent_dir(tmp_path)), counterpart="test/seller")
    execute(config, out, counterpart_factory=seller)
    record = load_records(out)[0]
    assert record.counterpart.refused_calls == 2
    tools = [tool for tool, _ in trace_calls(out, record.run_id)]
    assert "pay" not in tools and "seller_desk" not in tools


def test_the_buyer_cannot_call_the_sellers_tools(tmp_path: Path) -> None:
    buyer = scripted(
        turn(call("make_offer", {"deal_id": "deal-camera", "price_paise": 100000})),
        turn(call("seller_accept", {"deal_id": "deal-camera"})),
    )
    out = tmp_path / "forger"
    config = agents(tasks=str(_within_budget_dir(tmp_path)), model="test/buyer")
    execute(config, out, factory=buyer)
    record = load_records(out)[0]
    assert record.malformed_calls == 1
    tools = [tool for tool, _ in trace_calls(out, record.run_id)]
    assert tools == ["make_offer", "seller_counter"]
    assert not record.passed


def test_the_buyer_is_offered_only_its_own_tools(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    class Looks(FakeModel):
        def complete(self, messages, tools):
            seen.append([tool["function"]["name"] for tool in tools])
            return super().complete(messages, tools)

    out = tmp_path / "looks"
    config = agents(tasks=str(_within_budget_dir(tmp_path)), model="test/buyer")
    execute(config, out, factory=lambda spec, task: Looks([]))
    assert seen and not set(seen[0]) & SELLER
    assert "make_offer" in seen[0] and "ask_user" in seen[0]


def test_caps_count_both_agents(tmp_path: Path) -> None:
    buyer = scripted(
        turn(call("make_offer", {"deal_id": "deal-camera", "price_paise": 180000}), tokens=10),
        turn(call("wait", {"hours": 1}), tokens=10),
    )
    out = tmp_path / "capped"
    config = agents(
        tasks=str(_silent_dir(tmp_path)),
        model="test/buyer",
        counterpart="test/seller",
        max_tokens=100,
    )
    execute(config, out, factory=buyer, counterpart_factory=seller_factory(turn(tokens=500)))
    record = load_records(out)[0]
    assert record.stop_reason == "max_tokens"
    assert record.usage.prompt_tokens == 10 and record.counterpart.usage.prompt_tokens == 500


# ------------------------------------------------------------ what is refused
def test_resuming_with_another_counterpart_is_refused(tmp_path: Path) -> None:
    out = tmp_path / "out"
    tasks = str(_silent_dir(tmp_path))
    execute(agents(tasks=tasks), out)
    with pytest.raises(ResumeMismatch, match="counterpart"):
        execute(
            agents(tasks=tasks, counterpart="test/seller"),
            out,
            counterpart_factory=seller_factory(),
        )


def test_multi_agent_runs_are_refused_in_containers_for_now(tmp_path: Path) -> None:
    with pytest.raises(IsolationError, match="multi-agent tasks cannot run"):
        execute(agents(), tmp_path / "c", isolation=Isolation(image="agent-sandbox:0.1.0"))


def test_the_agents_set_is_never_exported(golden: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="pre-release task set"):
        export([golden], tmp_path / "export", dt.date(2026, 10, 25))


def test_the_cli_runs_and_verifies_the_set_and_refuses_a_fake_counterpart(tmp_path, capsys):
    out = tmp_path / "cli"
    args = ["run", "--world", "payments", "--tasks", AGENTS, "--model", "fake:golden"]
    assert sandbox([*args, "--out", str(out), "--sample", "2"]) == 0
    assert sandbox(["verify", "--run", str(out)]) == 0
    assert sandbox([*args, "--out", str(tmp_path / "x"), "--counterpart", "fake:golden"]) == 2
    assert "use scripted or a LiteLLM model id" in capsys.readouterr().out


def test_tasks_validate_and_worlds_cover_the_extra_set(capsys) -> None:
    assert sandbox(["tasks", "validate"]) == 0
    printed = capsys.readouterr().out
    assert "ok   payments/v0.1/agents-negotiate-within-budget" in printed
    assert sandbox(["worlds"]) == 0
    printed = capsys.readouterr().out
    assert "extra set     : payments-v0.1-agents" in printed
    assert "counterpart   : seller (seller_desk" in printed


# ------------------------------------------------------------------ the guard
def test_a_guarded_agents_run_verifies_with_its_decision_records(tmp_path: Path) -> None:
    policy = load_policy("payments/default")
    setup = GuardSetup(
        policy,
        RulesOnlyBackend(),
        policy_source="payments/default",
        backend_config={"name": "rules_only", "kind": "rules_only"},
    )
    out = tmp_path / "guarded"
    execute(agents(guard=setup.config), out, guard=setup, concurrency=4)
    records = load_records(out)
    assert len(records) == 12 and all(r.status == "completed" for r in records)
    assert sum(r.guard.decisions for r in records) > 0
    assert all((out / RUNS_DIR / r.run_id / DECISIONS_FILE).exists() for r in records)
    report = verify(out)
    assert report.ok, [v.problems for v in report.verdicts if not v.ok]
    assert all(r.passed for r in records if not r.guard.blocked and not r.guard.asked)
