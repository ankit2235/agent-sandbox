"""Our open guard model: its prompt format, backend, reward, and training scripts.

:mod:`.format`, :mod:`.backend`, :mod:`.reward` and the config models need only this
package and are tested in CI. :mod:`.hf` (inference with transformers), :mod:`.sft`
and :mod:`.grpo` (training with TRL) import torch, transformers, PEFT and TRL lazily;
those come from ``requirements-train.txt`` and are never installed by CI or by the
workspace (decision 187).
"""
