"""Write the guard's shared test vectors (``guard/test-vectors/*.json``), or check them.

Both SDKs run the same vectors: canonical JSON and its hashes, valid and invalid
policies, layered decisions and decision-record chains. Every decision case below states
its expected decision, layer and reason codes by hand; the Python engine must agree
before a file is written, and the TypeScript SDK must agree when it runs them.

    uv run python scripts/guard_vectors.py           # rewrite the files
    uv run python scripts/guard_vectors.py --check   # exit 1 if a file would change
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from pathlib import Path
from typing import Any

from guard.canonical import CanonicalError, canonical, digest
from guard.decision import BackendResult, DecisionRequest
from guard.engine import Guard
from guard.policy import PolicyError, parse_policy
from guard.records import DecisionLog
from guard.rules import Fields

OUT = Path(__file__).resolve().parents[1] / "guard" / "test-vectors"
T0 = "2026-01-01T09:00:00Z"
"""A Thursday, as the kit's EPOCH."""

# ------------------------------------------------------------------ canonical
CANONICAL: list[tuple[str, Any]] = [
    ("integer", 42),
    ("negative integer", -7),
    ("largest safe integer", 9007199254740991),
    ("2**53, exactly a double", 9007199254740992),
    ("10**21 as an integer", 10**21),
    ("float with an integer value", 5.0),
    ("negative zero", -0.0),
    ("one tenth", 0.1),
    ("a third", 1 / 3),
    ("small exponent, Python writes 1e-07", 1e-07),
    ("micro, Python writes 1e-06", 1e-06),
    ("below 1e-6", 1.5e-07),
    ("1e20, Python writes 1e+20", 1e20),
    ("1e21", 1e21),
    ("large exponent", 1.7976931348623157e308),
    ("smallest subnormal", 5e-324),
    ("latency", 0.123),
    ("confidence", 0.8749999999999999),
    ("true, false, null", [True, False, None]),
    ("escapes", 'quote " backslash \\ newline \n tab \t bell \u0007 del \u007f'),
    ("non-ASCII kept", "₹2,000 café 日本"),
    ("emoji", "pay 😀"),
    ("keys sorted by UTF-16 code units", {"￿": 1, "😀": 2, "a": 3, "B": 4, "": 5}),
    ("nested", {"b": [1, {"d": 1.0, "c": None}], "a": {"z": [], "y": {}}}),
]
NOT_CANONICAL: list[tuple[str, Any]] = [
    ("2**53 + 1, not exactly a double", 9007199254740993),
    ("-(2**53 + 1)", -9007199254740993),
]
"""Python-only: a JavaScript JSON parser rounds these to a double while parsing, so the
TypeScript SDK never sees them (and cannot be given them)."""


def canonical_vectors() -> dict[str, Any]:
    """Values, their canonical text and hash; and values with no canonical form."""
    cases = [
        {"name": name, "value": value, "canonical": canonical(value), "sha256": digest(value)}
        for name, value in CANONICAL
    ]
    for name, value in NOT_CANONICAL:
        try:
            canonical(value)
        except CanonicalError:
            continue
        msg = f"{name} should have no canonical form"
        raise AssertionError(msg)
    return {
        "format": "guard-vectors-canonical-v1",
        "cases": cases,
        "invalid": [
            {"name": name, "value": value, "python_only": True} for name, value in NOT_CANONICAL
        ],
    }


# ------------------------------------------------------------------- policies
def policy(actions: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """A minimal valid policy around ``actions``."""
    return {"format": "guard-policy-v1", "id": "vectors", "version": 1, "actions": actions, **extra}


VALID_POLICIES: list[tuple[str, dict[str, Any]]] = [
    ("minimal", policy({})),
    (
        "every rule",
        policy(
            {
                "pay": {
                    "budget": {"per_action": 100000, "daily": 300000, "monthly": 1000000},
                    "confirm_above": 50000,
                    "counterparties": {
                        "allow": ["shop@sandbox"],
                        "block": ["scam@sandbox"],
                        "confirm": ["new@sandbox"],
                        "unlisted": "block",
                    },
                    "categories": {"block": ["gambling"], "confirm": ["travel"]},
                    "mandate": {"required": False, "allow": ["mandate-1"]},
                    "time_windows": {
                        "windows": [{"days": ["mon", "fri"], "start": "09:00", "end": "18:30"}],
                        "outside": "ask_user",
                    },
                    "checks": ["vectors.rule.block-check"],
                    "model_check": True,
                }
            },
            world="vectors",
            description="Every field once.",
            default="block",
            model_check={"min_confidence": 0.75, "timeout_s": 2.5},
        ),
    ),
    (
        "a window to midnight",
        policy(
            {
                "t": {
                    "time_windows": {
                        "windows": [{"days": ["sun"], "start": "00:00", "end": "24:00"}]
                    }
                }
            }
        ),
    ),
]
INVALID_POLICIES: list[tuple[str, Any]] = [
    ("not an object", [1, 2]),
    ("wrong format", {**policy({}), "format": "guard-policy-v2"}),
    ("no actions", {key: value for key, value in policy({}).items() if key != "actions"}),
    ("version zero", {**policy({}), "version": 0}),
    ("version as text", {**policy({}), "version": "1"}),
    ("unknown top-level key", {**policy({}), "allow_everything": True}),
    ("unknown rule key", policy({"pay": {"override": True}})),
    ("negative budget", policy({"pay": {"budget": {"per_action": -1}}})),
    ("fractional budget", policy({"pay": {"budget": {"daily": 10.5}}})),
    ("budget above 2**53 - 1", policy({"pay": {"budget": {"monthly": 9007199254740993}}})),
    ("unknown default", {**policy({}), "default": "maybe"}),
    ("confidence above one", {**policy({}), "model_check": {"min_confidence": 1.5}}),
    ("zero timeout", {**policy({}), "model_check": {"timeout_s": 0}}),
    ("unlisted allow", policy({"pay": {"counterparties": {"unlisted": "allow"}}})),
    ("repeated value", policy({"pay": {"counterparties": {"block": ["a", "a"]}}})),
    (
        "unknown day",
        policy(
            {
                "t": {
                    "time_windows": {
                        "windows": [{"days": ["someday"], "start": "09:00", "end": "10:00"}]
                    }
                }
            }
        ),
    ),
    (
        "bad time",
        policy(
            {
                "t": {
                    "time_windows": {
                        "windows": [{"days": ["mon"], "start": "9:00", "end": "10:00"}]
                    }
                }
            }
        ),
    ),
    (
        "window ends before it starts",
        policy(
            {
                "t": {
                    "time_windows": {
                        "windows": [{"days": ["mon"], "start": "18:00", "end": "09:00"}]
                    }
                }
            }
        ),
    ),
    ("no windows", policy({"t": {"time_windows": {"windows": []}}})),
    ("check id not dotted", policy({"pay": {"checks": ["nodot"]}})),
    ("bad id", {**policy({}), "id": "Has Spaces"}),
]


def policy_vectors() -> dict[str, Any]:
    """Valid policies with their hashes; invalid ones."""
    valid = [
        {"name": name, "policy": raw, "hash": parse_policy(raw).hash}
        for name, raw in VALID_POLICIES
    ]
    for name, raw in INVALID_POLICIES:
        try:
            parse_policy(raw)
        except PolicyError:
            continue
        msg = f"policy {name!r} should be invalid"
        raise AssertionError(msg)
    return {
        "format": "guard-vectors-policies-v1",
        "valid": valid,
        "invalid": [{"name": name, "policy": raw} for name, raw in INVALID_POLICIES],
    }


# ------------------------------------------------------------------ decisions
def request(tool: str = "pay", *, at: str = T0, **extra: Any) -> dict[str, Any]:
    """A request in its canonical JSON form (every default written out)."""
    raw = {
        "world": "vectors",
        "instruction": "Pay the shop for the kettle, under 1,000.",
        "action": {"tool": tool, "args": {"payee": "shop@sandbox", "amount_paise": 70000}},
        "sim_time": at,
        **extra,
    }
    return DecisionRequest.model_validate(raw).model_dump(mode="json")


def answer(
    decision: str, confidence: float = 1.0, codes: list[str] | None = None, **extra: Any
) -> dict[str, Any]:
    """A backend's answer as JSON."""
    return {
        "decision": {"decision": decision, "reason_codes": codes or [], "confidence": confidence},
        "backend": "vectors-backend",
        "model": "vectors-model",
        "model_version": "v1",
        **extra,
    }


CHECKS = {
    "vectors.rule.block-check": "block",
    "vectors.rule.ask-check": "ask_user",
}
PAY = {"budget": {"per_action": 100000}}
FAST = {"model_check": {"timeout_s": 0.05}}

# name, policy, inputs, expected (decision, layer, codes, confidence)
DECISIONS: list[tuple[str, dict[str, Any], dict[str, Any], tuple[str, str, list[str], float]]] = [
    (
        "nothing broken, no backend",
        policy({"pay": PAY}),
        {"fields": {"amount": 70000}},
        ("allow", "hard", [], 1.0),
    ),
    (
        "over the per-action budget",
        policy({"pay": PAY}),
        {"fields": {"amount": 100001}},
        ("block", "hard", ["guard.policy.over-per-action-budget"], 1.0),
    ),
    (
        "exactly the per-action budget",
        policy({"pay": PAY}),
        {"fields": {"amount": 100000}},
        ("allow", "hard", [], 1.0),
    ),
    (
        "over the daily budget with today's history",
        policy({"pay": {"budget": {"daily": 100000}}}),
        {"fields": {"amount": 40000}, "history": [["pay", 70000, "2026-01-01T08:00:00Z"]]},
        ("block", "hard", ["guard.policy.over-daily-budget"], 1.0),
    ),
    (
        "yesterday does not count against today",
        policy({"pay": {"budget": {"daily": 100000}}}),
        {"fields": {"amount": 40000}, "history": [["pay", 70000, "2025-12-31T23:59:00Z"]]},
        ("allow", "hard", [], 1.0),
    ),
    (
        "another tool does not count",
        policy({"pay": {"budget": {"daily": 100000}}}),
        {"fields": {"amount": 40000}, "history": [["refund", 70000, "2026-01-01T08:00:00Z"]]},
        ("allow", "hard", [], 1.0),
    ),
    (
        "over the monthly budget",
        policy({"pay": {"budget": {"monthly": 100000}}}),
        {
            "fields": {"amount": 40000},
            "request": request(at="2026-01-20T10:00:00Z"),
            "history": [["pay", 70000, "2026-01-02T08:00:00Z"]],
        },
        ("block", "hard", ["guard.policy.over-monthly-budget"], 1.0),
    ),
    (
        "history in another time zone, same UTC day",
        policy({"pay": {"budget": {"daily": 100000}}}),
        {"fields": {"amount": 40000}, "history": [["pay", 70000, "2026-01-01T05:30:00+05:30"]]},
        ("block", "hard", ["guard.policy.over-daily-budget"], 1.0),
    ),
    (
        "above the confirm threshold",
        policy({"pay": {"confirm_above": 50000}}),
        {"fields": {"amount": 70000}},
        ("ask_user", "hard", ["guard.policy.confirm-above"], 1.0),
    ),
    (
        "a budget and no amount",
        policy({"pay": PAY}),
        {"fields": {}},
        ("ask_user", "hard", ["guard.policy.missing-amount"], 1.0),
    ),
    (
        "blocked counterparty",
        policy({"pay": {"counterparties": {"block": ["scam@sandbox"]}}}),
        {"fields": {"counterparty": "scam@sandbox"}},
        ("block", "hard", ["guard.policy.counterparty-blocked"], 1.0),
    ),
    (
        "unlisted counterparty asks by default",
        policy({"pay": {"counterparties": {"allow": ["shop@sandbox"]}}}),
        {"fields": {"counterparty": "other@sandbox"}},
        ("ask_user", "hard", ["guard.policy.counterparty-unlisted"], 1.0),
    ),
    (
        "unlisted counterparty blocked",
        policy({"pay": {"counterparties": {"allow": ["shop@sandbox"], "unlisted": "block"}}}),
        {"fields": {"counterparty": "other@sandbox"}},
        ("block", "hard", ["guard.policy.counterparty-unlisted"], 1.0),
    ),
    (
        "listed counterparty",
        policy({"pay": {"counterparties": {"allow": ["shop@sandbox"]}}}),
        {"fields": {"counterparty": "shop@sandbox"}},
        ("allow", "hard", [], 1.0),
    ),
    (
        "counterparty rule and none named",
        policy({"pay": {"counterparties": {"block": ["x"]}}}),
        {"fields": {}},
        ("ask_user", "hard", ["guard.policy.missing-counterparty"], 1.0),
    ),
    (
        "confirm-first category",
        policy({"pay": {"categories": {"confirm": ["travel"]}}}),
        {"fields": {"category": "travel"}},
        ("ask_user", "hard", ["guard.policy.category-confirm"], 1.0),
    ),
    (
        "blocked category wins over confirm",
        policy({"pay": {"categories": {"block": ["travel"], "confirm": ["travel"]}}}),
        {"fields": {"category": "travel"}},
        ("block", "hard", ["guard.policy.category-blocked"], 1.0),
    ),
    (
        "mandate required and none named",
        policy({"pay": {"mandate": {"required": True}}}),
        {"fields": {}},
        ("block", "hard", ["guard.policy.mandate-required"], 1.0),
    ),
    (
        "mandate not allowed",
        policy({"pay": {"mandate": {"allow": ["mandate-1"]}}}),
        {"fields": {"mandate": "mandate-2"}},
        ("block", "hard", ["guard.policy.mandate-not-allowed"], 1.0),
    ),
    (
        "inside the time window",
        policy(
            {
                "pay": {
                    "time_windows": {
                        "windows": [{"days": ["thu"], "start": "09:00", "end": "17:00"}]
                    }
                }
            }
        ),
        {"fields": {}},
        ("allow", "hard", [], 1.0),
    ),
    (
        "at the window's end",
        policy(
            {
                "pay": {
                    "time_windows": {
                        "windows": [{"days": ["thu"], "start": "08:00", "end": "09:00"}]
                    }
                }
            }
        ),
        {"fields": {}},
        ("ask_user", "hard", ["guard.policy.outside-time-window"], 1.0),
    ),
    (
        "outside the window, blocked",
        policy(
            {
                "pay": {
                    "time_windows": {
                        "windows": [{"days": ["fri"], "start": "09:00", "end": "17:00"}],
                        "outside": "block",
                    }
                }
            }
        ),
        {"fields": {}},
        ("block", "hard", ["guard.policy.outside-time-window"], 1.0),
    ),
    (
        "window read in UTC",
        policy(
            {
                "pay": {
                    "time_windows": {
                        "windows": [{"days": ["thu"], "start": "09:00", "end": "17:00"}]
                    }
                }
            }
        ),
        {"fields": {}, "request": request(at="2026-01-01T14:00:00+05:30")},
        ("ask_user", "hard", ["guard.policy.outside-time-window"], 1.0),
    ),
    (
        "tool the policy does not name",
        policy({"pay": PAY}),
        {"request": request("refund"), "fields": {}},
        ("ask_user", "default", ["guard.policy.uncovered-tool"], 1.0),
    ),
    (
        "tool the policy does not name, default allow",
        policy({"pay": PAY}, default="allow"),
        {"request": request("refund"), "fields": {}, "backend": {"result": answer("block")}},
        ("allow", "default", [], 1.0),
    ),
    (
        "tool the task did not delegate",
        policy({"pay": {}}),
        {"request": request(policy={"allowed_tools": ["refund"]}), "fields": {}},
        ("block", "hard", ["guard.policy.tool-not-delegated"], 1.0),
    ),
    (
        "world check blocks",
        policy({"pay": {"checks": ["vectors.rule.block-check", "vectors.rule.ask-check"]}}),
        {"fields": {}, "fires": ["vectors.rule.block-check", "vectors.rule.ask-check"]},
        ("block", "hard", ["vectors.rule.block-check", "vectors.rule.ask-check"], 1.0),
    ),
    (
        "world check asks",
        policy({"pay": {"checks": ["vectors.rule.ask-check"]}}),
        {"fields": {}, "fires": ["vectors.rule.ask-check"]},
        ("ask_user", "hard", ["vectors.rule.ask-check"], 1.0),
    ),
    (
        "delegation stricter than the policy",
        policy({"pay": {"budget": {"per_action": 100000}}}),
        {"fields": {"amount": 70000}, "delegation": {"pay": {"budget": {"per_action": 50000}}}},
        ("block", "hard", ["guard.policy.over-per-action-budget"], 1.0),
    ),
    (
        "a delegation cannot loosen the policy",
        policy({"pay": {"budget": {"per_action": 50000}}}),
        {"fields": {"amount": 70000}, "delegation": {"pay": {"budget": {"per_action": 900000}}}},
        ("block", "hard", ["guard.policy.over-per-action-budget"], 1.0),
    ),
    (
        "sandbox: the policy's numbers left out",
        policy({"pay": {"budget": {"per_action": 10000}, "confirm_above": 10000}}),
        {
            "fields": {"amount": 70000},
            "pack_limits": False,
            "delegation": {"pay": {"budget": {"per_action": 900000}}},
        },
        ("allow", "hard", [], 1.0),
    ),
    (
        "sandbox: the task's own limit still holds",
        policy({"pay": {"budget": {"per_action": 10000}}}),
        {
            "fields": {"amount": 70000},
            "pack_limits": False,
            "delegation": {"pay": {"budget": {"per_action": 50000}}},
        },
        ("block", "hard", ["guard.policy.over-per-action-budget"], 1.0),
    ),
    (
        "same code, stricter decision kept",
        policy({"pay": {"counterparties": {"allow": ["a"]}}}),
        {
            "fields": {"counterparty": "b"},
            "delegation": {"pay": {"counterparties": {"allow": ["a"], "unlisted": "block"}}},
        },
        ("block", "hard", ["guard.policy.counterparty-unlisted"], 1.0),
    ),
    (
        "backend allows",
        policy({"pay": PAY}),
        {"fields": {"amount": 70000}, "backend": {"result": answer("allow", 0.9)}},
        ("allow", "backend", [], 0.9),
    ),
    (
        "backend cannot unblock a hard block",
        policy({"pay": PAY}),
        {"fields": {"amount": 200000}, "backend": {"result": answer("allow", 1.0)}},
        ("block", "hard", ["guard.policy.over-per-action-budget"], 1.0),
    ),
    (
        "backend cannot allow a hard ask_user",
        policy({"pay": {"confirm_above": 50000}}),
        {"fields": {"amount": 70000}, "backend": {"result": answer("allow", 1.0)}},
        ("ask_user", "hard", ["guard.policy.confirm-above"], 1.0),
    ),
    (
        "backend makes a hard ask_user a block",
        policy({"pay": {"confirm_above": 50000}}),
        {
            "fields": {"amount": 70000},
            "backend": {"result": answer("block", 0.8, ["vectors.wrong_payee"])},
        },
        ("block", "backend", ["guard.policy.confirm-above", "vectors.wrong_payee"], 0.8),
    ),
    (
        "backend blocks",
        policy({"pay": PAY}),
        {
            "fields": {"amount": 70000},
            "backend": {"result": answer("block", 0.9, ["vectors.planted_instruction"])},
        },
        ("block", "backend", ["vectors.planted_instruction"], 0.9),
    ),
    (
        "low confidence allow asks",
        policy({"pay": PAY}, model_check={"min_confidence": 0.7}),
        {"fields": {"amount": 70000}, "backend": {"result": answer("allow", 0.6)}},
        ("ask_user", "backend", ["guard.low_confidence"], 0.6),
    ),
    (
        "low confidence block asks",
        policy({"pay": PAY}, model_check={"min_confidence": 0.7}),
        {"fields": {"amount": 70000}, "backend": {"result": answer("block", 0.5, ["vectors.x"])}},
        ("ask_user", "backend", ["vectors.x", "guard.low_confidence"], 0.5),
    ),
    (
        "the threshold itself is enough",
        policy({"pay": PAY}, model_check={"min_confidence": 0.7}),
        {"fields": {"amount": 70000}, "backend": {"result": answer("allow", 0.7)}},
        ("allow", "backend", [], 0.7),
    ),
    (
        "backend times out",
        policy({"pay": PAY}, **FAST),
        {"fields": {"amount": 70000}, "backend": {"late": answer("allow", 1.0)}},
        ("ask_user", "fail_closed", ["guard.timeout"], 0.0),
    ),
    (
        "late allow cannot unblock",
        policy({"pay": PAY}, **FAST),
        {"fields": {"amount": 200000}, "backend": {"late": answer("allow", 1.0)}},
        ("block", "hard", ["guard.policy.over-per-action-budget"], 1.0),
    ),
    (
        "late allow cannot allow a hard ask_user",
        policy({"pay": {"confirm_above": 50000}}, **FAST),
        {"fields": {"amount": 70000}, "backend": {"late": answer("allow", 1.0)}},
        ("ask_user", "hard", ["guard.policy.confirm-above"], 1.0),
    ),
    (
        "backend raises",
        policy({"pay": PAY}),
        {"fields": {"amount": 70000}, "backend": {"raises": True}},
        ("ask_user", "fail_closed", ["guard.backend_error"], 0.0),
    ),
    (
        "backend fails closed itself",
        policy({"pay": PAY}),
        {
            "fields": {"amount": 70000},
            "backend": {
                "result": {
                    **answer("ask_user", 0.0, ["guard.provider_error"]),
                    "error": "provider_error",
                }
            },
        },
        ("ask_user", "fail_closed", ["guard.provider_error"], 0.0),
    ),
    (
        "unknown field in the decision",
        policy({"pay": PAY}),
        {
            "fields": {"amount": 70000},
            "backend": {
                "invalid": {
                    **answer("allow"),
                    "decision": {
                        "decision": "allow",
                        "reason_codes": [],
                        "confidence": 1.0,
                        "override_hard_rules": True,
                    },
                }
            },
        },
        ("ask_user", "fail_closed", ["guard.invalid_result"], 0.0),
    ),
    (
        "unknown field beside the decision",
        policy({"pay": PAY}),
        {
            "fields": {"amount": 70000},
            "backend": {"invalid": {**answer("allow"), "force": "allow"}},
        },
        ("ask_user", "fail_closed", ["guard.invalid_result"], 0.0),
    ),
    (
        "unknown field under a hard block",
        policy({"pay": PAY}),
        {
            "fields": {"amount": 200000},
            "backend": {"invalid": {**answer("allow"), "force": "allow"}},
        },
        ("block", "hard", ["guard.policy.over-per-action-budget"], 1.0),
    ),
    (
        "not a decision at all",
        policy({"pay": PAY}),
        {"fields": {"amount": 70000}, "backend": {"invalid": {"decision": "allow"}}},
        ("ask_user", "fail_closed", ["guard.invalid_result"], 0.0),
    ),
    (
        "confidence out of range",
        policy({"pay": PAY}),
        {"fields": {"amount": 70000}, "backend": {"invalid": answer("allow", 1.5)}},
        ("ask_user", "fail_closed", ["guard.invalid_result"], 0.0),
    ),
    (
        "model check off for the tool",
        policy({"pay": {**PAY, "model_check": False}}),
        {"fields": {"amount": 70000}, "backend": {"result": answer("block", 1.0)}},
        ("allow", "hard", [], 1.0),
    ),
]


class VectorBackend:
    """A backend that answers as a vector says: a result, late, by raising, or invalidly."""

    name = "vectors-backend"

    def __init__(self, spec: dict[str, Any], timeout_s: float) -> None:
        self.spec = spec
        self.timeout_s = timeout_s

    def decide(self, request: DecisionRequest) -> Any:
        """Answer per the vector."""
        if self.spec.get("raises"):
            msg = "the backend broke"
            raise RuntimeError(msg)
        if "late" in self.spec:
            time.sleep(self.timeout_s * 4)  # purity-allow: a deliberately slow test backend
            return BackendResult.model_validate(self.spec["late"])
        if "invalid" in self.spec:
            try:
                return BackendResult.model_validate(self.spec["invalid"])
            except ValueError:
                return copy.deepcopy(self.spec["invalid"])
        return BackendResult.model_validate(self.spec["result"])


class FiringCheck:
    """A world check stand-in that fires or not, as the vector says."""

    def __init__(self, decision: str, fires: bool) -> None:
        self.decision = decision
        self.check = lambda request: fires


def run_case(raw_policy: dict[str, Any], inputs: dict[str, Any]) -> tuple[Any, ...]:
    """The Python engine's answer to one decision vector: (decision, layer, codes, confidence)."""
    parsed = parse_policy(raw_policy)
    fires = set(inputs.get("fires", []))
    checks = {name: FiringCheck(decision, name in fires) for name, decision in CHECKS.items()}
    spec = inputs.get("backend")
    backend = VectorBackend(spec, parsed.model_check.timeout_s) if spec is not None else None
    guard = Guard(
        parsed,
        backend,
        checks=checks,
        delegation=inputs.get("delegation"),
        pack_limits=inputs.get("pack_limits", True),
        log=DecisionLog(),
    )
    for tool, amount, when in inputs.get("history", []):
        guard.ledger.add(tool, amount, DecisionRequest.model_validate(request(at=when)).sim_time)
    outcome = guard.decide(
        DecisionRequest.model_validate(inputs.get("request", request())),
        Fields.model_validate(inputs.get("fields", {})),
    )
    assert outcome.record is not None
    return outcome.decision, outcome.layer, outcome.reason_codes, outcome.confidence, outcome.record


def decision_vectors() -> dict[str, Any]:
    """Every decision case, checked against its hand-written expectation."""
    cases = []
    for name, raw_policy, inputs, expected in DECISIONS:
        decision, layer, codes, confidence, record = run_case(raw_policy, inputs)
        got = (decision, layer, codes, confidence)
        if got != expected:
            msg = f"decision vector {name!r}: expected {expected}, the engine gave {got}"
            raise AssertionError(msg)
        full = {
            "request": inputs.get("request", request()),
            "fields": Fields.model_validate(inputs.get("fields", {})).model_dump(mode="json"),
        }
        cases.append(
            {
                "name": name,
                "policy": raw_policy,
                "request": full["request"],
                "fields": full["fields"],
                "delegation": inputs.get("delegation"),
                "pack_limits": inputs.get("pack_limits", True),
                "history": [
                    {"tool": tool, "amount": amount, "sim_time": when}
                    for tool, amount, when in inputs.get("history", [])
                ],
                "fires": inputs.get("fires", []),
                "backend": inputs.get("backend"),
                "expected": {
                    "decision": decision,
                    "layer": layer,
                    "reason_codes": codes,
                    "confidence": confidence,
                    "inputs_hash": record.inputs,
                    "policy_hash": record.policy.hash,
                    "delegation_hash": record.policy.delegation,
                },
            }
        )
    return {"format": "guard-vectors-decisions-v1", "checks": CHECKS, "cases": cases}


# --------------------------------------------------------------------- chains
def chain_vectors() -> dict[str, Any]:
    """An intact chain, and the ways it can be broken."""
    parsed = parse_policy(policy({"pay": PAY}))
    log = DecisionLog()
    guard = Guard(parsed, log=log, timer=iter(float(n) / 1000 for n in range(100)).__next__)
    for n, amount in enumerate((70000, 200000, 90000, 30000)):
        guard.decide(
            DecisionRequest.model_validate(request(at=f"2026-01-01T09:0{n}:00Z")),
            Fields(amount=amount, counterparty="shop@sandbox"),
        )
    intact = [record.model_dump(mode="json") for record in log.records]
    head = intact[-1]["hash"]
    edited = copy.deepcopy(intact)
    edited[1]["decision"] = "allow"
    edited_and_rehashed = copy.deepcopy(edited)
    edited_and_rehashed[1]["hash"] = digest({k: v for k, v in edited[1].items() if k != "hash"})
    extra = copy.deepcopy(intact)
    extra[0]["note"] = "added"
    cases = [
        ("intact", intact, head, 4, True),
        ("intact, no head given", intact, None, None, True),
        ("a decision edited", edited, head, 4, False),
        ("edited and its own hash recomputed", edited_and_rehashed, head, 4, False),
        (
            "a confidence edited by one ulp",
            _set(intact, 0, "confidence", 0.9999999999999999),
            head,
            4,
            False,
        ),
        ("the first record removed", intact[1:], head, 4, False),
        ("a middle record removed", [intact[0], intact[2], intact[3]], head, 4, False),
        ("the last record removed, head kept", intact[:3], head, 4, False),
        ("the last record removed, no head", intact[:3], None, None, True),
        ("two records swapped", [intact[0], intact[2], intact[1], intact[3]], head, 4, False),
        ("a record duplicated", [*intact[:2], intact[1], *intact[2:]], None, None, False),
        ("an unknown field added", extra, head, 4, False),
        ("empty, no head", [], None, None, True),
        ("empty, head kept", [], head, 4, False),
    ]
    from guard.records import verify_chain

    out = []
    for name, records, given_head, count, ok in cases:
        problems = verify_chain(records, head=given_head, count=count)
        if (not problems) != ok:
            msg = f"chain vector {name!r}: expected ok={ok}, got {problems}"
            raise AssertionError(msg)
        out.append({"name": name, "records": records, "head": given_head, "count": count, "ok": ok})
    return {"format": "guard-vectors-chains-v1", "cases": out}


def _set(records: list[dict[str, Any]], index: int, key: str, value: Any) -> list[dict[str, Any]]:
    changed = copy.deepcopy(records)
    changed[index][key] = value
    return changed


# ------------------------------------------------------------------------ main
def build() -> dict[str, str]:
    """Every vector file's name and text."""
    files = {
        "canonical.json": canonical_vectors(),
        "policies.json": policy_vectors(),
        "decisions.json": decision_vectors(),
        "chains.json": chain_vectors(),
    }
    return {
        name: json.dumps(data, indent=2, ensure_ascii=False) + "\n" for name, data in files.items()
    }


def main(argv: list[str] | None = None) -> int:
    """Write or check the vector files."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if a file would change")
    args = parser.parse_args(argv)
    stale = []
    for name, text in build().items():
        path = OUT / name
        if args.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != text:
                stale.append(name)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    if stale:
        sys.stdout.write(f"stale vectors (rerun scripts/guard_vectors.py): {stale}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
