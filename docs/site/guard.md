# The guard

The guard (`guard/`, v0.2) checks every high-impact action an agent attempts against what the
user delegated, before it runs. It decides **allow**, **block** or **ask_user** and records why.
It never holds or moves money and never touches a real system: it decides, and records.
Nothing in `guard/` imports a world.

## A policy

A policy is a YAML or JSON file (`guard-policy-v1`, schema
`guard/src/guard/schemas/policy.schema.json`). It names the tools it governs, and for each one
its hard rules:

| Rule | Holds the action when |
| --- | --- |
| `budget.per_action`, `daily`, `monthly` | its amount (minor units) is over the cap; daily and monthly count what this guard already allowed, by simulated UTC day and month |
| `confirm_above` | its amount is above the threshold (ask_user) |
| `counterparties`, `categories` | the value is blocked, needs confirming, or is not on an `allow` list |
| `mandate` | it names no mandate when one is required, or one the user did not allow |
| `time_windows` | it falls outside every window (UTC) |
| `checks` | one of the world's deterministic checks, named by id, fires |

A tool the policy does not name gets `default`. `model_check` says when the decision backend
is asked and the confidence below which its answer becomes ask_user.

```bash
uv run sandbox-guard policy list                    # payments/default, security/default
uv run sandbox-guard policy check payments/default  # validates; prints the id, version and hash
```

The hash is the SHA-256 of the policy's canonical JSON (RFC 8785), so the same policy in YAML
or JSON hashes alike, and every decision record names the exact policy.

**Delegated limits.** In a sandbox run the hard rules use each task's own limits (the payments
world's per-payment, daily and monthly caps, as they stand when the agent acts), not the pack's
numbers. The pack's numbers are defaults for the decorator and the proxy outside the sandbox.
Where both apply, every rule of both holds, so the stricter always wins.

## The layered decision

1. **Hard rules**, always first. A hard block ends the decision: no backend is asked.
2. **A decision backend** for what rules cannot see (does the action match the request? is
   content the agent read steering it?), within the policy's timeout.
3. The decision is the **stricter** of the two. A backend can make a decision stricter; nothing
   it answers can make one looser. A timeout, an error, an answer that is not a valid result, or
   a confidence below `min_confidence` becomes ask_user.

## Backends

| Backend | What it is | Status |
| --- | --- | --- |
| `rules_only` | every world check, no model | runs now |
| `guard-qwen2.5-0.5b` | our model, local inference | pending: not trained yet |
| `jev` | TypeSafe Jev, System One API | pending: needs `TYPESAFE_API_KEY` |
| `openai-decisions` | OpenAI Decisions API, `gpt-6-luna` | pending: needs `OPENAI_API_KEY` |
| `clef`, `clef-flash` | Cloudflare Clef on Workers AI | pending: needs a Cloudflare token and account |
| `strands-decider` | AWS Strands Decider, self-hosted | pending: needs the checkpoint downloaded and served |
| `judge-a`, `judge-b` | general chat models, through LiteLLM | pending: model ids and keys chosen at run time |

Every backend's config (`guard/src/guard/backends/configs/`) pins its model id and version and
names its timeout. Its token use goes in every decision record; prices are in
`training/src/training/eval/prices.yaml`. Only sandbox data is ever sent to a hosted backend.

## Decision records

Each decision is one line of `decisions.jsonl` (`guard-record-v1`): the hashes of the request
and the arguments (never the request itself), the policy's id, version and hash, the delegated
limits' hash, the backend and model version, the decision, its reason codes and confidence,
which layer decided, the latency and the simulated time. Each record holds the previous one's
hash; whoever keeps the log keeps its last hash and length too, so a removed last record shows.

```bash
uv run sandbox-guard verify decisions.jsonl --head <last hash> --count <records>
```

These records become the trust layer's receipts.

## In front of a tool, an MCP server, a sandbox run

- **Python decorator**: `guard.guarded(guard, world=..., instruction=..., clock=..., fields=...)`
  wraps any function; block raises `GuardBlocked`, ask_user calls `confirm` or raises
  `GuardAskUser`.
- **MCP proxy**: `guard.mcp_proxy.GuardProxy` in front of any MCP session, or
  `sandbox-guard proxy --policy payments/default --world payments --instruction "..." --now
  2026-01-01T09:00:00+00:00 --log decisions.jsonl -- <server command>` over stdio.
- **Sandbox runs**:

  ```bash
  uv run sandbox run --world payments --model <model> --tasks payments-v0.1 \
      --guard payments/default --backend rules_only --out runs/payments-rules-guard
  uv run sandbox verify --run runs/payments-rules-guard   # grades and decision records
  ```

  A held call never reaches the world; the agent is told it was blocked, or that it needs the
  user's confirmation (the agent decides whether to ask; the guard never asks itself). Guarded
  runs never go on the main leaderboard.

## TypeScript

`guard/typescript/` implements the same policy format, hard rules, layered decision and records,
and passes the same shared test vectors (`guard/test-vectors/`) as Python. The model adapters
and the MCP proxy are Python only.

```bash
cd guard/typescript && npm ci && npm run build && npm test
```

## The Guard models track

Each backend is scored behind the guard on identical inputs:

- on the action dataset (`sandbox-train eval --guard '{world}/default'`): correct blocks,
  wrongful blocks, missed mistakes, ask_user rate, calibration, latency p50/p99, cost per 1,000
  decisions, robustness under stress, for the public test split and the hidden set
  (aggregates only);
- in sandbox runs (`sandbox run --guard`): task success with the guard on, beside the same
  agent's task success without it, the block and ask_user rates, and whether every decision
  record verified.

`sandbox-train report ... --runs <guarded runs> --baseline-runs <unguarded runs>` writes
`guards/results.json`. A guard that blocks everything is not a win: task success is always
shown next to the block rate.

## Overhead

Measured on 2026-10-25 on an AMD Ryzen 7 7435HS (16 threads, Python 3.12.14), rules only,
behind each world's default pack, record writing included: p50 0.64 ms and p99 1.57 ms per
decision over the 8,337 rows of the `actions-v0.1` test split. A CI test holds both under 5 ms
(300 decisions to warm up, then 3,000). Model-mode latency on CPU and on a GPU is not measured
yet.

## Running the parts that need keys, a GPU or a download

Every command below is run by whoever holds the key or the machine; nothing in CI calls a
model. Keep run directories outside git (`runs/` is ignored). For each backend `<name>`:

```bash
# 1. On the dataset, behind the guard (public test split; then the hidden set, aggregates only):
uv run sandbox-train eval --backend <name> --guard '{world}/default' \
    --data training/data/out/actions-v0.1 --out runs/eval/<name>-test-guard.json
HIDDEN_TASKS_DIR=/path/to/agent-sandbox-hidden uv run sandbox-train eval --backend <name> \
    --guard '{world}/default' --hidden --data training/data/out/actions-v0.1 \
    --out runs/eval/<name>-hidden-guard.json

# 2. In sandbox runs, for each world, with the scripted agents and with a real agent model:
for world in payments security; do
  for agent in fake:golden fake:wrong; do
    uv run sandbox run --world $world --model $agent --tasks $world-v0.1 \
        --guard $world/default --backend <name> --out runs/guard/<name>-$world-${agent#fake:}
    uv run sandbox verify --run runs/guard/<name>-$world-${agent#fake:}
  done
done
```

What each backend needs first:

| Backend | Before running |
| --- | --- |
| `jev` | `export TYPESAFE_API_KEY=...` |
| `openai-decisions` | `export OPENAI_API_KEY=...` |
| `clef`, `clef-flash` | `export CLOUDFLARE_AUTH_TOKEN=... CLOUDFLARE_ACCOUNT_ID=...` |
| `strands-decider` | in its own environment: `pip install strands-decider==0.2.0`, then `strands-decider serve StrandsAgents/strands-decider-2B-qwen3.5-v1-2610 --port 8000` (downloads the weights) |
| `judge-a`, `judge-b` | `uv sync --all-packages --inexact --extra litellm`, the provider's key, and `--set model=<provider>/<model>` on every command |
| `guard-qwen2.5-0.5b` | train it (`training/src/training/guard_model/README.md`); install `requirements-train.txt` in this environment; model-mode latency on CPU and on a GPU is the eval's `latency_ms`, run once with `--set device=cpu` and once with `--set device=cuda --set dtype=float16` |

A real agent behind the guard: `--model <provider>/<model>` with the `litellm` extra and its key,
plus the same command without `--guard` for task success without it. The hidden set:
`HIDDEN_TASKS_DIR=... uv run sandbox run --tasks <world>-v0.1-hidden --guard ...`.

Then the leaderboard file, from the eval results and the run directories:

```bash
uv run sandbox-train report runs/eval/*.json --runs runs/guard/* --baseline-runs runs/plain/* \
    --out runs/report --updated YYYY-MM-DD
```

Before publishing any number from a hosted backend, check its provider's terms (OpenAI's could
not be read and TypeSafe's were not found, decision 239; Cloudflare's require everything
needed to replicate a disclosed benchmark). Publishing `guards/results.json` is the repo
owner's step.

## Not done yet

Our model is not trained, and no outside backend has been run. Model-mode latency on CPU and
on a GPU is not measured, and no hidden-set guard run has been done. The leaderboard track is
not live. The drug-safety pack waits on that world and its reviewer.
