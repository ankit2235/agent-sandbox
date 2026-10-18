# agent-sandbox

An open-source, [OpenEnv](https://github.com/huggingface/OpenEnv)-compatible sandbox platform for
measuring how well AI agents do real work inside simulated **worlds**. One shared, domain-neutral
kit; each world is one domain built on top of it.

An agent receives a task, acts **only** through that world's tools, and is scored by deterministic
graders that read the world's final state and the trace of what the agent did.

```bash
just setup          # uv sync: Python 3.12 + the workspace
just test           # the whole suite
just run-example    # one scripted episode in the hello-world world, graded
just worlds         # what is installed
```

## What this is

- A **research and evaluation** tool. Worlds are simulations: fake rails, fake merchants, fake
  logs, fake reports.
- **Deterministic.** Seeded randomness, a simulated clock, no internet inside a world. The same
  agent actions produce the same score, always.
- **Graded by plain Python** over final state plus trace. An LLM judge is used only where nothing
  else works, and is flagged when it is.
- **Domain-neutral at the core.** `kit/`, `runner/`, `stress/` (later `guard/`, `trust/`, the
  leaderboard) never import a world. Worlds register themselves through the
  `agent_sandbox.worlds` entry point, and CI fails if that line is crossed.

## What this is not

- Not a product, not a business, and not connected to any real system. No real money, no real
  customer data, no real networks, no real security tooling, ever.
- Not a source of medical, financial or legal advice. The drug-safety world, when it arrives, is a
  research benchmark and is **not validated for real pharmacovigilance**.
- Not a place for offensive security material. The security world is defensive only: no attack
  tooling, no exploit code, no real indicators.
- Not a leaderboard you should read as a product ranking. Scores come with confidence intervals and
  a list of what the benchmark does *not* test.

## Worlds

| World | Status | What the agent does |
| --- | --- | --- |
| `hello_world` | shipped (example) | Counts to a target. Exists to prove the kit's interfaces. |
| `payments` | Phase 1–3 | Pays through simulated UPI-inspired rails, buys from unreliable merchants, chases refunds and disputes. |
| `security` | Phase 3S | Works an alert queue as a SOC analyst: triage, investigate, contain, escalate, report. Defensive only. |
| `drug_safety` | Phase 3P, gated | Processes synthetic adverse-event reports: validity, duplicates, coding, seriousness, deadlines. Starts only once a pharmacovigilance physician has committed as reviewer. |

## Layout

```
kit/          core types, episode loop, trace, clock, rng, world registry, OpenEnv adapter
worlds/       one directory per domain; nothing outside worlds/ imports them
stress/       generic, seeded modifiers that make any task harder
runner/       the `sandbox` CLI: tasks, episodes, traces, scoring
docs/         DECISIONS.md, PROGRESS.md, and the docs site
```

## Status

Phase 0 of the build plan: the repo, the workspace and the core interfaces, with a hello-world
world proving them end to end — including through the OpenEnv adapter. The interfaces are tagged
`kit-v0.1` once the pair leads have reviewed them; see [docs/PROGRESS.md](docs/PROGRESS.md) for
what exists today and [docs/DECISIONS.md](docs/DECISIONS.md) for why.

## License

Apache-2.0. See [LICENSE](LICENSE).
