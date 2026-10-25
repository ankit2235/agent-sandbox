"""The open guard model's data, training configs and evaluation (Phase 7).

Domain-neutral: nothing here imports a world. A world says which of its tools are high
impact, how to read the state around an action and which rules the rules-only backend
checks, in a module it registers through the ``agent_sandbox.training`` entry point
(:mod:`guard.actions`, moved to the guard in Phase 8 with the backends).

- :mod:`training.data`: the action dataset (``actions-v0.1``), its labels from the
  graders, its splits by task family and its leakage checks.
- :mod:`guard.backends` (not here): rules only, LLM judges, decision models and our
  model's backend, each a :class:`guard.DecisionBackend`.
- :mod:`training.guard_model`: our model's training files, reward and training scripts
  (the scripts need ``requirements-train.txt``, never installed by CI).
- :mod:`training.eval`: identical tests for every backend, with intervals over families.
"""

DATASET_VERSION = "actions-v0.1"

__all__ = ["DATASET_VERSION"]
