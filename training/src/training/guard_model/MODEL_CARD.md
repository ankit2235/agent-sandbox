---
license: apache-2.0
base_model: Qwen/Qwen2.5-0.5B
library_name: peft
tags: [agent-safety, guardrails, synthetic-data, research]
---

# agent-sandbox guard (Qwen2.5-0.5B, LoRA): model card

**Status: not trained yet.** No weights exist. Every number below is pending a training
run and the evaluation in `docs/blog/guard-model.md`; nothing here is measured.

## What it is

A LoRA adapter on [Qwen/Qwen2.5-0.5B](https://huggingface.co/Qwen/Qwen2.5-0.5B) (Apache-2.0,
revision `060db6499f32faf8b98477b0a26969ef7d8b9987`) that reads one attempted high-impact
agent action, with the user's instruction, the delegated policy, the relevant world state
and the recent trace, and decides **allow**, **block** or **ask_user**, with reason codes and
a calibrated confidence. A larger variant on Qwen/Qwen2.5-1.5B (revision
`8faed761d45a263340a0528343f099c05c9a4323`) has a config and has not been trained either.

The agent-sandbox maintainers built this model and the benchmark it is scored on.

## Training data

`actions-v0.1` (its dataset card): attempts from the sandbox's simulated **payments** and
**security operations** worlds, labelled from the worlds' graders, split by task family.
Synthetic only. The hidden test set was never used for training; training rows that were
near-duplicates of hidden rows were removed. The drug-safety world does not exist yet, so
the model has never seen a drug-safety action.

## Intended use

Research: studying guards in front of agents' high-impact actions, as a baseline for the
sandbox's Guard models leaderboard.

## Limits

- **A research model trained on synthetic data. It is not validated for real money, real
  systems or real patients**, and must not be put in front of any.
- It knows two simulated worlds, one simulated company and English only.
- Its labels come from graders that the worlds' domain reviewers have not yet signed off.
- It was trained only on prompts up to 12,000 characters (about 94% of the training rows);
  longer requests at inference are outside what it saw.
- Hidden-set tasks share families with its training data: hidden results are reported by
  whether the family was trained on.

## Evaluation

Pending: held-out public split and hidden set, with intervals over families, robustness
under stress and wrongful-block rates, against the rules-only baseline, two LLM judges and
the decision models (`docs/blog/guard-model.md`). The report opens with where this model
loses.

## How it decides

The decision is the most probable of the three answer openings; its probability is the
confidence, then temperature-scaled on the validation split. Below the fitted threshold,
the answer becomes ask_user: a low-confidence case is never a silent allow.
