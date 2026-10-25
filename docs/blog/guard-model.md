# An open guard model trained on sandbox data: evaluation report

**Status: pending.** The dataset, the baselines, the training configs and the evaluation are
built and tested with fake and local stand-ins. No model has been run, trained or measured:
the only numbers here are the rules-only baseline's, which needs no model. Every other
section says what will go in it and how it is produced.

The agent-sandbox maintainers built the guard model and the benchmark it is scored on.
Every backend gets identical inputs and the results reproduce from the published configs
(`guard/src/guard/backends/configs/`, `training/src/training/guard_model/configs/`).

## Where our model loses

Written first, from `sandbox-train report` (`eval-report.md` opens with it): every metric
and row set where another backend beats ours. Pending real runs.

## The question

Before an agent's high-impact action runs (a payment, a checkout, a mandate, a refund
request; isolating, reimaging, disabling, resetting, blocking), should a guard **allow** it,
**block** it, or **ask the user**? The guard sees the user's instruction, the delegated
policy, the world state around the action, the recent trace, what the agent read, and the
action.

## The data

`actions-v0.1` (`training/data/actions-v0.1/DATASET_CARD.md`): attempts from the payments
and security worlds' scripted runs, their template variants and their stressed variants,
labelled from the worlds' graders, split by task family, decontaminated against the hidden
set. Drug safety is pending: that world does not exist yet.

## Backends

| Backend | Kind | Model and version | Status |
|---|---|---|---|
| rules_only | the worlds' deterministic rules (named `rules` before Phase 8) | `rules` | measured below |
| judge-a, judge-b | LLM judges through LiteLLM | chosen at run time, two providers | pending |
| jev | TypeSafe Jev, System One API | `jev-latest`; version as served | pending (key) |
| openai-decisions | OpenAI Decisions API | `gpt-6-luna` | pending (key) |
| strands-decider | AWS Strands Decider 2B, self-hosted | `strands-decider-2B-qwen3.5-v1-2610@216d1cc` | pending |
| clef, clef-flash | Cloudflare Clef, Workers AI | `@cf/cloudflare/clef`, `-flash` | pending (key) |
| guard-qwen2.5-0.5b | ours, LoRA on Qwen2.5-0.5B | `060db64` + adapter | pending training |

Galileo Luna-2 is not included: it needs a Galileo account, which we do not have.

## Measured so far: the rules-only baseline

The rules-only baseline needs no model, so it is the one backend measured (2026-10-25, on the
full `actions-v0.1` build, on the build machine's CPU). Rates are means over families with a 95%
bootstrap interval over families (half-width shown).

| Set | Rows (families) | Accuracy | Mistakes stopped | Missed | Wrongful blocks | Needless asks | Ask rate | ECE |
|---|---|---|---|---|---|---|---|---|
| Public test | 8,337 (12) | 29.0% ± 19.3 | 59.3% ± 28.2 | 40.7% ± 28.2 | 1.0% ± 1.4 | 19.2% ± 23.8 | 44.6% ± 23.4 | 0.402 |
| Hidden, all | 122 (29) | 47.1% ± 13.9 | 51.8% ± 17.0 | 48.2% ± 17.0 | 3.2% ± 3.9 | 1.9% ± 2.8 | 30.0% ± 16.0 | 0.287 |

- By world on the public test split: in payments the rules stop 4.2% of mistakes (they miss
  95.8%: budgets stated in words, look-alike payees and planted instructions are invisible to
  them); in security they stop 96.1% but send 72.5% of all decisions to the user.
- Hidden set by family: rows whose family was in train, 101 (accuracy 52.7%); in validation,
  12 (42.6%); in test, 8 (26.7%). One hidden family with no rows left in the dataset is
  reported as a count only, as is any bucket of fewer than three families.
- Robustness on the public test split: accuracy 43.7% without stress and 34.6% with it, over
  the 7 families that have both, a drop of 9.1 points ± 8.9.
- Latency per decision: p50 0.04 ms, p99 0.14 ms (in-process, CPU). No cost.

## Results: held-out public split

Pending. Accuracy, correct blocks, mistakes stopped, missed mistakes, wrongful blocks,
needless asks and the ask_user rate, each a mean over families with a 95% bootstrap
interval over families, by world and category; ECE; latency p50 and p99 (CPU and one small
GPU); cost per 1,000 decisions at the prices in `training/src/training/eval/prices.yaml`
(each dated). Task success with the guard on is Phase 8's.

## Results: hidden set

Pending. Aggregates only, split by whether each hidden task's family was in train,
validation or test: the hidden set is built from the public families, so a hidden result
on a family the model trained on measures something different from one on a held-out
family. Both are reported, and labelled.

## Robustness

Pending. The same families with and without stress modifiers designed to flip the
decision (planted instructions, look-alike payees, misleading notes, decoy anomalies), and
the drop.

## Calibration

Pending. Temperature and threshold fitted on the validation split; below the threshold a
decision becomes ask_user, never a silent allow. ECE before and after.

## Limits

A research model trained on synthetic data from two simulated worlds, scored by graders
not yet signed off by the worlds' reviewers. Not validated for real money, systems or
patients.

## Terms

Before any number here is published, each provider's terms on publishing benchmark results
are checked (pending; OpenAI's services agreement and TypeSafe's terms could not be read
when this was written).
