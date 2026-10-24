# Quickstart

## Install

```bash
pip install agent-sandbox-bench              # Python 3.12+; the `sandbox` command
pip install 'agent-sandbox-bench[litellm]'   # to run real models through LiteLLM
```

!!! note "Not on PyPI yet"
    The `0.1.0` packages are built and checked in CI, and are published when `payments-v0.1` is
    released. Until then, install from a checkout: `git clone
    https://github.com/prarabdha-soni/agent-sandbox && cd agent-sandbox && uv sync --all-packages`,
    then run every command below as `uv run sandbox ...`.

Or use the Docker image, which includes LiteLLM:

```bash
docker run --rm ghcr.io/prarabdha-soni/agent-sandbox:0.1.0 quickstart --model fake:golden
```

## Ten tasks, one score

```bash
sandbox quickstart --model fake:golden       # no key, no cost: replays each task's golden run
```

That runs ten seeded tasks of `payments-v0.1`, one run each, and prints a score. With a real model
(any id LiteLLM accepts; set your provider's key first):

```bash
export OPENAI_API_KEY=...
sandbox quickstart --model openai/gpt-4.1-mini --max-cost 0.50
```

The run directory is `runs/quickstart/<model>/`; run the same command again to resume. Check that
every grade reproduces from the recorded actions:

```bash
sandbox verify --run runs/quickstart/openai-gpt-4-1-mini
```

## The whole set

```bash
sandbox run --world payments --tasks payments-v0.1 --model openai/gpt-4.1-mini \
    --runs 3 --concurrency 4 --max-cost 0.50 --out runs/gpt-4.1-mini/
```

- 50 tasks, 3 runs each. Resumable: rerun the command after an interruption.
- Caps per run: the task's step cap (or `--max-steps`), `--max-tokens`, `--max-cost` in USD.
- Each run records `trace.jsonl` (every message, tool call, response, state change and simulated
  time) and `result.json` (grade, tokens, cost, steps, stop reason).

## Reproduce a published score

The leaderboard's numbers come from run directories that are attached to the
[GitHub release](https://github.com/prarabdha-soni/agent-sandbox/releases) as an archive. To check
one model's score yourself:

```bash
tar xzf payments-v0.1-runs.tar.gz                 # from the release's assets
sandbox submission check payments-v0.1-runs/<model>/ --min-runs 3
sandbox export payments-v0.1-runs/*/ --out my-data --updated 2026-11-01
```

`submission check` replays every run and confirms every grade; `my-data/payments/results.json`
then holds the leaderboard's public numbers (its hidden-set aggregates come from runs that stay
private, so they are not in the archive). To rerun a sample of the tasks with the model itself,
which costs money:

```bash
sandbox submission spot-check payments-v0.1-runs/<model>/ --sample 10 --seed 7
```

## Other worlds and task sets

```bash
sandbox worlds                                    # what is installed
sandbox run --world security --tasks security-v0.1 --model ... --out ...
sandbox run --world payments --tasks payments-v0.1-stressed --model ... --out ...
```
