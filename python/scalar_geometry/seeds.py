"""Deterministic seed derivation.

One master seed fixes an entire run. Every cell derives its own seed from the
master plus the cell's identity, so cells can run in any order, on any number
of machines, and still reproduce. Nothing in the pipeline ever calls a global
random number generator.
"""

from __future__ import annotations

from hashlib import blake2b

SEED_BITS = 64


def derive_seed(master: int, *parts: str) -> int:
    """Derive a cell seed from the master seed and the cell's identity.

    Order matters and is part of the identity, so pass the parts in a fixed
    sequence such as model, layer, scale, condition.
    """
    if master < 0:
        raise ValueError("master seed must be non-negative")
    h = blake2b(digest_size=SEED_BITS // 8)
    h.update(str(master).encode("utf-8"))
    for part in parts:
        # Length prefix so that ("ab", "c") and ("a", "bc") differ.
        h.update(len(part).to_bytes(4, "big"))
        h.update(part.encode("utf-8"))
    return int.from_bytes(h.digest(), "big")
