"""The plain files the training scripts read, written from a built dataset.

``sft-train.jsonl`` and ``sft-validation.jsonl`` hold ``{prompt, completion}`` rows;
``grpo-train.jsonl`` holds ``{prompt, decision, reason_codes}``. They are made here,
where the sandbox is installed, so the GPU machine needs only the training requirements.
The test split and the hidden set are never written here.

A prompt longer than ``max_prompt_chars`` is skipped and counted, so a long row is never
truncated into a completion the model cannot see.
"""

from __future__ import annotations

import json
from pathlib import Path

from guard.actions import ActionsRegistry, actions
from guard.backends.guard_model.format import completion, prompt, reasons_for

from ..data.build import read_split

MAX_PROMPT_CHARS = 12000
"""About 3,400 to 4,000 tokens for these JSON-heavy prompts (estimated at 3 to 3.5 characters a
token, not measured with the tokenizer), so prompt and completion fit the configs'
``max_length`` of 4,096. The full build's train prompts have a median of 6,158 characters and
a 90th percentile of 10,331 (measured)."""


def export(
    dataset: Path,
    out: Path,
    *,
    max_prompt_chars: int = MAX_PROMPT_CHARS,
    registry: ActionsRegistry = actions,
) -> dict[str, dict[str, int]]:
    """Write the training files; return rows written and skipped per file."""
    out.mkdir(parents=True, exist_ok=True)
    counts: dict[str, dict[str, int]] = {}
    for split, files in (
        ("train", ("sft-train", "grpo-train")),
        ("validation", ("sft-validation",)),
    ):
        rows = read_split(dataset, split)
        handles = {name: (out / f"{name}.jsonl").open("w", encoding="utf-8") for name in files}
        written = skipped = 0
        try:
            for row in rows:
                text = prompt(row.request, reasons_for(registry.get(row.world)))
                if len(text) > max_prompt_chars:
                    skipped += 1
                    continue
                written += 1
                answer = completion(row.label.decision, row.label.reason_codes)
                handles[files[0]].write(
                    json.dumps({"prompt": text, "completion": answer}, ensure_ascii=False) + "\n"
                )
                if len(files) > 1:
                    handles[files[1]].write(
                        json.dumps(
                            {
                                "prompt": text,
                                "decision": row.label.decision,
                                "reason_codes": json.dumps(row.label.reason_codes),
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
        finally:
            for handle in handles.values():
                handle.close()
        counts[split] = {"written": written, "skipped_too_long": skipped}
    return counts


__all__ = ["MAX_PROMPT_CHARS", "export"]
