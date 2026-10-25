---
title: agent-sandbox payments
emoji: 💸
colorFrom: indigo
colorTo: green
sdk: docker
pinned: false
app_port: 8000
base_path: /web
tags:
  - openenv
  - agents
  - benchmark
license: apache-2.0
---

# agent-sandbox: payments (`payments-v0.1`)

The payments world of [agent-sandbox](https://github.com/prarabdha-soni/agent-sandbox) as an
OpenEnv environment: simulated, UPI-inspired payment rails, merchants with reliability profiles,
refunds, disputes and a support inbox. An agent gets a user's request and acts only through the
world's tools; a deterministic grader scores the final state, and that score is the reward of the
step that ends the episode.

Everything is synthetic. No real payment system, money or customer data is involved, and the world
has no network access. It is a research benchmark, not a payments product. The world is in
**preview**: its graders wait for a payments domain reviewer's sign-off.

## Use it

```python
from payments_env import PaymentsAction, PaymentsEnv

with PaymentsEnv(base_url="http://localhost:8000").sync() as env:
    result = env.reset(task_id="rails-gym-autopay-mandate")
    print(result.observation.text)  # the user's request
    tools = {t["name"]: t["input_schema"] for t in result.observation.tools}
    result = env.step(
        PaymentsAction(
            tool="create_mandate",
            args={
                "payee": "fitbox@sandbox",
                "type": "recurring",
                "frequency": "monthly",
                "max_amount_paise": 120000,
            },
        )
    )
    result = env.step(PaymentsAction(tool="finish_episode"))
    print(result.reward, result.metadata["grade"]["passed"])  # 1.0 True
```

- **Action**: `PaymentsAction(tool, args)`, one tool call. `PaymentsAction(tool="finish_episode")`
  ends the episode when the agent has done what was asked, and returns the grade.
- **Observation**: `text` (what the tool returned), `data`, `error_code` (a refused call),
  `sim_time` (the simulated clock), and on reset `tools`, the tools the task allows with their JSON
  Schemas; `done` and `reward` as in OpenEnv.
- **Tasks**: 50 public tasks in one split, `v0.1` (`/payments_env/splits`, `/payments_env/tasks`).
  Pick one with `reset(task_id=...)` or `reset(split="v0.1", index=n)`. The same task and the same
  actions always give the same reward.
- **Reward**: the grader's score in [0, 1] when the episode ends (`finish_episode`, a terminal
  tool such as `ask_user`, or the step cap); 0 on every step before.

Tool reference, task format, scoring and what the world does **not** test:
<https://prarabdha-soni.github.io/agent-sandbox/>.

## Run it locally

```bash
docker build -t openenv-payments:latest -f server/Dockerfile .
docker run -p 8000:8000 openenv-payments:latest
```

or, without Docker, `uv run --project . server`.

## Deploy

From this folder, after `agent-sandbox-bench` 0.1.0 is on PyPI:

```bash
uv lock
openenv validate --level static --skip-build
openenv push --repo-id <namespace>/agent-sandbox-payments
```
