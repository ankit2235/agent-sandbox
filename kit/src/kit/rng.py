"""Seeded randomness.

The only module in the kit allowed to touch :mod:`random`, and it may only use
an explicitly seeded instance. Everything else asks a :class:`SeededRNG` for
numbers, choices and ids, so a run is reproducible from its seed alone.
"""

from __future__ import annotations

import hashlib
import random  # purity-allow: seeded instance only, never the module-level API
import uuid
from collections.abc import MutableSequence, Sequence
from typing import TypeVar

T = TypeVar("T")

_MASK64 = (1 << 64) - 1


def derive_seed(seed: int, label: str) -> int:
    """Derive a child seed from a parent seed and a label, order-independently."""
    digest = hashlib.blake2b(f"{seed}:{label}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") & _MASK64


class SeededRNG:
    """A labelled, deterministic random stream.

    Child streams are derived from the label, not from draw order, so one
    service consuming extra numbers cannot change what another service gets.
    """

    def __init__(self, seed: int, label: str = "root") -> None:
        self.seed = seed
        self.label = label
        self._random = random.Random(derive_seed(seed, label))

    def child(self, label: str) -> SeededRNG:
        """A derived stream, e.g. ``rng.child("rails")``."""
        return SeededRNG(self.seed, f"{self.label}/{label}")

    def random(self) -> float:
        """Float in ``[0.0, 1.0)``."""
        return self._random.random()

    def randint(self, low: int, high: int) -> int:
        """Integer in ``[low, high]``."""
        return self._random.randint(low, high)

    def choice(self, items: Sequence[T]) -> T:
        """One element of a non-empty sequence."""
        return self._random.choice(items)

    def sample(self, items: Sequence[T], count: int) -> list[T]:
        """``count`` distinct elements."""
        return self._random.sample(list(items), count)

    def shuffle(self, items: MutableSequence[T]) -> None:
        """Shuffle in place."""
        self._random.shuffle(items)

    def chance(self, probability: float) -> bool:
        """``True`` with the given probability. Used for seeded failure injection."""
        if not 0.0 <= probability <= 1.0:
            msg = f"probability must be in [0, 1], got {probability}"
            raise ValueError(msg)
        return self._random.random() < probability

    def bytes(self, count: int) -> bytes:
        """Deterministic bytes."""
        return self._random.randbytes(count)

    def uuid(self) -> str:
        """A UUID4-shaped identifier that is the same on every run of this seed."""
        return str(uuid.UUID(bytes=self.bytes(16), version=4))

    def token(self, length: int = 12) -> str:
        """A short lowercase id for handles, keys and references."""
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
        return "".join(self._random.choice(alphabet) for _ in range(length))

    def __repr__(self) -> str:
        return f"SeededRNG(seed={self.seed}, label={self.label!r})"
