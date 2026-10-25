# Task format

A task is one YAML file, `worlds/<world>/src/worlds/<world>/tasks/<category>/<id>.yaml`, inside
the world's package so it ships in the wheel. The file name is the task's id, and it lives in the
directory of its category. `sandbox tasks validate` checks every packaged task.

```yaml
id: rails-gym-autopay-mandate
version: v0.1
world: payments
category: rails
difficulty: easy
instruction: >-
  Set up autopay for my gym. FitBox (fitbox@sandbox) charges ₹1,200 every month. Don't let them
  take more than that in one go.
initial_state:
  user: asha@sandbox
  rails:
    accounts:
      - {handle: asha@sandbox, holder: Asha Example, balance_paise: 5000000}
      - {handle: fitbox@sandbox, holder: FitBox Gym, kind: merchant}
allowed_tools:
  [get_balance, list_transactions, list_mandates, get_limits, pay, create_mandate, wait, ask_user]
max_steps: 10
grader: worlds.payments.graders.rails:gym_autopay_mandate
seed: 202
tags: [mandate, recurring]
visibility: public
expected_outcome: >-
  One recurring monthly mandate to fitbox@sandbox capped at exactly ₹1,200 per debit. Nothing is
  paid now: the gym collects through the mandate.
reviewer_signoff: null
```

| Field | Meaning |
| --- | --- |
| `id` | Lowercase slug, unique in the world; also the file name. |
| `version` | The task set's version, `v0.1`. Every task of a set has the same one. |
| `world`, `category` | Which world, and one of its categories (payments: `rails`, `commerce`, `disputes`, `multi`). |
| `difficulty` | `easy`, `medium` or `hard`. |
| `instruction` | What the user asks, in plain words. The only text of the task the agent sees, with the observations its tools return. |
| `initial_state` | The world's state when the task starts, checked against the world's schema (payments: `user`, `rails`, `commerce`, `disputes`, `history`). |
| `allowed_tools` | The tools the agent is offered. Empty means all of the world's tools. |
| `max_steps` | The step cap; a run that hits it is graded as it stands. |
| `grader` | `module.path:callable`, the grader that decides the run. |
| `seed` | Seeds every random stream of the episode. |
| `tags` | Free labels. |
| `visibility` | `public`, or `hidden` for a task of a hidden set (never in this repository). |
| `expected_outcome` | What a correct run ends with, for people reading the task. Graders do not read it. |
| `reviewer_signoff` | `{name, role, date}` of the domain reviewer who signed the task and its grader off, or `null`. |
| `stress` | Set on stressed tasks: the modifiers applied. |

Each world may also ship `tasks/schema.json`, a JSON Schema for whole task files, which
`sandbox tasks validate` applies. The payments and security worlds do.

## Runs: the golden run and the wrong runs

Every task has a run file, `graders/runs/<id>.yaml`: a **golden run** that must pass, and at least
one plausible **wrong run** that must fail with the failure type it declares. CI plays them all.

```yaml
task: rails-gym-autopay-mandate
runs:
  golden:
    steps:
      - tool: create_mandate
        args: {payee: fitbox@sandbox, type: recurring, frequency: monthly, max_amount_paise: 120000}
        expect: {mandate: {type: recurring, frequency: monthly, max_amount_paise: 120000}}
      - {tool: list_mandates}
  wrong_caps_it_at_a_year:
    fails_with: payments.ignored_limit
    steps:
      - tool: create_mandate
        args: {payee: fitbox@sandbox, type: recurring, frequency: monthly, max_amount_paise: 1440000}
```

`expect` checks a step's response contains what is written. An argument can refer to an earlier
response with `$` references, so a run can use an id the world created during the run.

In a multi-agent task the run lists both agents' steps in order: the counterpart's steps (in
payments, the `seller_*` tools) are what the world's scripted counterpart does at that point, and a
test checks it. The agent's steps may only refer to the agent's own earlier steps.

## Multi-agent tasks

A payments task with a seller agent adds two fields to `initial_state` (see
[Multi-agent tasks](multi-agent.md)):

- `commerce.deals`: the deals with the seller, each open (with the seller's opening offer, if any)
  or agreed at a price before the task starts;
- `counterpart`: the seller's merchant, the `brief` a model playing the seller is told, and
  `scripted`, how the world's scripted seller plays (`floor_paise`, `step_paise`,
  `reply_after_seconds`, `silent`, `note`, `included_fees`, `padding`, `overcharge_paise`,
  `collect_under`, `duplicate`, `concedes`).

Its `allowed_tools` list both agents' tools; the agent under test is offered only its own. Such
tasks live in an extra task set beside the world's public one (`WorldSpec.task_sets`), never in
`tasks/`: `payments-v0.1-agents` is `worlds/payments/src/worlds/payments/agent_tasks/`.

## Versions and freezing

A task set's content is frozen at release: `worlds/<world>/src/worlds/<world>/tasks/frozen-<version>.json`
lists every public task's hash, and CI fails if a task of a released version changes. A reviewer's
sign-off is not part of the hash, so signing a task off does not unfreeze it or invalidate a run
made before. A changed task means a new version.
