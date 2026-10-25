"""The guard's decision interface (Phase 7). The guard library itself is Phase 8.

Domain-neutral: nothing here imports a world, the kit's world registry or a model
library. A backend answers one :class:`~guard.decision.DecisionRequest` with a
:class:`~guard.decision.BackendResult`; the training baselines, our guard model and,
later, the guard's own model check all speak this interface.
"""

from guard.decision import (
    DECISION_SCHEMA,
    DECISIONS,
    ActionCall,
    BackendResult,
    Decision,
    DecisionBackend,
    DecisionError,
    DecisionLabel,
    DecisionRequest,
    StepView,
    fail_closed,
    parse_decision,
)

__version__ = "0.1.0"

__all__ = [
    "DECISIONS",
    "DECISION_SCHEMA",
    "ActionCall",
    "BackendResult",
    "Decision",
    "DecisionBackend",
    "DecisionError",
    "DecisionLabel",
    "DecisionRequest",
    "StepView",
    "__version__",
    "fail_closed",
    "parse_decision",
]
