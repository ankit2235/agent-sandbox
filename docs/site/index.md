# agent-sandbox

An open-source, [OpenEnv](https://github.com/huggingface/OpenEnv)-compatible sandbox for measuring
how well AI agents do real work inside simulated **worlds**. An agent gets a task, acts only
through that world's tools (over MCP), and is scored by deterministic graders that read the world's
final state and the trace of what it did.

| World | Task set | Status | What the agent does |
| --- | --- | --- | --- |
| Payments | `payments-v0.1`, 50 public tasks | preview, release candidate | Pays through simulated UPI-inspired rails, buys from unreliable merchants, chases refunds and disputes. |
| Security operations | `security-v0.1`, 50 public tasks | preview | Works an alert queue as a SOC analyst: triage, investigate, contain, escalate, report. Defensive only. |

Both worlds are `preview` until their domain reviewers sign off every grader.

## What it is

- **A research and evaluation tool.** Worlds are simulations: fake rails, fake merchants, fake
  logs. No real money, no real customer data, no connection to any real system.
- **Deterministic.** Seeded randomness, a simulated clock, no internet inside a world. The same
  actions always get the same score, and `sandbox verify` proves it by replaying a run's recorded
  actions.
- **Graded by plain Python**, over final state and trace. No LLM judge.
- **Domain-neutral at the core.** The kit, runner and stress library never import a world.

## Where to start

- [Quickstart](quickstart.md): install, run ten tasks, reproduce a published score.
- [Concepts](concepts.md): worlds, tasks, episodes, traces, graders, stress and hidden sets.
- [Limitations](limitations.md): what the benchmark does **not** test. Read it before reading a
  score.
