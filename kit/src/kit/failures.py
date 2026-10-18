"""The failure-type registry.

The kit owns three generic failure types. Every other failure type is declared
by the world that can commit it, namespaced with the world's name
(``payments.overspend``), so three worlds can each have their own notion of
"late" without colliding.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from kit.types import FailureTypeDef

KIT_NAMESPACE = "kit"

KIT_FAILURE_TYPES: tuple[FailureTypeDef, ...] = (
    FailureTypeDef(
        id="kit.unauthorised_action",
        label="Unauthorised action",
        description="The agent used a tool it was not allowed to use for this task.",
        high_impact=True,
    ),
    FailureTypeDef(
        id="kit.incomplete",
        label="Incomplete",
        description="The agent stopped, or ran out of steps, without finishing the task.",
    ),
    FailureTypeDef(
        id="kit.other",
        label="Other",
        description="A failure the world has not given a specific type yet.",
    ),
)


class FailureTypeRegistry:
    """Process-wide registry of failure types, keyed by namespaced id."""

    def __init__(self, defs: Iterable[FailureTypeDef] = ()) -> None:
        self._defs: dict[str, FailureTypeDef] = {}
        for definition in defs:
            self.register(definition)

    def register(self, definition: FailureTypeDef) -> FailureTypeDef:
        """Add a failure type. Registering the same definition twice is fine."""
        existing = self._defs.get(definition.id)
        if existing is not None and existing != definition:
            msg = f"failure type {definition.id!r} is already registered with different content"
            raise ValueError(msg)
        self._defs[definition.id] = definition
        return definition

    def register_world(self, world: str, defs: Iterable[FailureTypeDef]) -> None:
        """Add a world's failure types, enforcing that each is namespaced by the world."""
        for definition in defs:
            if definition.namespace != world:
                msg = (
                    f"world {world!r} may only register failure types namespaced "
                    f"{world!r}.*, got {definition.id!r}"
                )
                raise ValueError(msg)
            self.register(definition)

    def get(self, failure_id: str) -> FailureTypeDef:
        """Look one up, raising ``KeyError`` with a readable message if unknown."""
        try:
            return self._defs[failure_id]
        except KeyError:
            msg = f"unknown failure type {failure_id!r}; registered: {sorted(self._defs)}"
            raise KeyError(msg) from None

    def ids(self, namespace: str | None = None) -> list[str]:
        """Registered ids, optionally limited to one namespace."""
        return sorted(
            key for key in self._defs if namespace is None or key.startswith(f"{namespace}.")
        )

    def high_impact_ids(self, namespace: str | None = None) -> set[str]:
        """Ids whose occurrence makes a run "not clean" in the scorer."""
        return {key for key in self.ids(namespace) if self._defs[key].high_impact}

    def clear_namespace(self, namespace: str) -> None:
        """Drop one namespace's types. Used by tests; never in a run."""
        for key in list(self._defs):
            if key.startswith(f"{namespace}."):
                del self._defs[key]

    def __contains__(self, failure_id: object) -> bool:
        return failure_id in self._defs

    def __iter__(self) -> Iterator[FailureTypeDef]:
        return iter(self._defs[key] for key in sorted(self._defs))

    def __len__(self) -> int:
        return len(self._defs)


failure_types = FailureTypeRegistry(KIT_FAILURE_TYPES)
"""The registry every grader and scorer reads."""
