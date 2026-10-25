"""Local inference for our model with transformers: the :class:`~.backend.Scorer` it runs on.

Needs ``training/src/training/guard_model/requirements-train.txt`` (torch, transformers,
PEFT); imported only when the ``guard_model`` backend is built. The base model is loaded at
a pinned revision, the LoRA adapter from a local folder; nothing is downloaded unless the
base model is not already in the local Hugging Face cache.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


class HFScorer:
    """A causal language model with an optional LoRA adapter."""

    def __init__(
        self,
        base_model: str,
        revision: str,
        *,
        adapter: str | None = None,
        device: str = "cpu",
        dtype: str = "float32",
    ) -> None:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.model_id = base_model if adapter is None else f"{base_model}+{adapter}"
        self.revision: str | None = revision
        self._torch: Any = torch
        self._tokenizer: Any = AutoTokenizer.from_pretrained(base_model, revision=revision)
        model: Any = AutoModelForCausalLM.from_pretrained(
            base_model, revision=revision, dtype=getattr(torch, dtype)
        )
        if adapter is not None:
            from peft import PeftModel

            model = PeftModel.from_pretrained(model, adapter)
        self._model: Any = model.to(device).eval()
        self._device = device

    def logprobs(self, text: str, continuations: Sequence[str]) -> list[float]:
        """Sum of token log-probabilities of each continuation after ``text``."""
        torch = self._torch
        prompt_ids = self._tokenizer(text, return_tensors="pt").input_ids
        start = prompt_ids.shape[1]
        out: list[float] = []
        with torch.no_grad():
            for continuation in continuations:
                ids = self._tokenizer(text + continuation, return_tensors="pt").input_ids
                ids = ids.to(self._device)
                logits = self._model(ids).logits[0, :-1]
                logp = torch.log_softmax(logits.float(), dim=-1)
                targets = ids[0, 1:]
                picked = logp[torch.arange(len(targets)), targets]
                out.append(float(picked[start - 1 :].sum()))
        return out

    def generate(self, text: str, max_new_tokens: int) -> str:
        """A greedy continuation."""
        torch = self._torch
        ids = self._tokenizer(text, return_tensors="pt").input_ids.to(self._device)
        with torch.no_grad():
            output = self._model.generate(ids, max_new_tokens=max_new_tokens, do_sample=False)
        return str(self._tokenizer.decode(output[0, ids.shape[1] :], skip_special_tokens=True))


__all__ = ["HFScorer"]
