# Contributing tasks

New tasks go into the **next** task set: a released set (`payments-v0.1`) is frozen. Open an issue
with the task proposal template first.

1. Write `worlds/<world>/src/worlds/<world>/tasks/<category>/<id>.yaml` ([Task format](task-format.md)).
   Synthetic data only: test-format handles, RFC 5737 addresses, RFC 2606 domains.
2. Write or reuse a grader in the world's `graders/`. Check **final state**, not what the agent said
   it did. Use the world's check library (`graders/common.py`) and its failure types.
3. Add a golden run and at least one plausible wrong run in `graders/runs/<id>.yaml`. The golden run
   must pass; each wrong run must fail with the failure type it declares. A grader that passes a
   known-bad run is a bug.
4. Run the checks:

   ```bash
   uv run sandbox tasks validate
   uv run pytest worlds/<world>/tests
   ```

5. Leave `reviewer_signoff: null`. The world's domain reviewer signs it off.

Every commit is signed off (`git commit -s`, the DCO). See `CONTRIBUTING.md` for the full rules,
including the ones CI enforces: no wall clock or unseeded randomness, no world imports outside
`worlds/`, graders never see the agent's reasoning, and no hidden task in the public repository.

## Contributing a world

Follow the world contract in `docs/DECISIONS.md` and the hello-world example in
`kit/src/kit/examples/hello_world/`. A world needs a committed domain reviewer before it starts.
