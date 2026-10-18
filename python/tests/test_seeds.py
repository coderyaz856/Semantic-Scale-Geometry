"""Tests for seed derivation.

The whole reproducibility story rests on this function: one master seed has to
fix an entire sweep, and two different cells must never collide on a seed just
because their identities concatenate to the same string.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scalar_geometry.seeds import SEED_BITS, derive_seed


class SeedDerivation(unittest.TestCase):
    def test_is_deterministic(self) -> None:
        a = derive_seed(7, "model", "3", "temperature", "carrier")
        b = derive_seed(7, "model", "3", "temperature", "carrier")
        self.assertEqual(a, b)

    def test_depends_on_the_master(self) -> None:
        self.assertNotEqual(derive_seed(1, "x"), derive_seed(2, "x"))

    def test_depends_on_part_order(self) -> None:
        self.assertNotEqual(derive_seed(1, "a", "b"), derive_seed(1, "b", "a"))

    def test_length_prefix_prevents_concatenation_collisions(self) -> None:
        # Without a length prefix these two cells would share a seed, which is
        # the classic way a sweep quietly correlates results that should be
        # independent.
        self.assertNotEqual(derive_seed(1, "ab", "c"), derive_seed(1, "a", "bc"))

    def test_empty_parts_are_distinguished(self) -> None:
        self.assertNotEqual(derive_seed(1), derive_seed(1, ""))
        self.assertNotEqual(derive_seed(1, "", "a"), derive_seed(1, "a", ""))

    def test_fits_the_declared_width(self) -> None:
        seed = derive_seed(99, "model", "layer")
        self.assertGreaterEqual(seed, 0)
        self.assertLess(seed, 2**SEED_BITS)

    def test_negative_master_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            derive_seed(-1, "x")

    def test_unicode_parts_are_handled(self) -> None:
        self.assertNotEqual(derive_seed(1, "café"), derive_seed(1, "cafe"))


if __name__ == "__main__":
    unittest.main()
