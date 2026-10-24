# Concepts

## Worlds

A **world** is one simulated domain: its services, the tools an agent may call, its tasks and its
graders. Payments simulates UPI-inspired rails, merchants and after-payment operations; security
simulates a small company's logs, alert queue, containment API and tickets. Each world registers
itself with the kit through the `agent_sandbox.worlds` entry point, and nothing outside `worlds/`
imports one.

A world's services are in-process apps over SQLite or JSON stores. Each can snapshot and restore
its state exactly, so every episode starts from the same bytes.

## Tasks

A **task** is one job for the agent: an instruction in plain words, the world's initial state,
the tools it may use, a step cap, a seed and the grader that decides it. Tasks are YAML files
packaged with their world; see [Task format](task-format.md). A **task set** is one world's tasks
of one version, for example `payments-v0.1`, and every result carries that label.

## Episodes and the clock

An **episode** is one run of one task:

1. The world's services load the task's initial state; the simulated clock and the seeded random
   streams are set; every store is snapshotted.
2. The agent calls one tool per step through MCP. Each call can change the world's state and is
   recorded as a trace step: tool, arguments, response, rows changed and simulated time.
3. Scheduled events (a cap that changes, a merchant's reply, a new alert) fire at their simulated
   time.
4. When the agent stops, or a cap is hit, the grader reads the final state and the trace.

There is no wall clock and no unseeded randomness inside a world, and no network.

## Traces

A **trace** is the full record of an episode, written as JSON lines. Graders see a view of it
without the agent's messages or reasoning. `sandbox verify` replays a trace's tool calls on a fresh
world and checks that every response and the grade come out the same.

## Graders

A **grader** is plain Python over the final state and the trace. It returns a pass or fail, a
score and a list of named checks, and names a **failure type** (for example
`payments.overspend`) when the run fails. See [How graders work](graders.md).

## Stress modifiers

A **stress modifier** makes a task harder without changing the job: a misleading page, a
conflicting note, time pressure, a flaky service, a limit that shifts mid-task, a fake offer, a
look-alike payee. The instruction and the grader stay; what the grader must also check travels in
the stressed task. `payments-v0.1-stressed` is the public stressed set.

## Hidden tasks

A **hidden set** is a private set of tasks, kept in a separate repository, that no model has seen.
This repository holds only its hashes, so anyone can check later that the set did not change.
Hidden scores are published only as an aggregate next to the public score. In v0.1 a hidden task is
a seeded variant of a public task under public modifiers, which catches copied answers but not
transfer to unseen kinds of work.

## OpenEnv

Any world can be served through [OpenEnv](https://github.com/huggingface/OpenEnv) (`pip install
'agent-sandbox-bench[openenv]'`): one action type, a tool call; the grader's score is the final
step's reward; the task sets are the environment's splits; `reset` lists the tools the task
allows. The payments world is packaged as an OpenEnv environment for the Hugging Face hub in
`worlds/payments/openenv/`.
