"""Domain-neutral core of the agent sandbox.

Nothing in this package may import a world. Worlds register themselves through
the ``agent_sandbox.worlds`` entry-point group; see :mod:`kit.world`.
"""

from kit.clock import EPOCH, ScheduledEvent, SimClock
from kit.failures import FailureTypeRegistry, failure_types
from kit.rng import SeededRNG
from kit.trace import DiffEntry, GraderTrace, GraderTraceStep, Trace, TraceStep
from kit.types import (
    CheckResult,
    Difficulty,
    FailureTypeDef,
    GradeResult,
    Observation,
    Signoff,
    StepResult,
    TaskSpec,
    ToolCall,
    ToolError,
    Usage,
    Visibility,
)
from kit.world import (
    Episode,
    Grader,
    ReadOnlyService,
    StateView,
    WorldRegistry,
    WorldSpec,
    WorldState,
    load_grader,
    worlds,
)

__all__ = [
    "EPOCH",
    "CheckResult",
    "DiffEntry",
    "Difficulty",
    "Episode",
    "FailureTypeDef",
    "FailureTypeRegistry",
    "GradeResult",
    "Grader",
    "GraderTrace",
    "GraderTraceStep",
    "Observation",
    "ReadOnlyService",
    "ScheduledEvent",
    "SeededRNG",
    "Signoff",
    "SimClock",
    "StateView",
    "StepResult",
    "TaskSpec",
    "ToolCall",
    "ToolError",
    "Trace",
    "TraceStep",
    "Usage",
    "Visibility",
    "WorldRegistry",
    "WorldSpec",
    "WorldState",
    "__version__",
    "failure_types",
    "load_grader",
    "worlds",
]

__version__ = "0.1.0"
