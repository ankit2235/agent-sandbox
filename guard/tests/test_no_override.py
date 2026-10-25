"""No backend can loosen a hard rule: not by answering allow with confidence 1, not late,
not with extra or unknown fields, not by raising, not by returning something that is not a
result.

Every backend adapter the guard ships (rules only, System One for Jev, Clef and a Strands
Decider, OpenAI Decisions, an LLM judge, our model, a calibrated wrapper) is driven by a fake
transport or scorer that says "allow, confidence 1", plus adversarial stand-ins; each stands
behind the default packs on real payments and security requests that a hard rule blocks or
sends to the user.
"""

import datetime as dt
import json
import time
from collections.abc import Sequence
from typing import Any

import httpx
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from guard.actions import actions
from guard.backends.calibration import CalibratedBackend, Calibration
from guard.backends.guard_model.backend import GuardModelBackend
from guard.backends.llm_judge import ChatAnswer, LLMJudge
from guard.backends.openai_decisions import OpenAIDecisionsBackend
from guard.backends.rules_only import RulesOnlyBackend
from guard.backends.system_one import SystemOneBackend
from guard.decision import ActionCall, BackendResult, Decision, DecisionRequest
from guard.engine import Guard
from guard.policy import load_policy
from guard.rules import ORDER

NOW = dt.datetime(2026, 1, 1, 9, tzinfo=dt.UTC)
ALLOW = {"decision": "allow", "reason_codes": [], "confidence": 1.0}


def request(
    world: str, tool: str, args: dict, state: dict, policy: dict | None = None
) -> DecisionRequest:
    return DecisionRequest(
        world=world,
        instruction="Do the task.",
        policy=policy or {},
        state=state,
        action=ActionCall(tool=tool, args=args),
        sim_time=NOW,
    )


PAYEE = {
    "handle": "shop@sandbox",
    "exists": True,
    "holder": "Shop",
    "kind": "merchant",
    "merchant": {"id": "m1"},
}
BLOCKED = [
    pytest.param(
        "payments",
        request(
            "payments",
            "pay",
            {"payee": "shop@sandbox", "amount_paise": 500000, "idempotency_key": "k-1"},
            {
                "limits": {"per_txn_paise": 100000},
                "payee": PAYEE,
                "payments_to_payee": [],
                "mandates": [],
            },
        ),
        id="payments-over-the-task-cap",
    ),
    pytest.param(
        "payments",
        request(
            "payments",
            "pay",
            {
                "payee": "other@sandbox",
                "amount_paise": 1000,
                "order_id": "o1",
                "idempotency_key": "k",
            },
            {
                "limits": None,
                "payee": PAYEE | {"handle": "other@sandbox"},
                "payments_to_payee": [],
                "mandates": [],
                "order": {
                    "id": "o1",
                    "merchant_handle": "shop@sandbox",
                    "status": "awaiting_payment",
                    "total_paise": 1000,
                    "payment_id": None,
                },
            },
        ),
        id="payments-wrong-merchant-for-the-order",
    ),
    pytest.param(
        "payments",
        request(
            "payments",
            "pay",
            {"payee": "shop@sandbox", "amount_paise": 1000, "idempotency_key": "k"},
            {"limits": None, "payee": PAYEE, "payments_to_payee": [], "mandates": []},
            {"allowed_tools": ["get_balance"]},
        ),
        id="payments-tool-not-delegated",
    ),
    pytest.param(
        "security",
        request(
            "security",
            "reimage_host",
            {"host_id": "web-01"},
            {
                "host": {"id": "web-01", "criticality": "low"},
                "evidence_collected": False,
                "alerts": [{"id": "a"}],
            },
        ),
        id="security-reimage-before-evidence",
    ),
    pytest.param(
        "security",
        request(
            "security",
            "block_indicator",
            {"kind": "domain", "value": "cdn.example.com"},
            {"business_indicators": [{"value": "cdn.example.com"}], "alerts": [{"id": "a"}]},
        ),
        id="security-block-a-supplier",
    ),
]
ASKED = [
    pytest.param(
        "payments",
        request(
            "payments",
            "pay",
            {"payee": "ghost@sandbox", "amount_paise": 1000, "idempotency_key": "k"},
            {
                "limits": None,
                "payee": {"handle": "ghost@sandbox", "exists": False},
                "payments_to_payee": [],
                "mandates": [],
            },
        ),
        id="payments-unknown-payee",
    ),
    pytest.param(
        "payments",
        request("payments", "refund_everything", {}, {}),
        id="payments-tool-the-policy-does-not-name",
    ),
]


def system_one_body() -> dict:
    return {
        "model": "jev-1.13.0",
        "answers": {
            "decision": {
                "choice": "allow",
                "confidence": 1.0,
                "probabilities": {"allow": 1.0},
                "override_hard_rules": True,
            }
        },
        "usage": {"input_tokens": 9},
    }


def transport(body: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))


class FakeChat:
    id = "fake/judge"

    def __init__(self, text: str) -> None:
        self.text = text

    def ask(self, messages: list[dict[str, str]]) -> ChatAnswer:
        return ChatAnswer(self.text, 10, 5)


class AllowScorer:
    model_id, revision = "fake/guard", "r1"

    def logprobs(self, text: str, continuations: Sequence[str]) -> list[float]:
        return [-50.0, -50.0, 0.0]  # block, ask_user, allow

    def generate(self, text: str, max_new_tokens: int) -> str:
        return "]}"


class Answer:
    """An adversarial backend: answers with whatever it is given, optionally late or by raising."""

    def __init__(
        self, name: str, answer: Any = None, *, delay: float = 0.0, raises: bool = False
    ) -> None:
        self.name, self.answer, self.delay, self.raises = name, answer, delay, raises
        self.calls = 0

    def decide(self, request: DecisionRequest) -> Any:
        self.calls += 1
        if self.raises:
            msg = "broken"
            raise RuntimeError(msg)
        if self.delay:
            time.sleep(self.delay)
        return self.answer


def allow_result(name: str = "adversary") -> BackendResult:
    return BackendResult(decision=Decision(**ALLOW), backend=name)


def backends() -> list[Any]:
    allow = allow_result()
    return [
        RulesOnlyBackend(),
        SystemOneBackend(
            "jev",
            endpoint="https://x.invalid/v1/systemone",
            model="jev-latest",
            client=transport(system_one_body()),
        ),
        SystemOneBackend(
            "clef",
            endpoint="https://x.invalid/ai/run",
            model="clef",
            client=transport({"result": system_one_body(), "success": True}),
        ),
        SystemOneBackend(
            "strands-decider",
            endpoint="http://127.0.0.1:1/v1/systemone",
            model=None,
            client=transport(system_one_body()),
        ),
        OpenAIDecisionsBackend(
            "openai-decisions",
            model="gpt-6-luna",
            api_key="not-a-key",
            client=transport(
                {
                    "model": "gpt-6-luna",
                    "answers": [
                        {
                            "name": "decision",
                            "type": "choice",
                            "choice": "allow",
                            "confidence": 1.0,
                            "probabilities": [{"value": "allow", "probability": 1.0}],
                        }
                    ],
                    "unknown": {"force": "allow"},
                }
            ),
        ),
        LLMJudge("judge", FakeChat(json.dumps(ALLOW))),
        LLMJudge("judge-extra-keys", FakeChat(json.dumps({**ALLOW, "override": True}))),
        GuardModelBackend("guard-model", AllowScorer()),
        CalibratedBackend(
            Answer("inner", allow),
            Calibration(
                backend="inner",
                temperature=1.0,
                threshold=0.0,
                target_missed=0.0,
                fitted_on="test",
                rows=1,
            ),
        ),
        Answer("always-allow", allow),
        Answer("late-allow", allow, delay=0.5),
        Answer("raises", raises=True),
        Answer("extra-fields", {"decision": ALLOW, "backend": "x", "force": "allow"}),
        Answer("bare-dict", {"decision": "allow"}),
        Answer(
            "constructed",
            BackendResult.model_construct(
                decision=Decision.model_construct(
                    decision="allow", reason_codes=[], confidence=7.0
                ),
                backend="x",
            ),
        ),
    ]


def guard_for(world: str, backend: Any) -> Guard:
    module = actions.get(world)
    policy = load_policy(f"{world}/default")
    policy = policy.model_copy(
        update={"model_check": policy.model_check.model_copy(update={"timeout_s": 0.1})}
    )
    return Guard(
        policy,
        backend,
        checks={r.id: r for r in module.rules},
        delegation=module.delegation,
        pack_limits=False,
    )


def decide(world: str, backend: Any, req: DecisionRequest) -> Any:
    module = actions.get(world)
    return guard_for(world, backend).decide(req, module.fields(req.model_dump(mode="json")))


@pytest.mark.parametrize(("world", "req"), BLOCKED)
def test_no_backend_turns_a_hard_block_into_an_allow(world: str, req: DecisionRequest) -> None:
    for backend in backends():
        outcome = decide(world, backend, req)
        assert outcome.decision == "block", backend.name
        assert outcome.layer == "hard", backend.name
        assert outcome.backend is None, f"{backend.name} was asked about a hard block"


@pytest.mark.parametrize(("world", "req"), ASKED)
def test_no_backend_turns_a_hard_ask_user_into_an_allow(world: str, req: DecisionRequest) -> None:
    for backend in backends():
        assert decide(world, backend, req).decision in ("ask_user", "block"), backend.name


def test_adversarial_answers_on_an_allowed_action_fail_closed() -> None:
    req = request(
        "payments",
        "pay",
        {"payee": "shop@sandbox", "amount_paise": 1000, "idempotency_key": "k"},
        {"limits": None, "payee": PAYEE, "payments_to_payee": [], "mandates": []},
    )
    expected = {
        "late-allow": "guard.timeout",
        "raises": "guard.backend_error",
        "extra-fields": "guard.invalid_result",
        "bare-dict": "guard.invalid_result",
        "constructed": "guard.invalid_result",
        "judge-extra-keys": "guard.schema",
    }
    for backend in backends():
        outcome = decide("payments", backend, req)
        if backend.name in expected:
            assert outcome.decision == "ask_user", backend.name
            assert outcome.layer == "fail_closed", backend.name
            assert outcome.reason_codes == [expected[backend.name]], backend.name
            assert outcome.confidence == 0.0
        else:
            assert outcome.decision == "allow", (backend.name, outcome.reason_codes)


SCENARIOS = [param.values[1] for param in BLOCKED + ASKED]


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    scenario=st.integers(min_value=0, max_value=len(SCENARIOS) - 1),
    decision=st.sampled_from(["allow", "block", "ask_user"]),
    confidence=st.floats(min_value=0, max_value=1),
    codes=st.lists(
        st.sampled_from(["payments.overspend", "security.false_containment", "x.y"]), unique=True
    ),
    error=st.sampled_from([None, "timeout", "provider_error"]),
)
def test_any_answer_is_never_looser_than_the_hard_rules(
    scenario: int, decision: str, confidence: float, codes: list[str], error: str | None
) -> None:
    req = SCENARIOS[scenario]
    world = req.world
    module = actions.get(world)
    fields = module.fields(req.model_dump(mode="json"))
    hard = guard_for(world, None).decide(req, fields)
    backend = Answer(
        "any",
        BackendResult(
            decision=Decision(decision=decision, reason_codes=codes, confidence=confidence),
            backend="any",
            error=error,
        ),
    )
    outcome = guard_for(world, backend).decide(req, fields)
    assert ORDER[outcome.decision] >= ORDER[hard.decision]
    if hard.decision == "block":
        assert outcome.decision == "block" and backend.calls == 0
