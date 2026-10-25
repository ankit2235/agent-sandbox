"""The ``rules_only`` backend: each world's deterministic checks, and no model.

A request that breaks no rule is allowed; one that breaks a ``block`` rule is blocked;
otherwise one that breaks an ``ask_user`` rule is sent to the user. Reason codes are the
broken rules' ids. Confidence is 1: the rules are certain of what they check, and
blind to everything else (look-alike payees named only in the text, planted
instructions, budgets stated in words), which is what this backend is here to show.

In front of a guard, this is the backend that makes it "rules only": the policy's hard
rules first, then every one of the world's checks, and no model anywhere.
"""

from __future__ import annotations

from guard.actions import ActionsRegistry, actions
from guard.decision import BackendResult, Decision, DecisionLabel, DecisionRequest


class RulesOnlyBackend:
    """Deterministic checks from each world's module."""

    def __init__(self, name: str = "rules_only", registry: ActionsRegistry = actions) -> None:
        self.name = name
        self._registry = registry

    def decide(self, request: DecisionRequest) -> BackendResult:
        """Apply every rule of the request's world."""
        world = self._registry.get(request.world)
        raw = request.model_dump(mode="json")
        broken = [rule for rule in world.rules if rule.check(raw)]
        decision: DecisionLabel
        if any(rule.decision == "block" for rule in broken):
            decision = "block"
        elif broken:
            decision = "ask_user"
        else:
            decision = "allow"
        return BackendResult(
            decision=Decision(
                decision=decision,
                reason_codes=[rule.id for rule in broken],
                confidence=1.0,
            ),
            backend=self.name,
            model="rules",
            model_version=f"{request.world}-rules",
        )


__all__ = ["RulesOnlyBackend"]
