"""Near-duplicate detection between rows, with the standard library only.

Two rows are near-duplicates when the Jaccard similarity of their word ``n``-gram sets
(shingles of the rendered request) is at least a threshold. Comparing every pair is
too slow for tens of thousands of rows, so each row gets a MinHash signature by one
permutation hashing (one 64-bit hash per shingle, binned; an empty bin borrows the next
non-empty one), the signatures are split into bands for locality-sensitive hashing, and
only rows that share a band are compared, on their signatures. The estimate's standard
error at a similarity of 0.8 is about 0.035 with 128 bins.
"""

from __future__ import annotations

import hashlib
import re
from array import array
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

EMPTY = (1 << 64) - 1
_WORD = re.compile(r"\w+")


class NearDuplicateConfig(BaseModel):
    """How near is too near."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    threshold: float = Field(default=0.8, gt=0.0, le=1.0)
    shingle: int = Field(default=5, ge=1)
    """Words per shingle."""
    bins: int = Field(default=128, ge=8)
    rows: int = Field(default=4, ge=1)
    """Signature bins per band; ``bins`` must be a multiple of it."""


def shingles(text: str, size: int) -> set[int]:
    """The 64-bit hashes of every run of ``size`` words in ``text`` (lowercased)."""
    words = _WORD.findall(text.lower())
    if len(words) < size:
        words = [*words, *[""] * (size - len(words))]
    return {
        int.from_bytes(
            hashlib.blake2b(" ".join(words[i : i + size]).encode(), digest_size=8).digest(), "big"
        )
        for i in range(len(words) - size + 1)
    }


def signature(hashes: Iterable[int], bins: int) -> array:
    """A one-permutation MinHash signature with rotation densification."""
    minima = [EMPTY] * bins
    for value in hashes:
        slot, rest = value % bins, value // bins
        if rest < minima[slot]:
            minima[slot] = rest
    filled = [i for i, value in enumerate(minima) if value != EMPTY]
    if not filled:
        return array("Q", minima)
    out = list(minima)
    for i in range(bins):
        if out[i] == EMPTY:
            j = next((k for k in filled if k > i), filled[0])
            distance = (j - i) % bins
            out[i] = (minima[j] * 1_000_003 + distance) & (EMPTY - 1)
    return array("Q", out)


def similarity(a: array, b: array) -> float:
    """The estimated Jaccard similarity of two signatures."""
    return sum(x == y for x, y in zip(a, b, strict=True)) / len(a)


@dataclass(frozen=True, slots=True)
class Pair:
    """Two near-duplicate rows, by index into the signature list."""

    left: int
    right: int
    similarity: float


def signatures(texts: Iterable[str], config: NearDuplicateConfig) -> list[array]:
    """Every text's signature."""
    if config.bins % config.rows:
        msg = f"bins ({config.bins}) must be a multiple of rows ({config.rows})"
        raise ValueError(msg)
    return [signature(shingles(text, config.shingle), config.bins) for text in texts]


def near_pairs(
    sigs: Sequence[array],
    groups: Sequence[str],
    config: NearDuplicateConfig,
    *,
    skip: Callable[[str, str], bool] | None = None,
    queries: Sequence[int] | None = None,
) -> list[Pair]:
    """Pairs of rows in different groups whose similarity reaches the threshold.

    ``groups[i]`` is row ``i``'s group (a family, a split); rows of one group are never
    compared. ``skip(a, b)`` may rule out more pairs of groups (already merged ones).
    With ``queries``, only pairs with at least one of those rows are returned.
    """
    wanted = None if queries is None else set(queries)
    buckets: dict[tuple[int, bytes], list[int]] = defaultdict(list)
    width = config.rows
    for index, sig in enumerate(sigs):
        for band in range(len(sig) // width):
            buckets[(band, sig[band * width : (band + 1) * width].tobytes())].append(index)
    seen: set[tuple[int, int]] = set()
    found: list[Pair] = []
    for members in buckets.values():
        if len(members) < 2 or len({groups[m] for m in members}) < 2:
            continue
        for x, left in enumerate(members):
            for right in members[x + 1 :]:
                a, b = groups[left], groups[right]
                if a == b or (skip is not None and skip(a, b)):
                    continue
                if wanted is not None and left not in wanted and right not in wanted:
                    continue
                key = (left, right)
                if key in seen:
                    continue
                seen.add(key)
                score = similarity(sigs[left], sigs[right])
                if score >= config.threshold:
                    found.append(Pair(left, right, score))
    return found


__all__ = [
    "NearDuplicateConfig",
    "Pair",
    "near_pairs",
    "shingles",
    "signature",
    "signatures",
    "similarity",
]
