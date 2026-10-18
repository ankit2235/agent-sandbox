# Contributing

Thanks for helping. Two rules matter more than the rest: **a run must be reproducible**, and **the
core must stay domain-neutral**.

## Developer Certificate of Origin

Every commit must be signed off, certifying the [DCO](https://developercertificate.org/):

```bash
git commit -s -m "kit: add the thing"
```

That adds a `Signed-off-by:` line. Pull requests with unsigned commits cannot be merged.

## Getting set up

```bash
just setup           # uv sync, Python 3.12
just ci              # exactly what CI runs: lint, type-check, tests
uv run pre-commit install
```

## The rules CI enforces

1. **No wall clock, no unseeded randomness.** Time comes from `SimClock`, randomness from
   `SeededRNG`. `kit/tests/test_purity.py` scans the source and fails on `time.time()`,
   `datetime.now()`, `random.*`, `uuid.uuid4()` and friends. If a file genuinely needs the
   primitive, mark the line `# purity-allow` and say why.
2. **The core never imports a world.** `kit/`, `runner/` and `stress/` must not import `worlds.*`,
   and one world must not import another. See `kit/tests/test_core_isolation.py`.
3. **Graders never see the agent's reasoning.** A grader is handed a `GraderTrace`, which has no
   field for it. Do not work around this.
4. **Every task needs a golden run and a plausible wrong run.** The golden run must pass; the wrong
   run must fail. A grader that passes a known-bad run is a bug.
5. **Synthetic data only.** Test-format identifiers, documentation IP ranges
   ([RFC 5737](https://datatracker.ietf.org/doc/html/rfc5737)) and example domains
   ([RFC 2606](https://datatracker.ietf.org/doc/html/rfc2606)). No real handles, card numbers, IPs,
   usernames, patient data or malware hashes.
6. **Hidden tasks never live here.** They live in a separate private repository, found through
   `HIDDEN_TASKS_DIR`.

## Adding a task

1. Write `worlds/<world>/tasks/<category>/<id>.yaml` matching `TaskSpec` (see
   `kit/src/kit/examples/hello_world/tasks/` for the shape).
2. Write or reuse a grader in `worlds/<world>/graders/`. Checks go over **final state**, not over
   what the agent said it did.
3. Add a golden run and at least one plausible wrong run.
4. Run `uv run sandbox tasks validate` and `just test`.
5. Get the world's domain reviewer to sign off, and record it in `reviewer_signoff`.

## Adding a world

Follow the world contract in `docs/DECISIONS.md` and the hello-world example, which is the
template: services implementing `WorldState`, a tool router, an episode, tasks, graders, failure
types namespaced with the world's name, and a `WORLD.md` stating what it does **not** test. A world
needs a committed domain reviewer before it starts. If you find yourself needing a change to the
kit, stop and propose it — a world-shaped kit is a kit bug.

## Pull requests

Small, with tests, and with a line in `docs/DECISIONS.md` for any design decision. Update
`CHANGELOG.md` and `docs/PROGRESS.md` when you finish a piece of work.
