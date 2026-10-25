"""Stage B (optional): RL with TRL's GRPOTrainer, rewarded from the grader-derived labels.

TRL is chosen over Prime Intellect's verifiers because verifiers 0.4 needs mcp 2.x and
0.3 conflicts with the lock's litellm and openai (decision 159), and TRL is already the
Stage A library. Run on a GPU machine with ``requirements-train.txt`` (never in CI)::

    python -m training.guard_model.grpo --config configs/grpo-qwen2.5-0.5b.yaml

The data are ``grpo-train.jsonl`` from ``sandbox-train sft-data``: ``prompt``,
``decision`` and ``reason_codes`` (a JSON list). The reward is :mod:`.reward`: wrongful
blocks and missed mistakes are penalised separately. Starts from the Stage A adapter.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .reward import RewardWeights, trl_reward


class GrpoConfig(BaseModel):
    """One RL run, as a YAML file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_model: str
    revision: str
    adapter: Path
    """The Stage A adapter to start from."""
    data: Path
    output_dir: Path
    dtype: Literal["float16", "bfloat16", "float32"] = "float16"
    num_generations: int = Field(default=4, ge=2)
    max_completion_length: int = Field(default=64, ge=8)
    learning_rate: float = Field(default=1e-6, gt=0)
    beta: float = Field(default=0.0, ge=0)
    per_device_batch_size: int = Field(default=4, ge=1)
    gradient_accumulation_steps: int = Field(default=4, ge=1)
    max_steps: int = Field(default=500, ge=1)
    logging_steps: int = Field(default=10, ge=1)
    save_steps: int = Field(default=100, ge=1)
    seed: int = 0
    weights: RewardWeights = Field(default_factory=RewardWeights)

    @classmethod
    def load(cls, path: Path) -> GrpoConfig:
        """Read a config."""
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def read_rows(path: Path) -> list[dict[str, str]]:
    """GRPO rows from one file, checked."""
    rows: list[dict[str, str]] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            if set(row) != {"prompt", "decision", "reason_codes"}:
                msg = f"{path}:{number}: a row must have prompt, decision and reason_codes"
                raise ValueError(msg)
            json.loads(row["reason_codes"])
            rows.append(row)
    if not rows:
        msg = f"{path} has no rows"
        raise ValueError(msg)
    return rows


def train(config: GrpoConfig) -> Path:
    """Run GRPO from the Stage A adapter and save it. Needs the training requirements."""
    import torch
    from datasets import Dataset
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from trl import GRPOConfig as TrlGrpoConfig
    from trl import GRPOTrainer

    tokenizer = AutoTokenizer.from_pretrained(config.base_model, revision=config.revision)
    base = AutoModelForCausalLM.from_pretrained(
        config.base_model, revision=config.revision, dtype=getattr(torch, config.dtype)
    )
    model = PeftModel.from_pretrained(base, str(config.adapter), is_trainable=True)
    args = TrlGrpoConfig(
        output_dir=str(config.output_dir),
        num_generations=config.num_generations,
        max_completion_length=config.max_completion_length,
        learning_rate=config.learning_rate,
        beta=config.beta,
        per_device_train_batch_size=config.per_device_batch_size,
        gradient_accumulation_steps=config.gradient_accumulation_steps,
        max_steps=config.max_steps,
        logging_steps=config.logging_steps,
        save_steps=config.save_steps,
        fp16=config.dtype == "float16",
        bf16=config.dtype == "bfloat16",
        seed=config.seed,
        remove_unused_columns=False,
        report_to="none",
    )
    trainer = GRPOTrainer(
        model=model,
        reward_funcs=trl_reward(config.weights),
        args=args,
        train_dataset=Dataset.from_list(read_rows(config.data / "grpo-train.jsonl")),
        processing_class=tokenizer,
    )
    trainer.train()
    trainer.save_model(str(config.output_dir))
    (config.output_dir / "guard-grpo-config.json").write_text(
        config.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    return config.output_dir


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m training.guard_model.grpo --config <file> [--check]``."""
    parser = argparse.ArgumentParser(prog="python -m training.guard_model.grpo")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--check", action="store_true", help="validate the config and the data only; no training"
    )
    args = parser.parse_args(argv)
    config = GrpoConfig.load(args.config)
    rows = read_rows(config.data / "grpo-train.jsonl")
    sys.stdout.write(f"{config.base_model}@{config.revision[:12]}: {len(rows)} prompts\n")
    if args.check:
        return 0
    sys.stdout.write(f"saved to {train(config)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
