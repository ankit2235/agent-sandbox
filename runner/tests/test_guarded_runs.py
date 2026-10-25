"""``sandbox run --guard``: the guard in front of the world's high-impact tools.

- No public or stressed golden run is held by the hard rules (the packs and each task's
  own delegated limits), in either world.
- Five backends run through the guard (rules only, our model, Jev, OpenAI Decisions, an LLM
  judge), each on fakes, and every run's decision records verify.
- ``sandbox verify`` finds an edited, removed or re-chained decision log; resuming with
  another guard, exporting a guarded run to the main leaderboard, and a policy for another
  world are refused.
"""

import json
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest
from runner_testkit import call, scripted, turn, world_config, write_hidden_set

from guard.backends.config import describe, load_config
from guard.backends.guard_model.backend import GuardModelBackend
from guard.backends.llm_judge import LLMJudge
from guard.backends.openai_decisions import OpenAIDecisionsBackend
from guard.backends.rules_only import RulesOnlyBackend
from guard.backends.system_one import SystemOneBackend
from guard.decision import BackendResult, Decision, DecisionRequest
from guard.policy import load_policy
from guard.records import DecisionLog, read_log
from runner.cli import main as sandbox
from runner.export import export
from runner.guarding import DECISIONS_FILE, ChatAdapter, GuardSetup
from runner.models import ModelReply
from runner.runs import RESULT_FILE, RUNS_DIR, ResumeMismatch, execute, load_records
from runner.verify import verify


class AllowBackend:
    """Says allow to everything, so only the hard rules can hold a call."""

    name = "allow-all"

    def decide(self, request: DecisionRequest) -> BackendResult:
        return BackendResult(
            decision=Decision(decision="allow", reason_codes=[], confidence=1.0), backend=self.name
        )


class AllowChat:
    """A judge model that always answers allow (as JSON)."""

    id = "fake/judge"

    def complete(self, messages: Sequence[dict], tools: Sequence[dict]) -> ModelReply:
        return ModelReply(text='{"decision": "allow", "reason_codes": [], "confidence": 0.9}')


class Scorer:
    """Our model's backend on a fake scorer that prefers allow."""

    model_id, revision = "fake/guard", "r1"

    def logprobs(self, text: str, continuations: Sequence[str]) -> list[float]:
        return [-9.0, -9.0, -0.01]

    def generate(self, text: str, max_new_tokens: int) -> str:
        return "]}"


def mock(body: object) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))


def setups(world: str) -> dict[str, GuardSetup]:
    policy = load_policy(f"{world}/default")
    choice = {
        "model": "jev-1.13.0",
        "answers": {"decision": {"choice": "allow", "confidence": 0.95}},
    }
    decisions = {
        "model": "gpt-6-luna",
        "answers": [{"name": "decision", "type": "choice", "choice": "allow", "confidence": 0.95}],
        "usage": {"input_tokens": 400},
    }
    backends = {
        "rules_only": RulesOnlyBackend(),
        "guard-qwen2.5-0.5b": GuardModelBackend("guard-qwen2.5-0.5b", Scorer()),
        "jev": SystemOneBackend(
            "jev",
            endpoint="https://x.invalid/v1/systemone",
            model="jev-latest",
            client=mock(choice),
        ),
        "openai-decisions": OpenAIDecisionsBackend(
            "openai-decisions", model="gpt-6-luna", api_key="not-a-key", client=mock(decisions)
        ),
        "judge-a": LLMJudge("judge-a", ChatAdapter(AllowChat())),
    }
    configs = {name: describe(load_config(name)) for name in backends}
    return {
        name: GuardSetup(
            policy, backend, policy_source=f"{world}/default", backend_config=configs[name]
        )
        for name, backend in backends.items()
    }


def guarded(world: str, setup: GuardSetup, **overrides: object):
    return world_config(world, guard=setup.config, **overrides)


@pytest.mark.parametrize("task_set", ["v0.1", "v0.1-stressed"])
@pytest.mark.parametrize("world", ["payments", "security"])
def test_no_public_golden_run_is_held_by_the_hard_rules(
    tmp_path: Path, world: str, task_set: str
) -> None:
    policy = load_policy(f"{world}/default")
    setup = GuardSetup(
        policy,
        AllowBackend(),
        policy_source=f"{world}/default",
        backend_config={"name": "allow-all", "kind": "rules_only"},
    )
    out = tmp_path / "out"
    config = guarded(world, setup, tasks=f"{world}-{task_set}")
    execute(config, out, concurrency=4, guard=setup)
    records = load_records(out)
    assert records and all(r.status == "completed" for r in records)
    held = [(r.task_id, r.guard) for r in records if r.guard.blocked or r.guard.asked]
    assert held == []
    assert all(r.passed for r in records), [r.task_id for r in records if not r.passed]
    assert sum(r.guard.decisions for r in records) > 30
    assert verify(out).ok


@pytest.mark.parametrize("world", ["payments", "security"])
@pytest.mark.parametrize(
    "backend", ["rules_only", "guard-qwen2.5-0.5b", "jev", "openai-decisions", "judge-a"]
)
def test_every_backend_runs_through_the_guard_and_its_records_verify(
    tmp_path: Path, world: str, backend: str
) -> None:
    setup = setups(world)[backend]
    for model in ("fake:golden", "fake:wrong"):
        out = tmp_path / model.replace(":", "-")
        execute(guarded(world, setup, model=model, model_name=model, sample=8), out, guard=setup)
        records = load_records(out)
        assert len(records) == 8 and all(r.status == "completed" for r in records)
        assert sum(r.guard.decisions for r in records) > 0
        report = verify(out)
        assert report.ok, [v.problems for v in report.verdicts if not v.ok]
        for record in records:
            rows, problems = read_log(out / RUNS_DIR / record.run_id / DECISIONS_FILE)
            assert problems == []
            assert all(row["policy"]["hash"] == setup.policy.hash for row in rows)
            assert all(row["backend"] is None or row["backend"]["name"] == backend for row in rows)


def test_the_guard_stops_mistakes_a_wrong_run_makes(tmp_path: Path) -> None:
    setup = setups("payments")["rules_only"]
    plain, guarded_out = tmp_path / "plain", tmp_path / "guarded"
    execute(world_config("payments", model="fake:wrong", model_name="fake:wrong"), plain)
    execute(
        guarded("payments", setup, model="fake:wrong", model_name="fake:wrong"),
        guarded_out,
        guard=setup,
    )
    unsafe = lambda out: sum(r.grade.failure_type is not None for r in load_records(out))  # noqa: E731
    records = load_records(guarded_out)
    assert sum(r.guard.blocked for r in records) > 0
    assert sum(r.passed for r in records) > sum(r.passed for r in load_records(plain))
    assert unsafe(guarded_out) < unsafe(plain)


@pytest.fixture
def one_run(tmp_path: Path) -> tuple[Path, Path]:
    setup = setups("payments")["rules_only"]
    out = tmp_path / "out"
    execute(
        guarded("payments", setup, model="fake:wrong", model_name="fake:wrong", sample=12),
        out,
        guard=setup,
    )
    for record in load_records(out):
        log = out / RUNS_DIR / record.run_id / DECISIONS_FILE
        if record.guard.decisions >= 2 and record.guard.allowed >= 1:
            return out, log
    pytest.fail("no run with two decisions in the sample")


def _rewrite(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_verify_finds_an_edited_decision(one_run) -> None:
    out, log = one_run
    rows, _ = read_log(log)
    rows[0]["decision"] = "block" if rows[0]["decision"] != "block" else "allow"
    _rewrite(log, rows)
    problems = [p for v in verify(out).verdicts for p in v.problems]
    assert any("edited" in p for p in problems)


def test_verify_finds_a_removed_last_decision(one_run) -> None:
    out, log = one_run
    rows, _ = read_log(log)
    _rewrite(log, rows[:-1])
    problems = [p for v in verify(out).verdicts for p in v.problems]
    assert any("head" in p for p in problems)


def test_verify_finds_a_missing_log(one_run) -> None:
    out, log = one_run
    log.unlink()
    assert any("missing" in p for v in verify(out).verdicts for p in v.problems)


def test_verify_finds_a_re_chained_log_without_an_allowed_call(one_run) -> None:
    """A valid chain and head, but an allowed decision dropped: the trace still has the call."""
    out, log = one_run
    rows, _ = read_log(log)
    keep = [row for row in rows if row is not next(r for r in rows if r["decision"] == "allow")]
    rebuilt = DecisionLog(log.with_name("rebuilt.jsonl"))
    for row in keep:
        rebuilt.append({k: v for k, v in row.items() if k not in ("format", "seq", "prev", "hash")})
    log.write_text(log.with_name("rebuilt.jsonl").read_text())
    result_path = log.with_name(RESULT_FILE)
    result = json.loads(result_path.read_text())
    result["guard"] |= {"decisions": len(keep), "head": rebuilt.head.hash if rebuilt.head else None}
    result_path.write_text(json.dumps(result))
    problems = [p for v in verify(out).verdicts for p in v.problems]
    assert any("guarded calls reached the world" in p for p in problems), problems


def test_verify_finds_a_policy_changed_since_the_run(tmp_path: Path) -> None:
    source = tmp_path / "policy.yaml"
    pack = (
        Path(__file__).parents[2] / "guard/src/guard/policies/payments/default.yaml"
    ).read_text()
    source.write_text(pack)
    setup = GuardSetup(
        load_policy(source),
        RulesOnlyBackend(),
        policy_source=str(source),
        backend_config={"name": "rules_only", "kind": "rules_only"},
    )
    out = tmp_path / "out"
    execute(guarded("payments", setup, sample=2), out, guard=setup)
    assert verify(out).ok
    source.write_text(pack.replace("version: 1", "version: 2"))
    assert any("changed since the run" in p for v in verify(out).verdicts for p in v.problems)


def test_resuming_with_another_guard_is_refused(tmp_path: Path) -> None:
    first, second = setups("payments")["rules_only"], setups("payments")["jev"]
    out = tmp_path / "out"
    execute(guarded("payments", first, sample=2), out, guard=first)
    with pytest.raises(ResumeMismatch, match="guard"):
        execute(guarded("payments", second, sample=2), out, guard=second)
    with pytest.raises(ValueError, match="guard"):
        execute(guarded("payments", first, sample=2), out)


def test_a_guarded_run_never_reaches_the_main_leaderboard(tmp_path: Path) -> None:
    setup = setups("payments")["rules_only"]
    out = tmp_path / "out"
    execute(guarded("payments", setup, sample=2), out, guard=setup)
    with pytest.raises(ValueError, match=r"guards/results\.json"):
        export([out], tmp_path / "board", __import__("datetime").date(2026, 11, 1))


def test_the_cli_runs_a_guarded_task_set(tmp_path: Path, capsys) -> None:
    out = tmp_path / "cli"
    code = sandbox(
        [
            "run",
            "--world",
            "payments",
            "--model",
            "fake:golden",
            "--tasks",
            "payments-v0.1",
            "--sample",
            "3",
            "--out",
            str(out),
            "--guard",
            "payments/default",
            "--backend",
            "rules_only",
        ]
    )
    assert code == 0
    assert all(r.guard is not None for r in load_records(out))
    assert sandbox(["verify", "--run", str(out)]) == 0
    assert (
        sandbox(
            [
                "run",
                "--world",
                "payments",
                "--model",
                "fake:golden",
                "--tasks",
                "payments-v0.1",
                "--out",
                str(tmp_path / "x"),
                "--guard",
                "payments/default",
            ]
        )
        == 2
    )
    assert (
        sandbox(
            [
                "run",
                "--world",
                "security",
                "--model",
                "fake:golden",
                "--tasks",
                "security-v0.1",
                "--out",
                str(tmp_path / "y"),
                "--guard",
                "payments/default",
                "--backend",
                "rules_only",
            ]
        )
        == 2
    )
    assert "is for payments" in capsys.readouterr().out


def test_a_guarded_hidden_run_writes_hashes_only(tmp_path: Path, monkeypatch) -> None:
    hidden = write_hidden_set(tmp_path, "payments", 2, monkeypatch)
    monkeypatch.setenv("HIDDEN_TASKS_DIR", str(hidden))
    setup = setups("payments")["rules_only"]
    out = tmp_path / "out"
    factory = scripted(
        turn(call("pay", {"payee": "x@sandbox", "amount_paise": 100, "idempotency_key": "k"})),
        turn(text="done"),
    )
    execute(
        guarded("payments", setup, tasks="payments-v0.1-hidden"), out, factory=factory, guard=setup
    )
    for record in load_records(out):
        assert record.guard.decisions == 1
        text = (out / RUNS_DIR / record.run_id / DECISIONS_FILE).read_text()
        assert "secret-" not in text and "canary" not in text.lower()


def test_a_guarded_hidden_run_without_the_private_repo_fails_cleanly(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("HIDDEN_TASKS_DIR", raising=False)
    setup = setups("payments")["rules_only"]
    with pytest.raises(Exception, match="HIDDEN_TASKS_DIR"):
        execute(
            guarded("payments", setup, tasks="payments-v0.1-hidden"), tmp_path / "out", guard=setup
        )
