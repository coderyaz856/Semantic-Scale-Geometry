"""Tests for scale loading, validation and expansion.

Written against ``unittest`` so they run with the standard library alone, and
``pytest`` collects them unchanged.

The row contract matters more than it looks. The shard's binary payload is row
major in the order ``enumerate_rows`` returns, so if that order ever becomes
dependent on dict iteration, on the filesystem, or on anything else outside the
YAML, every downstream number silently refers to the wrong text.
"""

from __future__ import annotations

import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scalar_geometry.scales import (
    BARE,
    CARRIER,
    UTTERANCE,
    capitalize_first,
    contexts_of,
    enumerate_rows,
    load_scales,
    render,
    summarize,
)

REPO = Path(__file__).resolve().parents[2]
SCALES = REPO / "data" / "scales" / "scales.yaml"

MINIMAL = """\
version: 0.3
language: en
frames:
  adjective:
    - "The {subj} is {item}."
    - "I found the {subj} {item}."
scales:
  - id: demo
    family: demo
    polarity: unipolar
    unit: lexical
    slot: adjective
    gold_confidence: high
    subjects: [coffee, water]
    items:
      - { text: cold, rank: 1 }
      - { text: cool, rank: 2 }
      - { text: warm, rank: 3 }
      - { text: hot, rank: 4 }
"""


def write(tmp: str, body: str) -> Path:
    path = Path(tmp) / "scales.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


class LoadingRejectsBadDefinitions(unittest.TestCase):
    def assert_rejected(self, body: str, fragment: str) -> None:
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as caught:
                load_scales(write(tmp, body))
            self.assertIn(fragment, str(caught.exception))

    def test_ranks_with_a_gap(self) -> None:
        self.assert_rejected(
            MINIMAL.replace("rank: 4 }", "rank: 5 }"), "ranks must be 1..k"
        )

    def test_ranks_with_a_tie(self) -> None:
        self.assert_rejected(
            MINIMAL.replace("{ text: hot, rank: 4 }", "{ text: hot, rank: 3 }"),
            "ranks must be 1..k",
        )

    def test_too_few_points(self) -> None:
        self.assert_rejected(
            MINIMAL.replace("      - { text: hot, rank: 4 }\n", ""),
            "at least four points",
        )

    def test_frame_without_both_slots(self) -> None:
        self.assert_rejected(
            MINIMAL.replace('"I found the {subj} {item}."', '"I found it {item}."'),
            "needs {subj} and {item}",
        )

    def test_lexical_scale_without_a_slot(self) -> None:
        self.assert_rejected(
            MINIMAL.replace("    slot: adjective\n", ""), "need a 'slot'"
        )

    def test_lexical_scale_without_subjects(self) -> None:
        self.assert_rejected(
            MINIMAL.replace("    subjects: [coffee, water]\n", ""),
            "at least one subject",
        )

    def test_unknown_slot(self) -> None:
        self.assert_rejected(
            MINIMAL.replace("slot: adjective", "slot: nope"), "unknown slot"
        )

    def test_duplicate_subjects(self) -> None:
        self.assert_rejected(
            MINIMAL.replace("[coffee, water]", "[coffee, coffee]"), "duplicate subjects"
        )

    def test_utterance_item_without_an_object_slot(self) -> None:
        body = MINIMAL.replace("unit: lexical", "unit: utterance")
        body = body.replace("    slot: adjective\n", "")
        body = body.replace("subjects: [coffee, water]", "objects: [report, file]")
        body = body.replace("text: cold", 'text: "Give it."')
        self.assert_rejected(body, "need '{obj}'")

    def test_non_english_is_refused(self) -> None:
        self.assert_rejected(
            MINIMAL.replace("language: en", "language: fr"), "English only"
        )


class Expansion(unittest.TestCase):
    def test_context_count_is_frames_times_subjects(self) -> None:
        with TemporaryDirectory() as tmp:
            scales = load_scales(write(tmp, MINIMAL))
        self.assertEqual(len(contexts_of(scales[0])), 2 * 2)
        self.assertEqual(scales[0].n_contexts, 4)

    def test_rows_cover_bare_and_every_context(self) -> None:
        with TemporaryDirectory() as tmp:
            scales = load_scales(write(tmp, MINIMAL))
        rows = enumerate_rows(scales)
        # 4 items bare, plus 4 items in each of 4 contexts.
        self.assertEqual(len(rows), 4 + 4 * 4)
        self.assertEqual(sum(1 for r in rows if r.condition == BARE), 4)
        self.assertEqual(sum(1 for r in rows if r.condition == CARRIER), 16)

    def test_rendering_substitutes_and_capitalises(self) -> None:
        with TemporaryDirectory() as tmp:
            scales = load_scales(write(tmp, MINIMAL))
        scale = scales[0]
        item = next(i for i in scale.items if i.rank == 4)
        self.assertEqual(render(scale, item, BARE, "bare"), "hot")
        self.assertEqual(
            render(scale, item, CARRIER, "f0:coffee"), "The coffee is hot."
        )
        self.assertEqual(
            render(scale, item, CARRIER, "f1:water"), "I found the water hot."
        )

    def test_capitalize_first_leaves_the_rest_alone(self) -> None:
        # str.capitalize would lower-case the tail and quietly rewrite items.
        self.assertEqual(
            capitalize_first("gonna need that PDF."), "Gonna need that PDF."
        )
        self.assertEqual(capitalize_first(""), "")


class TheRealScaleFile(unittest.TestCase):
    """The shipped definitions, checked as data rather than as code."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.scales = load_scales(SCALES)
        cls.rows = enumerate_rows(cls.scales)

    def test_row_identity_is_unique(self) -> None:
        keys = [(r.scale_id, r.condition, r.context, r.rank) for r in self.rows]
        self.assertEqual(len(keys), len(set(keys)))

    def test_row_order_is_stable_across_loads(self) -> None:
        again = enumerate_rows(load_scales(SCALES))
        self.assertEqual(
            [(r.scale_id, r.condition, r.context, r.rank) for r in self.rows],
            [(r.scale_id, r.condition, r.context, r.rank) for r in again],
        )

    def test_every_row_is_fully_substituted(self) -> None:
        for row in self.rows:
            self.assertTrue(row.text)
            self.assertNotIn("{", row.text, f"unsubstituted slot in {row.text!r}")

    def test_every_row_starts_with_a_capital(self) -> None:
        for row in self.rows:
            if row.condition == BARE:
                continue
            self.assertTrue(row.text[0].isupper(), f"not capitalised: {row.text!r}")

    def test_no_frame_uses_a_degree_modifier(self) -> None:
        """Degree modifiers resist the ends of a scale.

        "fairly scalding" and "quite colossal" read as errors while the
        mid-scale items read fine, so such a frame would degrade exactly the
        endpoints that carry the most gradient information.
        """
        banned = (" fairly ", " quite ", " rather ", " somewhat ", " slightly ")
        for scale in self.scales:
            for frame in scale.frames:
                for word in banned:
                    self.assertNotIn(word, f" {frame} ", f"{scale.id}: {frame!r}")

    def test_utterance_scales_have_no_bare_condition(self) -> None:
        for row in self.rows:
            scale = next(s for s in self.scales if s.id == row.scale_id)
            if scale.is_utterance:
                self.assertEqual(row.condition, UTTERANCE)

    def test_summary_counts_agree_with_the_rows(self) -> None:
        summary = summarize(self.scales)
        self.assertEqual(summary["rows"], len(self.rows))
        self.assertEqual(summary["items"], sum(len(s.items) for s in self.scales))
        self.assertEqual(
            summary["lexical_scales"] + summary["utterance_scales"], summary["scales"]
        )


if __name__ == "__main__":
    unittest.main()
