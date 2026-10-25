"""The guard library: checks every high-impact action against what the user delegated.

Domain-neutral: nothing here imports a world. A world's facts (its high-impact tools,
its checks, how an action maps to policy fields) come through its own module and the
``agent_sandbox.training`` entry point (:mod:`guard.actions`).

- :mod:`guard.policy`: the policy file (``guard-policy-v1``), versioned and hashed;
- :mod:`guard.rules`: the hard rules, always first;
- :mod:`guard.engine`: :class:`Guard`, the layered decision that fails closed;
- :mod:`guard.backends`: the decision backends behind the hard rules;
- :mod:`guard.records`: the hash-chained decision log and its verification;
- :mod:`guard.wrap` and :mod:`guard.mcp_proxy`: the decorator and the MCP proxy;
- :mod:`guard.decision`: the decision contract every backend answers in.

The guard never holds or moves money and never touches a real system: it decides, and
records why.
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
from guard.engine import Guard, Outcome
from guard.policy import Policy, PolicyError, load_policy, parse_policy
from guard.records import DecisionLog, DecisionRecord, verify_chain
from guard.rules import Fields
from guard.wrap import GuardAskUser, GuardBlocked, guarded

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
    "DecisionLog",
    "DecisionRecord",
    "DecisionRequest",
    "Fields",
    "Guard",
    "GuardAskUser",
    "GuardBlocked",
    "Outcome",
    "Policy",
    "PolicyError",
    "StepView",
    "__version__",
    "fail_closed",
    "guarded",
    "load_policy",
    "parse_decision",
    "parse_policy",
    "verify_chain",
]
