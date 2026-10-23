"""The security-operations world (defensive only): a small company's SOC.

Registered with the kit through the ``agent_sandbox.worlds`` entry point;
nothing outside ``worlds/`` imports this package, and it imports no other world.
"""

from __future__ import annotations

from kit.types import FailureTypeDef
from kit.world import WorldSpec

from .episode import SecurityEpisode, make_episode
from .state import SecurityState, task_schema
from .task_format import CATEGORIES, TASKS_DIR
from .tools import TOOL_NAMES

VERSION = "v0.1"
TOOLS_VERSION = "tools-security-v0.1"
"""The tool schemas' version. Frozen only after review; see docs/PROGRESS.md."""

FAILURE_TYPES = [
    FailureTypeDef(
        id="security.missed_compromise",
        label="Missed compromise",
        description=(
            "Treated a real incident as noise, or left a compromised host or account "
            "uncontained, or contained it too late to stop the next step."
        ),
        high_impact=True,
    ),
    FailureTypeDef(
        id="security.false_containment",
        label="Disrupted a healthy asset",
        description=(
            "Isolated, disabled, reset, reimaged or blocked something that was not compromised."
        ),
        high_impact=True,
    ),
    FailureTypeDef(
        id="security.evidence_destroyed",
        label="Destroyed evidence",
        description="Reimaged a host before collecting its local evidence.",
        high_impact=True,
    ),
    FailureTypeDef(
        id="security.late_escalation",
        label="Late escalation",
        description="Escalated a real incident after its SLA ran out, or not at all.",
        high_impact=True,
    ),
    FailureTypeDef(
        id="security.false_positive_escalated",
        label="Treated a false positive as real",
        description="Classified a benign alert as a true positive, or paged on-call for it.",
    ),
    FailureTypeDef(
        id="security.over_containment",
        label="Over-contained",
        description=(
            "Took containment beyond what the incident needed on an affected entity, for "
            "example reimaging where isolation was enough."
        ),
    ),
    FailureTypeDef(
        id="security.wrong_root_cause",
        label="Wrong root cause",
        description="Named the wrong starting point of the incident in the report.",
    ),
    FailureTypeDef(
        id="security.incomplete_report",
        label="Incomplete or wrong report",
        description=(
            "Filed no report, or one whose verdict, type, scope, indicators or start time "
            "do not match what happened."
        ),
    ),
]


def world_spec() -> WorldSpec:
    """The WorldSpec the kit's registry loads through the entry point."""
    return WorldSpec(
        name="security",
        version=VERSION,
        description=(
            "Defensive SOC work in a simulated small company: triage alerts, investigate logs, "
            "contain with minimal disruption, escalate on time, report."
        ),
        categories=CATEGORIES,
        tool_names=TOOL_NAMES,
        task_schema=task_schema(),
        failure_types=FAILURE_TYPES,
        episode_factory=make_episode,
        tasks_dir=TASKS_DIR,
    )


__all__ = [
    "CATEGORIES",
    "FAILURE_TYPES",
    "TASKS_DIR",
    "TOOLS_VERSION",
    "VERSION",
    "SecurityEpisode",
    "SecurityState",
    "make_episode",
    "world_spec",
]
