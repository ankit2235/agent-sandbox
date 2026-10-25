"""Baselines, each a :class:`guard.DecisionBackend` built from a config file.

- ``rules``: the worlds' deterministic rules (:mod:`.rules`), no model.
- ``llm_judge``: a general chat model through LiteLLM, asked for the decision JSON
  (:mod:`.judge`). The model id is a config value with no default.
- ``system_one``: decision models that speak the System One API: TypeSafe Jev, Cloudflare
  Clef and a self-hosted Strands Decider (:mod:`.system_one`).
- ``openai_decisions``: OpenAI's Decisions API (:mod:`.openai_decisions`).

Every backend sees the same rendered request (:func:`training.render.render`) and the
same three decisions described the same way (:mod:`.questions`).
"""
