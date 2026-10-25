"""The guard library around the vectors: packs, policy files, the engine, the decision log,
the decorator and the ``sandbox-guard`` CLI."""

import datetime as dt
import json
from pathlib import Path

import pytest
import yaml

from guard import (
    ActionCall,
    BackendResult,
    Decision,
    DecisionLog,
    DecisionRequest,
    Fields,
    Guard,
    GuardAskUser,
    GuardBlocked,
    PolicyError,
    guarded,
    load_policy,
    parse_policy,
    verify_chain,
)
from guard.actions import actions
from guard.cli import main as guard_cli
from guard.policy import effective, packs, parse_delegation
from guard.records import read_log

NOW = dt.datetime(2026, 1, 1, 9, tzinfo=dt.UTC)
POLICY = {
    "format": "guard-policy-v1",
    "id": "library",
    "version": 3,
    "actions": {"pay": {"budget": {"per_action": 1000, "daily": 2000}, "confirm_above": 800}},
}


def req(tool: str = "pay", at: dt.datetime = NOW, **args: object) -> DecisionRequest:
    return DecisionRequest(
        world="w", instruction="i", action=ActionCall(tool=tool, args=args), sim_time=at
    )


def test_the_packs_load_and_name_only_checks_their_world_has() -> None:
    assert packs() == ["payments/default", "security/default"]
    for name in packs():
        policy = load_policy(name)
        world = actions.get(policy.world or "")
        Guard(policy, checks={rule.id: rule for rule in world.rules})
        assert set(policy.actions) == set(world.high_impact), name


def test_yaml_and_json_of_the_same_policy_hash_alike(tmp_path: Path) -> None:
    (tmp_path / "p.yaml").write_text(yaml.safe_dump(POLICY), encoding="utf-8")
    (tmp_path / "p.json").write_text(json.dumps(POLICY), encoding="utf-8")
    assert load_policy(tmp_path / "p.yaml").hash == load_policy(tmp_path / "p.json").hash
    changed = {**POLICY, "version": 4}
    assert parse_policy(changed).hash != parse_policy(POLICY).hash


def test_a_broken_policy_file_is_refused_with_its_place(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "format: guard-policy-v1\nid: x\nversion: 1\nactions: {pay: {budget: {daily: -5}}}\n"
    )
    with pytest.raises(PolicyError, match="actions/pay/budget/daily"):
        load_policy(bad)
    (tmp_path / "broken.yaml").write_text("actions: [unclosed\n")
    with pytest.raises(PolicyError, match="not YAML"):
        load_policy(tmp_path / "broken.yaml")
    with pytest.raises(FileNotFoundError, match="payments/default"):
        load_policy("payments/nonexistent")


def test_sandbox_runs_leave_out_the_packs_numbers_and_keep_the_task_s() -> None:
    policy = parse_policy(POLICY)
    task = parse_delegation({"pay": {"budget": {"per_action": 5000}}})
    outside = effective(policy, "pay", task)
    inside = effective(policy, "pay", task, pack_limits=False)
    assert next(rule.budget for rule in outside.rules if rule.budget).per_action == 1000
    assert [rule.budget.per_action for rule in inside.rules if rule.budget] == [5000]
    assert inside.rules[0].confirm_above is None


def test_daily_budget_counts_only_what_was_allowed() -> None:
    guard = Guard(parse_policy(POLICY), log=DecisionLog())
    assert guard.decide(req(), Fields(amount=700)).decision == "allow"
    assert guard.decide(req(), Fields(amount=900)).decision == "ask_user"  # held: not counted
    assert guard.decide(req(), Fields(amount=700)).decision == "allow"  # 1,400 of 2,000
    third = guard.decide(req(), Fields(amount=700))
    assert third.reason_codes == ["guard.policy.over-daily-budget"]
    tomorrow = guard.decide(req(at=NOW + dt.timedelta(days=1)), Fields(amount=700))
    assert tomorrow.decision == "allow"


def test_fields_are_strict() -> None:
    with pytest.raises(ValueError):
        Fields.model_validate({"amount": "700"})
    with pytest.raises(ValueError):
        Fields.model_validate({"amount": True})
    with pytest.raises(ValueError):
        Fields.model_validate({"amount": -1})
    with pytest.raises(ValueError):
        Fields.model_validate({"payee": "x"})


def test_a_delegation_naming_an_unknown_check_is_refused_when_it_is_used() -> None:
    guard = Guard(parse_policy(POLICY), delegation=lambda raw: {"pay": {"checks": ["w.rule.nope"]}})
    with pytest.raises(PolicyError, match=r"w\.rule\.nope"):
        guard.decide(req(), Fields(amount=1))
    with pytest.raises(PolicyError, match=r"w\.rule\.nope"):
        Guard(parse_policy(POLICY), delegation={"pay": {"checks": ["w.rule.nope"]}})


def test_a_record_holds_hashes_not_the_request(tmp_path: Path) -> None:
    log = DecisionLog(tmp_path / "d.jsonl")
    guard = Guard(parse_policy(POLICY), log=log)
    outcome = guard.decide(req(payee="secret-payee@sandbox"), Fields(amount=5000))
    text = (tmp_path / "d.jsonl").read_text()
    assert "secret-payee" not in text
    assert outcome.record is not None and outcome.record.decision == "block"
    assert outcome.record.policy.id == "library" and outcome.record.policy.version == 3
    assert outcome.record.sim_time == "2026-01-01T09:00:00Z"


def _logged(tmp_path: Path, count: int = 4) -> tuple[Path, DecisionLog]:
    log = DecisionLog(tmp_path / "decisions.jsonl")
    guard = Guard(parse_policy(POLICY), log=log)
    for n in range(count):
        guard.decide(req(at=NOW + dt.timedelta(minutes=n)), Fields(amount=100 * (n + 1)))
    return tmp_path / "decisions.jsonl", log


def test_a_log_read_back_from_disk_verifies(tmp_path: Path) -> None:
    path, log = _logged(tmp_path)
    rows, problems = read_log(path)
    assert problems == []
    assert verify_chain(rows, head=log.head.hash, count=log.head.count) == []


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (lambda lines: [lines[0], lines[1].replace('"allow"', '"block"'), *lines[2:]], "edited"),
        (lambda lines: [lines[0], *lines[2:]], "removed or reordered"),
        (lambda lines: lines[:-1], "head"),
        (lambda lines: [lines[1], lines[0], *lines[2:]], "removed or reordered"),
        (lambda lines: [lines[0], "{not json", *lines[1:]], "not JSON"),
    ],
    ids=["edited", "removed", "last-removed", "reordered", "garbage-line"],
)
def test_the_cli_finds_a_tampered_log(tmp_path: Path, change, problem: str, capsys) -> None:
    path, log = _logged(tmp_path)
    lines = path.read_text().splitlines()
    path.write_text("\n".join(change(lines)) + "\n")
    code = guard_cli(["verify", str(path), "--head", log.head.hash, "--count", str(log.head.count)])
    assert code == 1
    assert problem in capsys.readouterr().out


def test_the_cli_checks_policies(tmp_path: Path, capsys) -> None:
    assert guard_cli(["policy", "check", "payments/default"]) == 0
    assert "payments-default v1" in capsys.readouterr().out
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {**POLICY, "world": "payments", "actions": {"pay": {"checks": ["payments.rule.nope"]}}}
        )
    )
    assert guard_cli(["policy", "check", str(bad)]) == 1
    assert "payments.rule.nope" in capsys.readouterr().out
    assert guard_cli(["policy", "list"]) == 0


def test_the_decorator_runs_only_what_was_allowed() -> None:
    paid: list[int] = []
    guard = Guard(parse_policy(POLICY), log=DecisionLog())
    options = {
        "world": "w",
        "instruction": "Pay.",
        "clock": lambda: NOW,
        "fields": lambda args: {"amount": args["amount"]},
    }

    @guarded(guard, **options)
    def pay(payee: str, amount: int) -> int:
        paid.append(amount)
        return amount

    @guarded(guard, tool="pay", confirm=lambda outcome: True, **options)
    def pay_confirmed(payee: str, amount: int) -> int:
        paid.append(amount)
        return amount

    assert pay("shop", 100) == 100
    with pytest.raises(GuardBlocked) as blocked:
        pay("shop", amount=5000)
    assert "guard.policy.over-per-action-budget" in blocked.value.outcome.reason_codes
    with pytest.raises(GuardAskUser):
        pay("shop", 900)
    assert pay_confirmed("shop", 900) == 900
    assert paid == [100, 900]
    layers = [(r.decision, r.layer) for r in guard.log.records]
    assert layers == [
        ("allow", "hard"),
        ("block", "hard"),
        ("ask_user", "hard"),
        ("ask_user", "hard"),
        ("allow", "user"),
    ]
    assert verify_chain([r.model_dump(mode="json") for r in guard.log.records]) == []


def test_a_sim_time_without_a_time_zone_is_refused() -> None:
    guard = Guard(parse_policy(POLICY))
    with pytest.raises(PolicyError, match="time zone"):
        guard.decide(req(at=dt.datetime(2026, 1, 1, 9, 30)), Fields(amount=1))


def test_the_backend_s_answer_is_recorded_with_its_model_and_tokens(tmp_path: Path) -> None:
    class Backend:
        name = "b"

        def decide(self, request: DecisionRequest) -> BackendResult:
            return BackendResult(
                decision=Decision(decision="block", reason_codes=["w.x"], confidence=0.9),
                backend="b",
                model="m",
                model_version="m-1",
                input_tokens=12,
                output_tokens=0,
            )

    guard = Guard(parse_policy(POLICY), Backend(), log=DecisionLog())
    outcome = guard.decide(req(), Fields(amount=10))
    record = outcome.record
    assert (outcome.decision, outcome.layer) == ("block", "backend")
    assert record.backend.model_version == "m-1" and record.tokens.input == 12
