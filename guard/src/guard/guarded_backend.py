"""A backend behind the guard's hard rules, as a :class:`guard.DecisionBackend` itself.

``sandbox-train eval --guard '{world}/default'`` scores a backend the way it is deployed: each
dataset row is decided by a fresh :class:`guard.Guard` (the policy pack's hard rules and the
row's world checks, the row task's own delegated limits, then the backend). A fresh guard
per row, because rows come from different episodes: one row's allowed amount is never
another's spend. Every decision goes to one shared log, so its records, and the measured
latency, include writing the record.
"""

from __future__ import annotations

from collections.abc import Mapping

from guard.actions import ActionsRegistry, actions
from guard.decision import BackendResult, Decision, DecisionBackend, DecisionRequest
from guard.engine import Guard
from guard.policy import Policy
from guard.records import DecisionLog


class GuardedBackend:
    """``backend`` (or no backend: hard rules only) behind each world's policy."""

    def __init__(
        self,
        policies: Mapping[str, Policy],
        backend: DecisionBackend | None,
        *,
        log: DecisionLog | None = None,
        registry: ActionsRegistry = actions,
    ) -> None:
        self.policies = dict(policies)
        self.backend = backend
        self.log = log
        self.name = f"{backend.name if backend is not None else 'hard_rules'}+guard"
        self._registry = registry

    def decide(self, request: DecisionRequest) -> BackendResult:
        """Decide one row with a fresh guard."""
        policy = self.policies.get(request.world)
        if policy is None:
            msg = f"no policy for world {request.world!r}; have {sorted(self.policies)}"
            raise KeyError(msg)
        world = self._registry.get(request.world)
        guard = Guard(
            policy,
            self.backend,
            checks={rule.id: rule for rule in world.rules},
            log=self.log,
            delegation=world.delegation,
            pack_limits=False,
        )
        outcome = guard.decide(request, world.fields(request.model_dump(mode="json")))
        answer = outcome.backend
        return BackendResult(
            decision=Decision(
                decision=outcome.decision,
                reason_codes=outcome.reason_codes,
                confidence=outcome.confidence,
            ),
            backend=self.name,
            model=answer.model if answer is not None else None,
            model_version=answer.model_version if answer is not None else None,
            probabilities=answer.probabilities if outcome.layer == "backend" and answer else None,
            input_tokens=answer.input_tokens if answer is not None else None,
            output_tokens=answer.output_tokens if answer is not None else None,
            error=answer.error if outcome.layer == "fail_closed" and answer else None,
        )


__all__ = ["GuardedBackend"]
