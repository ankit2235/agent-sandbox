"""The open guard model's data, baselines, training configs and evaluation (Phase 7).

Domain-neutral: nothing here imports a world. A world says which of its tools are high
impact, how to read the state around an action and which rules the rules-only baseline
checks, in a module it registers through the ``agent_sandbox.training`` entry point
(:mod:`training.world`).

- :mod:`training.data`: the action dataset (``actions-v0.1``), its labels from the
  graders, its splits by task family and its leakage checks.
- :mod:`training.baselines`: rules only, LLM judges and decision models, each a
  :class:`guard.DecisionBackend`.
- :mod:`training.guard_model`: our model's prompt format, backend, reward and training
  scripts (the scripts need ``requirements-train.txt``, never installed by CI).
- :mod:`training.eval`: identical tests for every backend, with intervals over families.
"""

DATASET_VERSION = "actions-v0.1"

__all__ = ["DATASET_VERSION"]
