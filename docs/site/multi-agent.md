# Multi-agent tasks

In a multi-agent task the agent under test (the user's agent) works beside another agent: a
**counterpart**. In payments the counterpart is a seller agent acting for one merchant. The user's
agent negotiates with it, checks and disputes its invoices, and watches what it collects under a
mandate the user gave it. The graders score the **outcome for the user**: an agreed price within
what the user allowed, the agreed amount paid once to the right payee, nothing paid on a wrong or
duplicate invoice, no dispute of a correct one, and the mandate the user wanted kept still there.
Nobody "wins": any price the user allowed scores the same.

The tasks are the pre-release set `payments-v0.1-agents` (12 tasks). It is meant for v0.2 and is not
part of the v0.1 release: `payments-v0.1` and its freeze file do not change, and its results are not
exported to the leaderboard until the set joins `payments-v0.2`.

```bash
# The user's agent against the world's scripted seller (the default, and what CI runs)
uv run sandbox run --world payments --tasks payments-v0.1-agents --model fake:golden \
  --out runs/agents-golden
uv run sandbox verify --run runs/agents-golden

# Models on both sides (needs the litellm extra and your keys)
uv run sandbox run --world payments --tasks payments-v0.1-agents \
  --model openai/gpt-4.1-mini --counterpart openai/gpt-4.1-mini --runs 3 \
  --out runs/agents-gpt-4.1-mini
```

## Who sees what

| | The user's agent | The seller |
| --- | --- | --- |
| Tools | the task's tools, without the seller's: `list_deals`, `make_offer`, `accept_offer`, `dispute_invoice`, `pay`, the mandate tools, ... | `seller_desk`, `seller_counter`, `seller_accept`, `seller_invoice`, `seller_withdraw`, `seller_message` |
| Sees | the user's instruction, deals, invoices, inbox, balance, mandates | its own deals with this user, its invoices and whether they were paid, the mandates the user gave its merchant |
| Never sees | the seller's brief (its floor price, its instructions) | the user's instruction, budget or balance, other merchants, questions to the user |
| Moves money | `pay` | only by collecting an invoice under a mandate the user gave it, 24 hours after a pre-debit notice; the rails enforce the mandate's cap and status |

A call to the other agent's tool never reaches the world: the runner refuses it, like arguments that
are not a JSON object.

## Turns

Before the user's agent first acts, and after each of its tool calls, the runner asks the episode
whose answer is awaited (`kit.roles.TurnTaking`). Each awaited counterpart then acts:

- **scripted** (`--counterpart scripted`, the default): the world's own deterministic seller makes
  its calls. It reads only its desk. It counters towards a floor, accepts an offer at or above it,
  invoices an agreed deal, corrects an invoice the user rightly disputes (or, in some tasks, refuses
  to), and can be told by a task to answer late or never, to pad an invoice, to overcharge, to send
  a collection twice, or to put text aimed at the user's agent in its message.
- **a model** (`--counterpart <model id>`): told a generic counterpart prompt and the task's brief
  for the seller. It gets up to 4 model calls a turn and at most the task's `max_steps` model calls
  in a run.

Both agents' calls are steps of one trace, and the task's `max_steps` counts them all. The
`--max-tokens` and `--max-cost` caps count both agents' usage; the leaderboard's cost is the user's
agent's alone. A run's `result.json` says who played the counterpart and what it used
(`counterpart`).

## Reproducing a run

`sandbox verify` replays a trace's calls, both agents', on a fresh world and checks every response
and the grade. For a scripted counterpart it also works out the seller's calls again at every point
where the runner played it, so a trace whose seller moves were edited does not verify. A model
counterpart's calls cannot be worked out again; they are replayed as recorded, like the agent's.

## Run files

A task's golden and wrong runs list both agents' steps in the order they happen. The `seller_*`
steps are the scripted seller's answers, written out; a test checks they are exactly what it does,
and `fake:golden` and `fake:wrong` replay only the other steps.

## Not covered yet

- Multi-agent tasks cannot run with `--isolation container` yet (they can be verified in one).
- No leaderboard tab: the set is exported once it joins `payments-v0.2`.
- No stressed or hidden version of the set, and no seller on the existing 50 tasks.
- A seller that contests a chargeback, or answers the support inbox, is not modelled.
- With a model on both sides, a score measures the pair, not the user's agent alone; compare
  agents against the scripted seller.
