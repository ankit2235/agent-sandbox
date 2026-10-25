"""Policies: what a user delegated, as a versioned, hashed YAML or JSON file (``guard-policy-v1``).

A policy names the tools it governs (``actions``) and, for each, its hard rules:

- ``budget``: ``per_action``, ``daily`` and ``monthly`` caps on the action's ``amount``
  (minor units: paise, cents); daily and monthly count this guard's earlier allowed
  actions of the same tool, by the simulated UTC day and month;
- ``confirm_above``: an amount above which the user must confirm;
- ``counterparties`` and ``categories``: ``allow``, ``block`` and ``confirm`` lists, and
  what an unlisted one gets when an ``allow`` list is given;
- ``mandate``: whether the action must name one, and which ones it may name;
- ``time_windows``: UTC windows the action must fall in, and what happens outside them;
- ``checks``: the world's deterministic checks (by id) that are hard rules here;
- ``model_check``: whether the decision backend is asked as well.

A tool the policy does not name gets ``default``. The file is checked against
``schemas/policy.schema.json`` (shared with the TypeScript SDK) before anything reads it.
Its hash is the SHA-256 of its canonical JSON (:mod:`guard.canonical`), so YAML and the
same content in JSON hash alike, and every decision record names the exact policy.

**Delegation.** A task, a user or an operator can delegate limits of their own in the
``actions`` format (the sandbox reads them from each task through the world's
``guard_delegation``). :func:`effective` joins them with the policy: every rule of both
applies, so the stricter always wins, and a delegation can never be loosened by the
policy or loosen it. With ``pack_limits=False`` (sandbox runs) the policy's own numbers
(``budget``, ``confirm_above``) are left out: they are defaults for the decorator and the
proxy outside the sandbox, and a task's own limits replace them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

import jsonschema
import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from guard.canonical import digest
from guard.decision import DecisionLabel

FORMAT = "guard-policy-v1"
POLICIES = Path(str(files("guard").joinpath("policies")))
POLICY_SCHEMA: dict[str, Any] = json.loads(
    files("guard").joinpath("schemas/policy.schema.json").read_text(encoding="utf-8")
)
DELEGATION_SCHEMA: dict[str, Any] = {
    **{key: POLICY_SCHEMA[key] for key in ("$schema", "$defs")},
    "type": "object",
    "additionalProperties": {"$ref": "#/$defs/rules"},
}
"""A delegation: per-tool rules in the policy's ``actions`` format."""
_POLICY = jsonschema.Draft202012Validator(POLICY_SCHEMA)
_DELEGATION = jsonschema.Draft202012Validator(DELEGATION_SCHEMA)
"""Compiled once: ``jsonschema.validate`` would check the schema itself on every call."""

Day = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAYS: tuple[Day, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
Hold = Literal["block", "ask_user"]


class PolicyError(ValueError):
    """A policy or delegation is not valid, or names a check nobody provides."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Budget(_Strict):
    """Caps on the action's amount."""

    per_action: int | None = Field(default=None, ge=0)
    daily: int | None = Field(default=None, ge=0)
    monthly: int | None = Field(default=None, ge=0)


class ListRule(_Strict):
    """Allowed, blocked and confirm-first values of one field."""

    allow: list[str] | None = None
    block: list[str] = Field(default_factory=list)
    confirm: list[str] = Field(default_factory=list)
    unlisted: Hold = "ask_user"
    """What a value not in ``allow`` gets, when ``allow`` is given."""


class MandateRule(_Strict):
    """Whether the action must name a mandate, and which it may name."""

    required: bool = False
    allow: list[str] | None = None


class Window(_Strict):
    """Days of the week and a UTC time range, ``start`` included and ``end`` not."""

    days: list[Day]
    start: str
    end: str


class TimeWindows(_Strict):
    """The windows an action must fall in (any one), and what happens outside them."""

    windows: list[Window]
    outside: Hold = "ask_user"


class ActionRules(_Strict):
    """The hard rules for one tool."""

    budget: Budget | None = None
    confirm_above: int | None = Field(default=None, ge=0)
    counterparties: ListRule | None = None
    categories: ListRule | None = None
    mandate: MandateRule | None = None
    time_windows: TimeWindows | None = None
    checks: list[str] = Field(default_factory=list)
    model_check: bool = True


class ModelCheck(_Strict):
    """How the decision backend is asked."""

    min_confidence: float = Field(default=0.5, ge=0, le=1)
    """Below this, a backend's answer becomes ask_user (``guard.low_confidence``)."""
    timeout_s: float = Field(default=10.0, gt=0)


class Policy(_Strict):
    """One policy file."""

    format: Literal["guard-policy-v1"]
    id: str
    version: int = Field(ge=1)
    world: str | None = None
    description: str = ""
    default: DecisionLabel = "ask_user"
    """The decision for a guarded tool the policy does not name."""
    model_check: ModelCheck = Field(default_factory=ModelCheck)
    actions: dict[str, ActionRules]
    _hash: str = PrivateAttr(default="")

    @property
    def hash(self) -> str:
        """The SHA-256 of the file's canonical JSON."""
        return self._hash


def _schema_error(error: jsonschema.ValidationError) -> str:
    where = "/".join(str(part) for part in error.absolute_path) or "(top level)"
    return f"{where}: {error.message}"


def _windows(rules: dict[str, ActionRules], label: str) -> None:
    for tool, rule in rules.items():
        if rule.time_windows is None:
            continue
        for window in rule.time_windows.windows:
            if window.start >= window.end:
                msg = f"{label} {tool}: a window must start before it ends ({window.start})"
                raise PolicyError(msg)


def parse_policy(raw: Any) -> Policy:
    """A policy from parsed YAML or JSON, checked against the shared schema."""
    error = jsonschema.exceptions.best_match(_POLICY.iter_errors(raw))
    if error is not None:
        msg = f"not a {FORMAT} policy: {_schema_error(error)}"
        raise PolicyError(msg) from error
    policy = Policy.model_validate(raw)
    _windows(policy.actions, "policy")
    policy._hash = digest(raw)
    return policy


def parse_delegation(raw: Any) -> dict[str, ActionRules]:
    """A delegation (per-tool rules) checked against the shared schema."""
    error = jsonschema.exceptions.best_match(_DELEGATION.iter_errors(raw))
    if error is not None:
        msg = f"not a delegation: {_schema_error(error)}"
        raise PolicyError(msg) from error
    rules = {tool: ActionRules.model_validate(value) for tool, value in raw.items()}
    _windows(rules, "delegation")
    return rules


def packs() -> list[str]:
    """The packaged policy packs, as ``<world>/<name>``."""
    return sorted(
        f"{path.parent.name}/{path.stem}" for path in POLICIES.glob("*/*.yaml") if path.is_file()
    )


def load_policy(source: str | Path) -> Policy:
    """A policy from a file (YAML or JSON) or a packaged pack name such as ``payments/default``."""
    path = Path(source)
    if not path.is_file():
        path = POLICIES / f"{source}.yaml"
    if not path.is_file():
        msg = f"no policy file or pack {str(source)!r}; packs: {packs()}"
        raise FileNotFoundError(msg)
    text = path.read_text(encoding="utf-8")
    try:
        raw = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as error:
        msg = f"{path}: not YAML or JSON: {error}"
        raise PolicyError(msg) from error
    return parse_policy(raw)


@dataclass(frozen=True, slots=True)
class Effective:
    """Every rule that applies to one tool: the policy's and the delegation's, all of them."""

    covered: bool
    """Whether the policy names the tool (if not, its ``default`` applies)."""
    rules: tuple[ActionRules, ...]
    model_check: bool

    @property
    def checks(self) -> list[str]:
        """Every check id, in order, once."""
        return list(dict.fromkeys(check for rule in self.rules for check in rule.checks))


def effective(
    policy: Policy,
    tool: str,
    delegation: dict[str, ActionRules] | None = None,
    *,
    pack_limits: bool = True,
) -> Effective:
    """The rules for ``tool``: the stricter of the policy and the delegation always wins."""
    own = policy.actions.get(tool)
    rules: list[ActionRules] = []
    if own is not None:
        rules.append(
            own if pack_limits else own.model_copy(update={"budget": None, "confirm_above": None})
        )
    delegated = (delegation or {}).get(tool)
    if delegated is not None:
        rules.append(delegated)
    return Effective(
        covered=own is not None,
        rules=tuple(rules),
        model_check=own is not None and own.model_check,
    )


__all__ = [
    "DAYS",
    "DELEGATION_SCHEMA",
    "FORMAT",
    "POLICIES",
    "POLICY_SCHEMA",
    "ActionRules",
    "Budget",
    "Effective",
    "ListRule",
    "MandateRule",
    "ModelCheck",
    "Policy",
    "PolicyError",
    "TimeWindows",
    "Window",
    "effective",
    "load_policy",
    "packs",
    "parse_delegation",
    "parse_policy",
]
