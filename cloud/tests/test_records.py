"""Rows: renames that must not drift, and the one decision made here."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  - import path, must come first
import runfixture
from sg_cloud import records, runs


class RowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.run_dir = runs.load(runfixture.build(self.root))

    def test_run_row_carries_provenance_and_the_prefix(self) -> None:
        row = records.run_row(self.run_dir, "gs://bucket/runs/r-0123456789ab")
        self.assertEqual(row["run_id"], runfixture.RUN_ID)
        self.assertEqual(row["state"], "SUCCEEDED")
        self.assertEqual(row["created_at"], "2026-10-18T09:00:00Z")
        self.assertEqual(row["completed_at"], "2026-10-18T09:04:12Z")
        self.assertEqual(row["model_revision"], runfixture.MODEL_REVISION)
        self.assertEqual(row["artifact_uri"], "gs://bucket/runs/r-0123456789ab")
        self.assertIsNone(row["error_code"])

    def test_measurement_columns_map_to_the_real_field_names(self) -> None:
        rows = records.measurement_rows(
            self.run_dir, self.run_dir.measurement_documents()
        )
        self.assertEqual(len(rows), 8)  # two layers, four scales
        row = next(r for r in rows if r["scale_id"] == "loudness" and r["layer"] == 0)
        self.assertEqual(row["in_sample_order"], 1.0)
        self.assertEqual(row["leave_one_out_order"], 0.886)
        self.assertEqual(row["supervised_variance_share"], 0.34)
        self.assertEqual(row["unsupervised_pc1_share"], 0.35)
        self.assertEqual(row["permutation_p"], 0.0002)
        self.assertEqual(row["context_stability"], 0.09)
        self.assertEqual(row["model_id"], runfixture.MODEL_ID)

    def test_subspace_gap_is_computed_from_its_own_operands(self) -> None:
        rows = records.subspace_rows(self.run_dir, self.run_dir.subspace_documents())
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["n_directions"], 19)
        self.assertAlmostEqual(row["effective_dimensions"], 16.04)
        self.assertAlmostEqual(row["matched_null_dimensions"], 17.16)
        self.assertAlmostEqual(row["dimension_gap"], 17.16 - 16.04, places=9)
        self.assertEqual(row["dimensions_99"], 19)
        self.assertAlmostEqual(row["half_split_cosine"], 0.9999)
        self.assertAlmostEqual(row["permutation_p"], 0.0196)

    def test_counts_stay_integers_and_statistics_stay_floats(self) -> None:
        row = records.subspace_rows(self.run_dir, self.run_dir.subspace_documents())[0]
        for column in (
            "n_directions",
            "dimensions_90",
            "dimensions_99",
            "layer",
            "remove_top",
        ):
            self.assertIsInstance(row[column], int, column)
        for column in ("effective_dimensions", "mean_absolute_cosine", "permutation_p"):
            self.assertIsInstance(row[column], float, column)

    def test_merge_keys_are_unique_in_a_well_formed_run(self) -> None:
        measurements = records.measurement_rows(
            self.run_dir, self.run_dir.measurement_documents()
        )
        subspaces = records.subspace_rows(
            self.run_dir, self.run_dir.subspace_documents()
        )
        self.assertEqual(
            records.duplicate_keys(measurements, records.MEASUREMENT_KEY), []
        )
        self.assertEqual(records.duplicate_keys(subspaces, records.SUBSPACE_KEY), [])

    def test_duplicate_keys_are_detected_not_deduplicated(self) -> None:
        rows = records.measurement_rows(
            self.run_dir, self.run_dir.measurement_documents()
        )
        repeated = records.duplicate_keys(rows + rows[:1], records.MEASUREMENT_KEY)
        self.assertEqual(len(repeated), 1)


class SignificanceTests(unittest.TestCase):
    def test_cutoff_matches_the_textbook_procedure(self) -> None:
        # Four tests at 0.05: thresholds 0.0125, 0.025, 0.0375, 0.05.
        self.assertEqual(
            records.benjamini_hochberg([0.0002, 0.0014, 0.039, 0.47], 0.05),
            2,
        )

    def test_a_larger_p_can_carry_smaller_ones_through(self) -> None:
        # The step-up property: rank 3 passes, so ranks 1 and 2 survive with
        # it even though rank 2 alone would not have met its own threshold.
        self.assertEqual(records.benjamini_hochberg([0.01, 0.02, 0.03], 0.05), 3)

    def test_none_survives_when_nothing_is_small(self) -> None:
        self.assertEqual(records.benjamini_hochberg([0.2, 0.3, 0.9], 0.05), 0)

    def test_empty_family_decides_nothing(self) -> None:
        self.assertEqual(records.benjamini_hochberg([], 0.05), 0)

    def test_decision_is_per_family_and_follows_the_repository_definition(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        run = runs.load(runfixture.build(Path(temp.name)))
        rows = records.measurement_rows(run, run.measurement_documents(), alpha=0.05)

        for layer in (0, 3):
            family = {
                r["scale_id"]: r["significant"] for r in rows if r["layer"] == layer
            }
            self.assertEqual(
                {scale for scale, flag in family.items() if flag},
                {"loudness", "size"},
                f"layer {layer}",
            )

    def test_carrier_and_utterance_scales_form_one_family(self) -> None:
        # The layer-3 shape of the first real run: three utterance scales whose
        # p-values all pass BH among themselves (m = 3) and none of which pass
        # with the carrier scales they are reported alongside (m = 22). The
        # second answer is the report's, so it must be the ledger's too.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        run = runs.load(runfixture.build(Path(temp.name)))

        def result(scale: str, condition: str, p: float) -> dict:
            return {
                "scale_id": scale,
                "condition": condition,
                "remove_top": 0,
                "p_permutation": p,
            }

        carrier = [result(f"lexical{i}", "carrier", 0.5) for i in range(19)]
        utterance = [
            result("politeness", "utterance", 0.0139),
            result("formality", "utterance", 0.0167),
            result("hedging", "utterance", 0.0167),
        ]
        document = {"model": "m", "layer": 3, "results": carrier + utterance}
        rows = records.measurement_rows(run, [("layer03.json", document)])

        self.assertEqual(records.benjamini_hochberg([0.0139, 0.0167, 0.0167], 0.05), 3)
        self.assertFalse(any(r["significant"] for r in rows))

    def test_bare_items_are_corrected_as_their_own_family(self) -> None:
        # 0.002 passes rank 1 among 19 bare scales (threshold 0.0026) and would
        # not among 41 if bare were pooled with the contextual group (0.0012).
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        run = runs.load(runfixture.build(Path(temp.name)))
        results = [
            {
                "scale_id": f"s{i}",
                "condition": condition,
                "remove_top": 0,
                "p_permutation": 0.002 if (condition, i) == ("bare", 0) else 0.5,
            }
            for condition in ("bare", "carrier")
            for i in range(19)
        ] + [
            {
                "scale_id": f"u{i}",
                "condition": "utterance",
                "remove_top": 0,
                "p_permutation": 0.5,
            }
            for i in range(3)
        ]
        document = {"model": "m", "layer": 3, "results": results}
        rows = records.measurement_rows(run, [("layer03.json", document)])

        significant = {
            (r["condition"], r["scale_id"]) for r in rows if r["significant"]
        }
        self.assertEqual(significant, {("bare", "s0")})

    def test_an_explicit_decision_from_the_runtime_wins(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        run = runs.load(runfixture.build(Path(temp.name)))
        documents = run.measurement_documents()
        for _name, document in documents:
            for result in document["results"]:
                result["significant"] = True
        rows = records.measurement_rows(run, documents)
        self.assertTrue(all(row["significant"] for row in rows))

    def test_a_missing_p_value_leaves_the_decision_null(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        run = runs.load(runfixture.build(Path(temp.name)))
        documents = run.measurement_documents()
        documents[0][1]["results"][0].pop("p_permutation")
        rows = records.measurement_rows(run, documents)
        undecided = [r for r in rows if r["permutation_p"] is None]
        self.assertEqual(len(undecided), 1)
        self.assertIsNone(undecided[0]["significant"])


if __name__ == "__main__":
    unittest.main()
