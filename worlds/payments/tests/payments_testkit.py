"""Shared helpers for the payments episode, tool and MCP tests."""

import copy
from typing import Any

from kit.clock import EPOCH, SimClock
from kit.rng import SeededRNG
from kit.types import GradeResult, StepResult, TaskSpec, ToolCall
from worlds.payments import world_spec
from worlds.payments.episode import PaymentsEpisode

USER = "asha@sandbox"
HOUR = 3_600
DAY = 24 * HOUR

PROFILES = {
    "honest": "honest-goods",
    "slow": "slow-goods",
    "wrong_item": "mixup-goods",
    "price_change": "drift-goods",
    "unclear_refund_policy": "vague-goods",
}
"""Profile -> merchant id. Every merchant sells the same two things, so tests can swap them."""


def handle(merchant_id: str) -> str:
    return f"{merchant_id}@sandbox"


def base_state(**overrides: Any) -> dict[str, Any]:
    """One user with ₹50,000, and one merchant per profile selling a kettle and a T-shirt."""
    merchants, products = [], []
    for profile, merchant_id in PROFILES.items():
        merchants.append(
            {
                "id": merchant_id,
                "name": merchant_id.replace("-", " ").title(),
                "handle": handle(merchant_id),
                "category": "home",
                "profile": profile,
            }
        )
        products += [
            {
                "id": f"{merchant_id}-kettle",
                "merchant_id": merchant_id,
                "name": "Steel Kettle",
                "category": "kitchen",
                "price_paise": 149_900,
                "stock": 5,
            },
            {
                "id": f"{merchant_id}-tee",
                "merchant_id": merchant_id,
                "name": "Cotton T-shirt",
                "category": "clothing",
                "price_paise": 59_900,
                "stock": 5,
                "variants": ["S", "M", "L"],
            },
        ]
    state: dict[str, Any] = {
        "user": USER,
        "rails": {
            "accounts": [
                {"handle": USER, "holder": "Asha Example", "balance_paise": 5_000_000},
                *(
                    {
                        "handle": handle(m),
                        "holder": m.replace("-", " ").title(),
                        "kind": "merchant",
                        "balance_paise": 1_000_000,
                    }
                    for m in PROFILES.values()
                ),
            ]
        },
        "commerce": {
            "merchants": merchants,
            "products": products,
            "plans": [
                {
                    "id": "honest-basic",
                    "merchant_id": "honest-goods",
                    "name": "Basic box",
                    "price_paise": 29_900,
                    "frequency": "monthly",
                },
                {
                    "id": "honest-plus",
                    "merchant_id": "honest-goods",
                    "name": "Plus box",
                    "price_paise": 49_900,
                    "frequency": "monthly",
                },
            ],
            "offers": [
                {"code": "SAVE10", "merchant_id": "honest-goods", "percent_off": 10},
                {
                    "code": "OLD50",
                    "merchant_id": "honest-goods",
                    "flat_off_paise": 5_000,
                    "expires_after_seconds": 0,
                },
                {
                    "code": "BIG200",
                    "merchant_id": "honest-goods",
                    "flat_off_paise": 20_000,
                    "min_cart_paise": 200_000,
                },
            ],
        },
    }
    for key, value in overrides.items():
        state[key] = value
    return state


def make_task(
    state: dict[str, Any] | None = None,
    *,
    allowed_tools: list[str] | None = None,
    max_steps: int = 200,
    seed: int = 7,
    task_id: str = "payments-flow",
) -> TaskSpec:
    return TaskSpec(
        id=task_id,
        version="v0.1",
        world="payments",
        category="multi",
        difficulty="medium",
        instruction="Scripted flow used by the Phase 2 tests.",
        initial_state=state if state is not None else base_state(),
        allowed_tools=allowed_tools or [],
        max_steps=max_steps,
        grader="payments_testkit:not_graded",
        seed=seed,
    )


def not_graded(state: Any, trace: Any, task: TaskSpec) -> GradeResult:
    """Phase 2 tests check state directly; graders arrive in Phase 3."""
    return GradeResult(passed=True, score=1.0)


class Session:
    """One episode driven by a scripted agent, one tool call at a time."""

    def __init__(self, task: TaskSpec | None = None, seed: int | None = None) -> None:
        self.task = task if task is not None else make_task()
        self.clock = SimClock(EPOCH)
        episode = world_spec().make_episode(
            self.task, self.clock, SeededRNG(self.task.seed if seed is None else seed)
        )
        assert isinstance(episode, PaymentsEpisode)
        self.episode = episode
        self.first = episode.reset(self.task)
        self.results: list[StepResult] = []

    def call(self, tool: str, **args: Any) -> StepResult:
        result = self.episode.step(ToolCall(tool=tool, args=args))
        self.results.append(result)
        return result

    def ok(self, tool: str, **args: Any) -> dict[str, Any]:
        """Call a tool that must not be refused; return its data."""
        result = self.call(tool, **args)
        error = result.observation.error
        assert error is None, f"{tool} refused: {error.code}: {error.message}"
        return copy.deepcopy(result.observation.data)

    def refused(self, tool: str, **args: Any) -> str:
        """Call a tool that must be refused; return the code."""
        error = self.call(tool, **args).observation.error
        assert error is not None, f"{tool} was not refused"
        return error.code

    def admin(self, service: str, resource: str, **filters: Any) -> Any:
        return self.episode.state[service].query(resource, **filters)

    def order(self, order_id: str) -> dict[str, Any]:
        return next(o for o in self.admin("commerce", "orders") if o["id"] == order_id)

    def balance(self, account: str) -> int:
        rows = self.admin("rails", "accounts")
        return next(int(r["balance_paise"]) for r in rows if account in (r["handle"], r["id"]))

    def buy(self, merchant_id: str, product: str = "kettle", variant: str | None = None) -> str:
        """Add one item, check out, pay the order. Returns the order id."""
        line: dict[str, Any] = {
            "merchant_id": merchant_id,
            "product_id": f"{merchant_id}-{product}",
        }
        if variant is not None:
            line["variant"] = variant
        self.ok("add_to_cart", quantity=1, **line)
        request = self.ok("checkout", merchant_id=merchant_id)["payment_request"]
        paid = self.ok(
            "pay",
            payee=request["payee"],
            amount_paise=request["amount_paise"],
            idempotency_key=f"buy-{request['order_id']}",
            order_id=request["order_id"],
        )
        assert paid["outcome"] == "paid", paid
        return str(request["order_id"])
