# Training the guard model

Nothing here has been run yet: no weights were downloaded and no model was trained. These
are the steps for whoever runs it. Every step that needs a GPU, a download or a key is
yours to run; CI never does any of them.

## 0. On your machine (no GPU): the data

```bash
export HIDDEN_TASKS_DIR=/path/to/agent-sandbox-hidden      # the private repository
uv run sandbox-train dataset build --config full --out training/data/out --workers 6 --decontaminate
uv run sandbox-train dataset check training/data/out/actions-v0.1
uv run sandbox-train dataset hidden-check training/data/out/actions-v0.1
uv run sandbox-train dataset sft training/data/out/actions-v0.1 --out training/data/out/sft
```

`training/data/out/sft/` now holds `sft-train.jsonl`, `sft-validation.jsonl` and
`grpo-train.jsonl`: plain prompt-completion rows, no test split, nothing from the hidden
set. Those three files are all the GPU machine needs.

## 1. Stage A pilot on a free T4 (Kaggle or Colab): Qwen2.5-0.5B

Estimate, **not measured**: 8–12 GB of GPU memory (fp16 weights about 1 GB, LoRA r=16,
sequence length 4,096, micro-batch 1, gradient checkpointing) and 4–8 hours per epoch for
roughly 30–40M tokens (about 10,000 training rows of 2,500–4,000 tokens). That is near a free
session's limit: check the platform's current GPU and session limits before you start, and
use `save_steps` checkpoints to resume.

1. Upload `training/data/out/sft/` as a **private** dataset on the platform (yours to do).
2. In a notebook with a T4:

```bash
git clone https://github.com/ankit2235/agent-sandbox && cd agent-sandbox && git checkout phases-1-10
pip install -r training/src/training/guard_model/requirements-train.txt
pip install --no-deps ./training
mkdir -p training/data/out && ln -s /path/to/uploaded/sft training/data/out/sft
python -m training.guard_model.sft --config training/src/training/guard_model/configs/sft-kaggle-t4-qwen2.5-0.5b.yaml --check
```

3. Measure first: copy the config, set `max_steps: 200`, run it without `--check`, and note
   the step time and peak memory (`nvidia-smi`). Then run the full config:

```bash
python -m training.guard_model.sft --config training/src/training/guard_model/configs/sft-kaggle-t4-qwen2.5-0.5b.yaml
```

The adapter lands in `training/data/out/guard-qwen2.5-0.5b-sft/`. Download it.

**Known risk, unverified**: `torch==2.14.1` resolves to CUDA 13 wheels. Check that the
T4 (compute capability 7.5) is supported by that build on the platform; if not, use the
platform's preinstalled torch and pin the version you used in the run's notes.

## 2. Stage A on one rented GPU: Qwen2.5-1.5B

Estimate, **not measured**: 16–22 GB in bf16 at 4,096 tokens, tight on a 24 GB L4 or
A10G (an A100 40 GB is the safe choice), about 2–3 hours per epoch on an A100-class GPU
(longer on an L4). Same steps, with
`configs/sft-rented-l4-qwen2.5-1.5b.yaml`, and a 200-step timing run first.

## 3. Calibrate and evaluate (CPU is enough for 0.5B; any GPU is faster)

```bash
uv pip install -r training/src/training/guard_model/requirements-train.txt   # in the workspace venv, locally only
uv run sandbox-train eval --backend guard-qwen2.5-0.5b --data training/data/out/actions-v0.1 \
  --split validation --predictions training/data/out/eval/guard-val.jsonl --out training/data/out/eval/guard-val.json
uv run sandbox-train calibrate --predictions training/data/out/eval/guard-val.jsonl --out training/data/out/eval/guard-cal.json
uv run sandbox-train eval --backend guard-qwen2.5-0.5b --data training/data/out/actions-v0.1 \
  --calibration training/data/out/eval/guard-cal.json --out training/data/out/eval/guard-test.json
uv run sandbox-train eval --backend guard-qwen2.5-0.5b --data training/data/out/actions-v0.1 \
  --calibration training/data/out/eval/guard-cal.json --hidden --out training/data/out/eval/guard-hidden.json
```

Installing the training requirements into the workspace environment changes it; use a
separate virtualenv if you would rather keep the workspace as CI has it.

## 4. Stage B (optional, pending): GRPO

```bash
python -m training.guard_model.grpo --config training/src/training/guard_model/configs/grpo-qwen2.5-0.5b.yaml --check
python -m training.guard_model.grpo --config training/src/training/guard_model/configs/grpo-qwen2.5-0.5b.yaml
```

The reward (`reward.py`) penalises wrongful blocks and missed mistakes separately.

## 5. Publishing (the owner's, pending)

Weights go up only after the eval report is written, with `MODEL_CARD.md` filled in from
the measured numbers. Nothing here uploads anything.
