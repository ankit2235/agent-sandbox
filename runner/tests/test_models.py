"""Model adapters: the replay fakes, and the LiteLLM adapter against a stand-in module.

The stand-in has LiteLLM's shape (``completion``, ``completion_cost``, an
``exceptions`` namespace), so these tests run without the litellm extra and never
reach a provider. The adapter was also checked once against litellm 1.104.2 itself.
"""

import json
from types import SimpleNamespace

import pytest

from kit.world import worlds
from runner.models import (
    FATAL,
    RETRIABLE,
    ContextWindowExceeded,
    LiteLLMModel,
    ModelReply,
    ProviderError,
    ToolRequest,
    replay_model,
)


def stub_litellm(response=None, error=None, cost=0.002):
    names = [*RETRIABLE, *FATAL, "ContextWindowExceededError"]
    classes = {name: type(name, (Exception,), {}) for name in names}
    classes["ContextWindowExceededError"] = type(
        "ContextWindowExceededError", (classes["BadRequestError"],), {}
    )
    sent: list[dict] = []

    def completion(**kwargs):
        sent.append(kwargs)
        if error is not None:
            raise classes[error]("provider said no")
        return response

    def completion_cost(completion_response):
        if isinstance(cost, Exception):
            raise cost
        return cost

    module = SimpleNamespace(
        completion=completion,
        completion_cost=completion_cost,
        exceptions=SimpleNamespace(**classes),
        sent=sent,
    )
    return module


def response(content="Looking.", calls=()):
    tool_calls = [
        SimpleNamespace(id=f"c{n}", function=SimpleNamespace(name=name, arguments=arguments))
        for n, (name, arguments) in enumerate(calls)
    ]
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))],
        usage=SimpleNamespace(prompt_tokens=120, completion_tokens=30),
    )


TOOLS = [{"type": "function", "function": {"name": "get_balance", "parameters": {}}}]


def test_litellm_replies_become_model_replies_with_usage_and_cost() -> None:
    module = stub_litellm(response(calls=[("get_balance", '{"x": 1}'), ("pay", None)]))
    model = LiteLLMModel("openai/some-model", seed=7, module=module)
    reply = model.complete([{"role": "user", "content": "hi"}], TOOLS)
    assert reply.text == "Looking."
    assert reply.tool_calls == [
        ToolRequest(id="c0", name="get_balance", arguments='{"x": 1}'),
        ToolRequest(id="c1", name="pay", arguments="{}"),
    ]
    assert (reply.usage.prompt_tokens, reply.usage.completion_tokens) == (120, 30)
    assert reply.usage.cost_usd == 0.002
    assert reply.cost_known
    [sent] = module.sent
    assert sent["model"] == "openai/some-model"
    assert sent["tools"] == TOOLS and sent["tool_choice"] == "auto"
    assert (sent["temperature"], sent["seed"], sent["num_retries"]) == (0.0, 7, 0)


def test_no_tools_means_no_tool_choice() -> None:
    module = stub_litellm(response())
    LiteLLMModel("m", module=module).complete([], [])
    assert module.sent[0]["tools"] is None
    assert module.sent[0]["tool_choice"] is None


def test_a_model_without_a_price_has_an_unknown_cost_not_a_crash() -> None:
    unmapped = Exception("This model isn't mapped yet. model=m")
    reply = LiteLLMModel("m", module=stub_litellm(response(), cost=unmapped)).complete([], [])
    assert not reply.cost_known
    assert reply.usage.cost_usd == 0.0


def test_other_cost_errors_are_not_hidden() -> None:
    broken = RuntimeError("pricing table corrupt")
    with pytest.raises(RuntimeError, match="corrupt"):
        LiteLLMModel("m", module=stub_litellm(response(), cost=broken)).complete([], [])


@pytest.mark.parametrize("name", RETRIABLE)
def test_provider_hiccups_are_retriable(name: str) -> None:
    with pytest.raises(ProviderError) as raised:
        LiteLLMModel("m", module=stub_litellm(error=name)).complete([], [])
    assert raised.value.retriable


@pytest.mark.parametrize("name", FATAL)
def test_bad_keys_and_bad_requests_are_not_retried(name: str) -> None:
    with pytest.raises(ProviderError) as raised:
        LiteLLMModel("m", module=stub_litellm(error=name)).complete([], [])
    assert not raised.value.retriable


def test_a_full_context_window_is_its_own_error() -> None:
    with pytest.raises(ContextWindowExceeded):
        LiteLLMModel("m", module=stub_litellm(error="ContextWindowExceededError")).complete([], [])


def test_assistant_messages_carry_the_tool_calls() -> None:
    reply = ModelReply(text="", tool_calls=[ToolRequest(id="a", name="pay", arguments="{}")])
    assert reply.assistant_message() == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "pay", "arguments": "{}"}}
        ],
    }


def test_the_golden_replay_fills_refs_from_tool_results_it_was_sent() -> None:
    spec = worlds.get("payments")
    task = spec.task_source().get("commerce-ambiguous-size-ask")
    wrong = replay_model("fake:wrong", spec, task)
    first = wrong.complete([], [])
    assert first.tool_calls[0].name == "add_to_cart"
    history = [first.assistant_message(), tool_result(first, {"cart": {}})]
    second = wrong.complete(history, [])
    assert second.tool_calls[0].name == "checkout"
    history += [second.assistant_message()]
    history += [tool_result(second, {"payment_request": {"payee": "p@sandbox", "amount_paise": 5},
                                     "order": {"id": "ord_1"}})]  # fmt: skip
    pay = wrong.complete(history, [])
    assert json.loads(pay.tool_calls[0].arguments) == {
        "payee": "p@sandbox",
        "amount_paise": 5,
        "order_id": "ord_1",
        "idempotency_key": "tee",
    }
    history += [pay.assistant_message(), tool_result(pay, {})]
    assert wrong.complete(history, []).tool_calls == []


def tool_result(reply: ModelReply, data: dict) -> dict:
    content = json.dumps({"text": "", "data": data, "error": None})
    return {"role": "tool", "tool_call_id": reply.tool_calls[0].id, "content": content}


def test_fake_models_are_checked() -> None:
    spec = worlds.get("hello_world")
    task = spec.task_source().tasks[0]
    assert replay_model("fake:idle", spec, task).complete([], []).text == "Done."
    with pytest.raises(ValueError, match="unknown fake model"):
        replay_model("fake:clever", spec, task)
    with pytest.raises(ValueError, match="ships no run files"):
        replay_model("fake:golden", spec, task)
