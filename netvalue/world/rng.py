"""Keyed random streams.

Every draw in the world comes from a stream keyed by *what it is about* rather than by
call order. That property is the foundation of the Phase 4 comparison, so it is worth
stating plainly:

    Two policies that make different choices must still face the *same world*.

If the world drew from one sequential generator, a policy that retried twice instead of
once would shift every subsequent draw, and the two policies would be measured against
different realised worlds. The measured difference would then contain a large component of
pure noise, and paired comparison under common random numbers would be impossible.

Keying each draw by ``(transaction_id, attempt_index, purpose)`` makes the world's latent
randomness a pure function of the key. Whether a given retry on a given transaction at a
given attempt index succeeds is fixed before any policy runs, so policies differ only in
*which* draws they consume.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any, TypeVar

import numpy as np

T = TypeVar("T")


def derive_seed(seed: int, *keys: Any) -> int:
    """A stable 64-bit seed from a base seed and an arbitrary key tuple.

    Uses blake2b over the repr rather than :func:`hash`, because Python's string hashing is
    randomised per process and would silently break reproducibility across runs.
    """
    payload = repr((seed, keys)).encode("utf-8")
    digest = hashlib.blake2b(payload, digest_size=8).digest()
    return int.from_bytes(digest, "big")


def stream(seed: int, *keys: Any) -> np.random.Generator:
    """An independent generator for one purpose, reproducible across processes."""
    return np.random.default_rng(derive_seed(seed, *keys))


def bernoulli(seed: int, p: float, *keys: Any) -> bool:
    """One keyed coin flip.

    The draw is fixed by the key, so asking "would this retry have succeeded?" returns the
    same answer no matter which policy asks, or whether any policy asks at all.
    """
    if not 0.0 <= p <= 1.0:
        raise ValueError(f"probability out of range: {p}")
    return bool(stream(seed, *keys).random() < p)


def uniform(seed: int, low: float, high: float, *keys: Any) -> float:
    return float(stream(seed, *keys).uniform(low, high))


def choice(seed: int, options: Sequence[T], weights: Sequence[float], *keys: Any) -> T:
    """Weighted categorical draw, keyed."""
    if len(options) != len(weights):
        raise ValueError("options and weights must be the same length")
    total = float(sum(weights))
    if total <= 0.0:
        raise ValueError("weights must sum to a positive value")
    probs = [w / total for w in weights]
    idx = int(stream(seed, *keys).choice(len(options), p=probs))
    return options[idx]


def exponential_hours(seed: int, median_hours: float, *keys: Any) -> float:
    """Exponential delay parameterised by its median, which is how the calibrated
    response-time figures are quoted."""
    if median_hours <= 0.0:
        raise ValueError("median must be positive")
    scale = median_hours / np.log(2.0)
    return float(stream(seed, *keys).exponential(scale))
