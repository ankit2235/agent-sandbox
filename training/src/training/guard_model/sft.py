"""Stage A: supervised fine-tuning with TRL's SFTTrainer and a LoRA adapter.

Run on a GPU machine where ``requirements-train.txt`` is installed (never in CI)::

    python -m training.guard_model.sft --config configs/sft-kaggle-t4-qwen2.5-0.5b.yaml

The data are the plain prompt-completion files ``sandbox-train sft-data`` writes
(``sft-train.jsonl``, ``sft-validation.jsonl``), so this script needs nothing of the
sandbox but this module. Loss is on the completion only (TRL's default for
prompt-completion data). The adapter and the tokenizer are saved to ``output_dir``,
with the config beside them.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class LoraSettings(BaseModel):
    """The LoRA adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    r: int = Field(default=16, ge=1)
    alpha: int = Field(default=32, ge=1)
    dropout: float = Field(default=0.05, ge=0.0, lt=1.0)
    target_modules: list[str] | Literal["all-linear"] = "all-linear"


class SftConfig(BaseModel):
    """One training run, as a YAML file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_model: str
    revision: str
    """The exact commit of the base model on the Hugging Face Hub."""
    data: Path
    """The folder with ``sft-train.jsonl`` and ``sft-validation.jsonl``."""
    output_dir: Path
    dtype: Literal["float16", "bfloat16", "float32"] = "float16"
    max_length: int = Field(default=2048, ge=128)
    epochs: float = Field(default=1.0, gt=0)
    max_steps: int = -1
    """A positive value stops after that many steps (the timing pilot)."""
    learning_rate: float = Field(default=1e-4, gt=0)
    per_device_batch_size: int = Field(default=2, ge=1)
    gradient_accumulation_steps: int = Field(default=8, ge=1)
    warmup_steps: int = Field(default=50, ge=0)
    logging_steps: int = Field(default=10, ge=1)
    save_steps: int = Field(default=500, ge=1)
    eval_steps: int = Field(default=500, ge=1)
    seed: int = 0
    lora: LoraSettings = Field(default_factory=LoraSettings)

    @classmethod
    def load(cls, path: Path) -> SftConfig:
        """Read a config; relative paths in it are relative to the working directory."""
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def read_rows(path: Path) -> list[dict[str, str]]:
    """Prompt-completion rows from one file, checked."""
    rows: list[dict[str, str]] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            if set(row) != {"prompt", "completion"}:
                msg = f"{path}:{number}: a row must have exactly prompt and completion"
                raise ValueError(msg)
            rows.append(row)
    if not rows:
        msg = f"{path} has no rows"
        raise ValueError(msg)
    return rows


def train(config: SftConfig) -> Path:
    """Fine-tune and save the adapter. Needs the training requirements and a GPU."""
    import torch
    from datasets import Dataset
    from peft import LoraConfig
    from trl import SFTConfig as TrlSftConfig
    from trl import SFTTrainer

    train_rows = read_rows(config.data / "sft-train.jsonl")
    eval_rows = read_rows(config.data / "sft-validation.jsonl")
    args = TrlSftConfig(
        output_dir=str(config.output_dir),
        num_train_epochs=config.epochs,
        max_steps=config.max_steps,
        learning_rate=config.learning_rate,
        per_device_train_batch_size=config.per_device_batch_size,
        per_device_eval_batch_size=config.per_device_batch_size,
        gradient_accumulation_steps=config.gradient_accumulation_steps,
        warmup_steps=config.warmup_steps,
        logging_steps=config.logging_steps,
        save_steps=config.save_steps,
        eval_strategy="steps",
        eval_steps=config.eval_steps,
        max_length=config.max_length,
        completion_only_loss=True,
        fp16=config.dtype == "float16",
        bf16=config.dtype == "bfloat16",
        seed=config.seed,
        report_to="none",
        model_init_kwargs={
            "revision": config.revision,
            "dtype": getattr(torch, config.dtype),
        },
    )
    lora = LoraConfig(
        r=config.lora.r,
        lora_alpha=config.lora.alpha,
        lora_dropout=config.lora.dropout,
        target_modules=config.lora.target_modules,
        task_type="CAUSAL_LM",
    )
    trainer = SFTTrainer(
        model=config.base_model,
        args=args,
        train_dataset=Dataset.from_list(train_rows),
        eval_dataset=Dataset.from_list(eval_rows),
        peft_config=lora,
    )
    trainer.train()
    trainer.save_model(str(config.output_dir))
    (config.output_dir / "guard-sft-config.json").write_text(
        config.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    return config.output_dir


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m training.guard_model.sft --config <file> [--check]``."""
    parser = argparse.ArgumentParser(prog="python -m training.guard_model.sft")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--check", action="store_true", help="validate the config and the data only; no training"
    )
    args = parser.parse_args(argv)
    config = SftConfig.load(args.config)
    counts: dict[str, Any] = {
        name: len(read_rows(config.data / f"sft-{name}.jsonl")) for name in ("train", "validation")
    }
    sys.stdout.write(f"{config.base_model}@{config.revision[:12]}: {counts}\n")
    if args.check:
        return 0
    sys.stdout.write(f"saved to {train(config)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
