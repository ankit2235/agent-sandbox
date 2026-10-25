"""Decision backends, each a :class:`guard.DecisionBackend` built from a config file.

- ``rules_only``: the worlds' deterministic checks (:mod:`.rules_only`), no model.
- ``llm_judge``: a general chat model asked for the decision JSON (:mod:`.llm_judge`). The
  model id is a config value with no default.
- ``system_one``: decision models that speak the System One API: TypeSafe Jev, Cloudflare
  Clef and a self-hosted Strands Decider (:mod:`.system_one`).
- ``openai_decisions``: OpenAI's Decisions API (:mod:`.openai_decisions`).
- ``guard_model``: our model, run locally (:mod:`.guard_model`).

Every backend sees the same rendered request (:func:`guard.render.render`) and the
same three decisions described the same way (:mod:`.questions`). The guard puts each one
behind its hard rules (:mod:`guard.engine`); the training code scores them bare.
"""
