"""The baselines: rules, the LLM judge, System One and OpenAI Decisions, and their configs.

No test calls a provider: HTTP backends get an ``httpx.MockTransport``, the judge a
``runner.models.FakeModel``.
"""

import json
from pathlib import Path

import httpx
import pytest

from guard.backends.config import build_backend, configs, load_config
from guard.backends.llm_judge import LLMJudge, system_prompt
from guard.backends.openai_decisions import OpenAIDecisionsBackend
from guard.backends.rules_only import RulesOnlyBackend
from guard.backends.system_one import ConfigError, SystemOneBackend
from guard.decision import ActionCall, DecisionRequest
from runner.guarding import ChatAdapter
from runner.models import ContextWindowExceeded, FakeModel, ModelReply, ProviderError
from training.data.build import read_split

NOW = "2026-01-01T09:00:00Z"


def _payments(action: dict, state: dict) -> DecisionRequest:
    return DecisionRequest(
        world="payments",
        instruction="Pay the bill.",
        state=state,
        action=ActionCall(**action),
        sim_time=NOW,
    )


# ---------------------------------------------------------------- rules
def test_rules_allow_a_payment_that_breaks_nothing() -> None:
    request = _payments(
        {"tool": "pay", "args": {"payee": "a@sandbox", "amount_paise": 100}},
        {"limits": {"per_txn_paise": 1000}, "payee": {"exists": True}, "payments_to_payee": []},
    )
    result = RulesOnlyBackend().decide(request)
    assert result.decision.decision == "allow"
    assert result.decision.reason_codes == []


def test_rules_block_over_the_cap_and_ask_for_an_unknown_payee() -> None:
    over = _payments(
        {"tool": "pay", "args": {"payee": "a@sandbox", "amount_paise": 5000}},
        {"limits": {"per_txn_paise": 1000}, "payee": {"exists": True}},
    )
    assert RulesOnlyBackend().decide(over).decision.reason_codes == [
        "payments.rule.over-per-txn-cap"
    ]
    unknown = _payments(
        {"tool": "pay", "args": {"payee": "x@sandbox", "amount_paise": 1}},
        {"payee": {"exists": False}},
    )
    decided = RulesOnlyBackend().decide(unknown).decision
    assert decided.decision == "ask_user"
    assert decided.reason_codes == ["payments.rule.unknown-payee"]


def test_rules_block_a_repeat_payment_within_a_day() -> None:
    request = _payments(
        {
            "tool": "pay",
            "args": {"payee": "a@sandbox", "amount_paise": 250, "idempotency_key": "n"},
        },
        {
            "payee": {"exists": True},
            "payments_to_payee": [
                {
                    "amount_paise": 250,
                    "status": "success",
                    "idempotency_key": "o",
                    "created_at": "2026-01-01T08:00:00.000000Z",
                }
            ],
        },
    )
    assert (
        "payments.rule.repeat-payment" in RulesOnlyBackend().decide(request).decision.reason_codes
    )


def test_rules_block_reimaging_before_evidence_and_blocking_a_supplier() -> None:
    reimage = DecisionRequest(
        world="security",
        instruction="Contain it.",
        state={
            "host": {"criticality": "low"},
            "evidence_collected": False,
            "alerts": [{"id": "A"}],
        },
        action=ActionCall(tool="reimage_host", args={"host_id": "ws-001"}),
        sim_time=NOW,
    )
    assert RulesOnlyBackend().decide(reimage).decision.reason_codes == [
        "security.rule.reimage-before-evidence"
    ]
    supplier = DecisionRequest(
        world="security",
        instruction="Contain it.",
        state={"business_indicators": [{"value": "x.example.net"}], "alerts": []},
        action=ActionCall(
            tool="block_indicator", args={"kind": "domain", "value": "x.example.net"}
        ),
        sim_time=NOW,
    )
    assert RulesOnlyBackend().decide(supplier).decision.decision == "block"


def test_rules_run_on_real_rows(small_build: Path) -> None:
    rows = read_split(small_build, "test")
    decisions = {RulesOnlyBackend().decide(row.request).decision.decision for row in rows}
    assert decisions <= {"allow", "block", "ask_user"}


# ---------------------------------------------------------------- judge
def _request() -> DecisionRequest:
    return _payments({"tool": "pay", "args": {"payee": "a@sandbox"}}, {})


def test_the_judge_reads_one_json_decision() -> None:
    reply = '{"decision": "block", "reason_codes": ["payments.overspend"], "confidence": 0.8}'
    model = FakeModel([ModelReply(text=reply)], model_id="fake:judge")
    result = LLMJudge("judge-a", ChatAdapter(model)).decide(_request())
    assert result.decision.decision == "block"
    assert result.model == "fake:judge"
    assert result.model_version == "judge-v1"
    [messages] = model.calls
    assert "payments.overspend" in messages[0]["content"]
    assert "Attempted action:" in messages[1]["content"]


@pytest.mark.parametrize(
    ("turn", "error"),
    [
        (ModelReply(text="I would block this."), "invalid_json"),
        (ModelReply(text='{"decision": "deny", "reason_codes": [], "confidence": 1}'), "schema"),
        (ProviderError("rate limited", retriable=True), "provider_error"),
        (ContextWindowExceeded("too long"), "context_window"),
    ],
)
def test_the_judge_fails_closed(turn: object, error: str) -> None:
    result = LLMJudge("judge-a", ChatAdapter(FakeModel([turn]))).decide(_request())
    assert result.decision.decision == "ask_user"
    assert result.decision.confidence == 0.0
    assert result.error == error


def test_the_judge_prompt_fills_every_placeholder() -> None:
    text = system_prompt("v1", {"payments.overspend": "Paid too much."})
    assert "{" not in text.replace('{"decision"', "")
    with pytest.raises(FileNotFoundError):
        system_prompt("v9", {})


# ------------------------------------------------------------- system one
def _system_one(handler, **kwargs) -> SystemOneBackend:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return SystemOneBackend(
        "jev", endpoint="https://api.test/v1/systemone", model="jev-latest", client=client, **kwargs
    )


def test_system_one_sends_one_choice_and_a_noul_per_reason() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        seen["auth"] = request.headers.get("authorization")
        answers = {
            "decision": {
                "type": "choice",
                "choice": "block",
                "confidence": 0.7,
                "probabilities": {"allow": 0.1, "block": 0.7, "ask_user": 0.2},
            }
        }
        answers |= {
            f"reason_{i}": {"type": "noul", "noul": 0.9 if i == 1 else 0.1} for i in range(7)
        }
        return httpx.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": answers,
                "usage": {"input_tokens": 900, "output_tokens": 8},
            },
        )

    result = _system_one(handler, api_key="k").decide(_request())
    assert seen["model"] == "jev-latest"
    assert seen["auth"] == "Bearer k"
    assert set(seen["questions"]["decision"]["criteria"]) == {"allow", "block", "ask_user"}
    assert sum(q["type"] == "noul" for q in seen["questions"].values()) == 7
    assert result.decision.decision == "block"
    assert result.decision.reason_codes == ["payments.fake_discount"]
    assert result.model_version == "jev-1.13.0"
    assert result.input_tokens == 900
    assert result.probabilities == {"allow": 0.1, "block": 0.7, "ask_user": 0.2}


def test_system_one_accepts_a_cloudflare_envelope() -> None:
    body = {"result": {"answers": {"decision": {"choice": "allow", "confidence": 0.9}}}}
    result = _system_one(lambda r: httpx.Response(200, json=body)).decide(_request())
    assert result.decision.decision == "allow"
    assert result.decision.reason_codes == []


@pytest.mark.parametrize(
    ("handler", "error"),
    [
        (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=r)), "timeout"),
        (lambda r: httpx.Response(500, json={}), "provider_error"),
        (lambda r: httpx.Response(200, content=b"<html>"), "invalid_response"),
        (
            lambda r: httpx.Response(200, json={"answers": {"decision": {"choice": "maybe"}}}),
            "invalid_response",
        ),
        (lambda r: httpx.Response(200, json=[1, 2]), "invalid_response"),
    ],
)
def test_system_one_fails_closed(handler, error: str) -> None:
    result = _system_one(handler).decide(_request())
    assert result.decision.decision == "ask_user"
    assert result.error == error


# ------------------------------------------------------- openai decisions
def _decisions(handler) -> OpenAIDecisionsBackend:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenAIDecisionsBackend(
        "openai-decisions", model="gpt-6-luna", api_key="k", client=client
    )


def test_openai_decisions_sends_choices_and_predicates() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "answers": [
                    {
                        "type": "choice",
                        "name": "decision",
                        "choice": "ask_user",
                        "confidence": 0.6,
                        "probabilities": [
                            {"value": "allow", "probability": 0.3},
                            {"value": "block", "probability": 0.1},
                            {"value": "ask_user", "probability": 0.6},
                        ],
                    },
                    {"type": "predicate", "name": "reason_5", "probability": 0.8},
                ],
                "usage": {"input_tokens": 1200},
            },
        )

    result = _decisions(handler).decide(_request())
    choice = seen["questions"][0]
    assert choice["type"] == "choice"
    assert {c["value"] for c in choice["choices"]} == {"allow", "block", "ask_user"}
    assert all(c["description"] for c in choice["choices"])
    assert seen["model"] == "gpt-6-luna"
    assert result.decision.decision == "ask_user"
    assert result.decision.reason_codes == ["payments.unconfirmed_payment"]
    assert result.input_tokens == 1200
    assert result.output_tokens is None


def test_openai_decisions_treats_a_refusal_as_fail_closed() -> None:
    body = {"answers": [{"type": "refusal", "name": "decision"}]}
    result = _decisions(lambda r: httpx.Response(200, json=body)).decide(_request())
    assert result.error == "refusal"
    assert result.decision.decision == "ask_user"


# ---------------------------------------------------------------- configs
def test_every_packaged_config_validates() -> None:
    names = configs()
    assert {"rules_only", "judge-a", "judge-b", "jev", "clef", "openai-decisions"} <= set(names)
    for name in names:
        assert load_config(name).name == name


def test_the_judges_have_no_default_model_and_are_not_the_decisions_model() -> None:
    for name in ("judge-a", "judge-b"):
        config = load_config(name)
        assert config.model is None
        with pytest.raises(ConfigError, match="no default"):
            build_backend(config)


def test_set_fills_a_field_and_rejects_a_malformed_one() -> None:
    config = load_config("judge-a", ["model=provider/some-model", "timeout_s=30"])
    assert config.model == "provider/some-model"
    assert config.timeout_s == 30
    with pytest.raises(ConfigError):
        load_config("judge-a", ["model"])


def test_a_missing_key_or_account_stops_the_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="TYPESAFE_API_KEY"):
        build_backend(load_config("jev"))
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.setenv("CLOUDFLARE_AUTH_TOKEN", "t")
    with pytest.raises(ConfigError, match="CLOUDFLARE_ACCOUNT_ID"):
        build_backend(load_config("clef"))


def test_the_clef_endpoint_gets_the_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDFLARE_AUTH_TOKEN", "t")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct123")
    backend = build_backend(load_config("clef"))
    assert isinstance(backend, SystemOneBackend)
    assert backend.endpoint.endswith("/accounts/acct123/ai/run/@cf/cloudflare/clef")


def test_an_unknown_config_lists_the_packaged_ones() -> None:
    with pytest.raises(FileNotFoundError, match="packaged ones"):
        load_config("nope")
