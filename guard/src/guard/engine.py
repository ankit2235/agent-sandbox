"""The layered decision, failing closed: :class:`Guard`.

For one attempted action:

1. **Hard rules** (:mod:`guard.rules`), always, first: the policy's rules and the task's
   delegated limits for the tool (:func:`guard.policy.effective`), the world checks the
   policy names, a tool the task did not delegate, a tool the policy does not name. If
   any blocks, the decision is block and no backend is asked.
2. **The backend** (any :class:`guard.DecisionBackend`), when the policy asks for a model
   check on the tool: run with the policy's timeout, its answer checked to be a
   :class:`guard.BackendResult`. A timeout, an exception, an answer that is not a valid
   result, or a backend's own failure becomes ask_user (:func:`guard.fail_closed`); an
   answer below the policy's ``min_confidence`` becomes ask_user (``guard.low_confidence``).
3. The decision is the stricter of the two (block, then ask_user, then allow). A backend
   can make a decision stricter; nothing it answers can make one looser, so it can never
   turn a hard block, or a hard ask_user, into an allow.

Every decision is appended to the guard's :class:`guard.records.DecisionLog`, if it has
one. The record's time is the request's ``sim_time`` (the simulated clock, in a sandbox
run); only the latency reads the real clock, and it never feeds a decision.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic import ValidationError

from guard.backends.calibration import LOW_CONFIDENCE
from guard.canonical import digest
from guard.decision import (
    BackendResult,
    DecisionBackend,
    DecisionLabel,
    DecisionRequest,
    fail_closed,
)
from guard.policy import ActionRules, Policy, PolicyError, effective, parse_delegation
from guard.records import (
    BackendRef,
    DecisionLog,
    DecisionRecord,
    Layer,
    PolicyRef,
    Tokens,
)
from guard.rules import ORDER, Check, Fields, Hit, Ledger, code, evaluate, strictest

Timer = Callable[[], float]
DelegationSource = Mapping[str, Any] | Callable[[dict[str, Any]], Mapping[str, Any]]
"""Delegated limits: fixed, or read from each request (as JSON) by the world's module."""


@dataclass(frozen=True, slots=True)
class Outcome:
    """What the guard decided about one action, and how."""

    decision: DecisionLabel
    reason_codes: list[str]
    confidence: float
    layer: Layer
    tool: str
    fields: Fields
    hits: list[Hit] = field(default_factory=list)
    backend: BackendResult | None = None
    """The backend's answer, when it was asked (after failing closed, if it did)."""
    delegation: str | None = None
    """The hash of the delegated limits it was decided under, if any."""
    record: DecisionRecord | None = None


def _ask(backend: DecisionBackend, request: DecisionRequest, timeout_s: float) -> BackendResult:
    """The backend's answer within the timeout, or a fail-closed one."""
    box: list[Any] = []
    failure: list[BaseException] = []

    def run() -> None:
        try:
            box.append(backend.decide(request))
        except Exception as error:  # the fail-closed boundary: any backend error is ask_user
            failure.append(error)

    worker = threading.Thread(target=run, name=f"guard-{backend.name}", daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        return fail_closed(backend.name, "timeout")
    if failure:
        return fail_closed(backend.name, "backend_error")
    result = box[0]
    if not isinstance(result, BackendResult):
        return fail_closed(backend.name, "invalid_result")
    try:
        return BackendResult.model_validate(result.model_dump())
    except ValidationError:
        return fail_closed(backend.name, "invalid_result")


class Guard:
    """A policy, an optional backend, the world's checks and a decision log."""

    def __init__(
        self,
        policy: Policy,
        backend: DecisionBackend | None = None,
        *,
        checks: Mapping[str, Check] | None = None,
        log: DecisionLog | None = None,
        delegation: DelegationSource | None = None,
        pack_limits: bool = True,
        timer: Timer = time.perf_counter,  # purity-allow: decision latency, never a world's clock
    ) -> None:
        self.policy = policy
        self.backend = backend
        self.checks: Mapping[str, Check] = checks or {}
        self.log = log
        self.pack_limits = pack_limits
        self.delegation = delegation
        self.ledger = Ledger()
        self._timer = timer
        self._known(policy.actions, f"policy {policy.id}")
        if delegation is not None and not callable(delegation):
            self._known(parse_delegation(dict(delegation)), "the delegation")

    def _known(self, rules: Mapping[str, ActionRules], label: str) -> None:
        named = {check for rule in rules.values() for check in rule.checks}
        missing = sorted(named - set(self.checks))
        if missing:
            msg = f"{label} names checks no world provides: {missing}"
            raise PolicyError(msg)

    def _delegated(self, raw: dict[str, Any]) -> tuple[dict[str, ActionRules], str | None]:
        """The delegation that applies to one request, and its hash for the record."""
        source = self.delegation
        given = source(raw) if callable(source) else source
        if not given:
            return {}, None
        rules = parse_delegation(dict(given))
        self._known(rules, "the delegation")
        return rules, digest(dict(given))

    def decide(
        self,
        request: DecisionRequest,
        fields: Fields | Mapping[str, Any] | None = None,
        *,
        args: Mapping[str, Any] | None = None,
    ) -> Outcome:
        """Decide one action and record it. ``args`` are the raw arguments, if the request's
        were rewritten (opaque values hidden); their hash goes in the record.
        """
        start = self._timer()
        if request.sim_time.utcoffset() is None:
            msg = "a request's sim_time must carry a time zone (the kit's SimClock is UTC)"
            raise PolicyError(msg)
        tool = request.action.tool
        fields = fields if isinstance(fields, Fields) else Fields.model_validate(dict(fields or {}))
        raw = request.model_dump(mode="json")
        delegated, delegation_hash = self._delegated(raw)
        rules = effective(self.policy, tool, delegated, pack_limits=self.pack_limits)
        hits = evaluate(
            rules,
            tool=tool,
            fields=fields,
            request=raw,
            when=request.sim_time,
            ledger=self.ledger,
            checks=self.checks,
            default=self.policy.default,
        )
        hard = strictest([hit.decision for hit in hits])
        hard_codes = [hit.code for hit in hits]
        answer: BackendResult | None = None
        decision, codes, confidence = hard, hard_codes, 1.0
        uncovered = code("uncovered-tool")
        own_hits = [hit for hit in hits if hit.code != uncovered]
        layer: Layer = "hard" if rules.covered or own_hits else "default"
        if hard != "block" and rules.model_check and self.backend is not None:
            answer = _ask(self.backend, request, self.policy.model_check.timeout_s)
            said = answer.decision
            model, model_codes = said.decision, list(said.reason_codes)
            if answer.error is None and said.confidence < self.policy.model_check.min_confidence:
                model = "ask_user"
                model_codes = [*model_codes, LOW_CONFIDENCE]
            if ORDER[model] > ORDER[hard] or not hits:
                decision = strictest([hard, model])
                layer = "fail_closed" if answer.error is not None else "backend"
                confidence = said.confidence
                codes = [*hard_codes, *(model_codes if model != "allow" else [])]
        if decision == "allow":
            codes = []
            if fields.amount is not None:
                self.ledger.add(tool, fields.amount, request.sim_time)
        elapsed = (self._timer() - start) * 1000.0
        outcome = Outcome(
            decision=decision,
            reason_codes=list(dict.fromkeys(codes)),
            confidence=confidence,
            layer=layer,
            tool=tool,
            fields=fields,
            hits=hits,
            backend=answer,
        )
        record = self._record(outcome, raw, fields, args, elapsed, delegation_hash)
        return replace(outcome, record=record, delegation=delegation_hash)

    def confirmed(self, outcome: Outcome, request: DecisionRequest) -> Outcome:
        """The user confirmed an ask_user: record it, and count its amount as allowed."""
        if outcome.decision != "ask_user":
            msg = f"only an ask_user decision can be confirmed, not {outcome.decision}"
            raise ValueError(msg)
        if outcome.fields.amount is not None:
            self.ledger.add(outcome.tool, outcome.fields.amount, request.sim_time)
        allowed = Outcome(
            decision="allow",
            reason_codes=list(outcome.reason_codes),
            confidence=1.0,
            layer="user",
            tool=outcome.tool,
            fields=outcome.fields,
        )
        raw = request.model_dump(mode="json")
        record = self._record(
            allowed, raw, outcome.fields, request.action.args, 0.0, outcome.delegation
        )
        return replace(allowed, record=record, delegation=outcome.delegation)

    def _record(
        self,
        outcome: Outcome,
        raw: dict[str, Any],
        fields: Fields,
        args: Mapping[str, Any] | None,
        elapsed_ms: float,
        delegation_hash: str | None,
    ) -> DecisionRecord | None:
        if self.log is None:
            return None
        answer = outcome.backend
        backend = (
            BackendRef(name=answer.backend, model=answer.model, model_version=answer.model_version)
            if answer is not None
            else None
        )
        tokens = (
            Tokens(input=answer.input_tokens, output=answer.output_tokens)
            if answer is not None
            and (answer.input_tokens is not None or answer.output_tokens is not None)
            else None
        )
        body = {
            "sim_time": raw["sim_time"],
            "policy": PolicyRef(
                id=self.policy.id,
                version=self.policy.version,
                hash=self.policy.hash,
                delegation=delegation_hash,
            ).model_dump(mode="json"),
            "inputs": digest({"request": raw, "fields": fields.model_dump(mode="json")}),
            "action": {
                "tool": outcome.tool,
                "args": digest(dict(args if args is not None else raw["action"].get("args", {}))),
            },
            "backend": backend.model_dump(mode="json") if backend is not None else None,
            "layer": outcome.layer,
            "decision": outcome.decision,
            "reason_codes": outcome.reason_codes,
            "confidence": outcome.confidence,
            "latency_ms": round(elapsed_ms, 3),
            "tokens": tokens.model_dump(mode="json") if tokens is not None else None,
            "error": answer.error if answer is not None else None,
        }
        return self.log.append(body)


__all__ = ["DelegationSource", "Guard", "Outcome", "Timer"]
