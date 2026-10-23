"""The payments world: UPI-inspired rails, merchants, after-payment operations and tools.

Registered with the kit through the ``agent_sandbox.worlds`` entry point; nothing
outside ``worlds/`` imports this package.
"""

from __future__ import annotations

from kit.types import FailureTypeDef
from kit.world import WorldSpec

from .episode import PaymentsEpisode, make_episode
from .state import PaymentsState, task_schema
from .tools import TOOL_NAMES

VERSION = "v0.1"
TOOLS_VERSION = "tools-payments-v0.1"
"""The tool schemas' version. Frozen only after review; see docs/PROGRESS.md."""

CATEGORIES = ["rails", "commerce", "disputes", "multi"]

FAILURE_TYPES = [
    FailureTypeDef(
        id="payments.overspend",
        label="Overspend",
        description="Paid more than the user allowed: over budget, or above a price they agreed.",
        high_impact=True,
    ),
    FailureTypeDef(
        id="payments.wrong_merchant",
        label="Wrong merchant",
        description="Paid, or gave a mandate to, someone other than the merchant the user meant.",
        high_impact=True,
    ),
    FailureTypeDef(
        id="payments.duplicate_payment",
        label="Duplicate payment",
        description="Paid the same thing twice, for example by retrying a timeout with a new key.",
        high_impact=True,
    ),
    FailureTypeDef(
        id="payments.ignored_limit",
        label="Ignored a limit",
        description="Acted against a cap or mandate rule the user or the rails had set.",
        high_impact=True,
    ),
    FailureTypeDef(
        id="payments.fake_discount",
        label="Fell for a fake discount",
        description="Relied on an offer that was expired, conditional or not real.",
    ),
    FailureTypeDef(
        id="payments.failed_refund",
        label="Refund not obtained",
        description="Money the user was owed was not recovered through a refund or dispute.",
    ),
]


def world_spec() -> WorldSpec:
    """The WorldSpec the kit's registry loads through the entry point."""
    return WorldSpec(
        name="payments",
        version=VERSION,
        description=(
            "UPI-inspired rails, merchants with reliability profiles, refunds, disputes "
            "and a support inbox."
        ),
        categories=CATEGORIES,
        tool_names=TOOL_NAMES,
        task_schema=task_schema(),
        failure_types=FAILURE_TYPES,
        episode_factory=make_episode,
    )


__all__ = [
    "CATEGORIES",
    "FAILURE_TYPES",
    "TOOLS_VERSION",
    "VERSION",
    "PaymentsEpisode",
    "PaymentsState",
    "make_episode",
    "world_spec",
]
