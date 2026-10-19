"""Reading a run directory: what is accepted, what is refused, what is null."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  - import path, must come first
import runfixture
from sg_cloud import runs
from sg_cloud.errors import Exit


class TempRun(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.addCleanup(self._temp.cleanup)


class LoadTests(TempRun):
    def test_reads_a_complete_run(self) -> None:
        path = runfixture.build(self.root)
        run = runs.load(path)
        self.assertEqual(run.run_id, runfixture.RUN_ID)
        self.assertEqual(run.state, "SUCCEEDED")
        self.assertTrue(run.succeeded)
        self.assertEqual(run.manifest_field("device"), "cpu")

    def test_refuses_a_directory_missing_contract_files(self) -> None:
        path = runfixture.build(self.root)
        (path / "manifest.json").unlink()
        with self.assertRaises(runs.AdapterError) as caught:
            runs.load(path)
        self.assertEqual(caught.exception.code, Exit.INVALID_RUN)

    def test_refuses_a_run_with_no_state(self) -> None:
        path = runfixture.build(self.root)
        (path / "status.json").write_text(json.dumps({"run_id": "x"}), encoding="utf-8")
        with self.assertRaises(runs.AdapterError):
            runs.load(path)

    def test_refuses_a_run_id_that_would_reshape_the_bucket(self) -> None:
        path = runfixture.build(self.root)
        status = json.loads((path / "status.json").read_text(encoding="utf-8"))
        status["run_id"] = "../escape"
        (path / "status.json").write_text(json.dumps(status), encoding="utf-8")
        with self.assertRaises(runs.AdapterError) as caught:
            runs.load(path)
        self.assertEqual(caught.exception.code, Exit.INVALID_RUN)

    def test_a_failed_run_is_read_but_not_publishable(self) -> None:
        path = runfixture.build(self.root, state="FAILED")
        run = runs.load(path)
        self.assertFalse(run.succeeded)
        with self.assertRaises(runs.AdapterError) as caught:
            runs.require_succeeded(run)
        self.assertEqual(caught.exception.code, Exit.RUN_NOT_SUCCEEDED)


class ProvenanceTests(TempRun):
    def test_absent_fields_are_reported_not_invented(self) -> None:
        manifest = json.loads(json.dumps(runfixture.MANIFEST))
        del manifest["config"]["model"]["revision"]
        del manifest["inputs"]["background_sha256"]
        run = runs.load(runfixture.build(self.root, manifest=manifest))
        self.assertIsNone(run.manifest_field("model_revision"))
        self.assertEqual(
            sorted(run.missing_provenance()),
            ["background_digest", "model_revision"],
        )

    def test_nested_spellings_are_accepted(self) -> None:
        # The runtime branch may nest these. Both spellings are declared in
        # MANIFEST_FIELDS, and this is the test that keeps that promise.
        manifest = {
            "model": {"id": "m", "revision": "r"},
            "inputs": {"scales_digest": "s"},
        }
        run = runs.load(runfixture.build(self.root, manifest=manifest))
        self.assertEqual(run.manifest_field("model_id"), "m")
        self.assertEqual(run.manifest_field("model_revision"), "r")
        self.assertEqual(run.manifest_field("scales_digest"), "s")


class TimestampTests(unittest.TestCase):
    def test_offsets_are_normalized_to_utc(self) -> None:
        self.assertEqual(
            runs.normalize_timestamp("2026-10-18T11:00:00+02:00"),
            "2026-10-18T09:00:00Z",
        )

    def test_naive_timestamps_are_read_as_utc(self) -> None:
        self.assertEqual(
            runs.normalize_timestamp("2026-10-18T09:00:00"),
            "2026-10-18T09:00:00Z",
        )

    def test_none_and_empty_are_null(self) -> None:
        self.assertIsNone(runs.normalize_timestamp(None))
        self.assertIsNone(runs.normalize_timestamp("  "))

    def test_unparseable_is_an_error_not_a_guess(self) -> None:
        with self.assertRaises(runs.AdapterError):
            runs.normalize_timestamp("last tuesday")


class ResultDiscoveryTests(TempRun):
    def test_documents_are_classified_by_shape(self) -> None:
        run = runs.load(runfixture.build(self.root))
        measurements = run.measurement_documents()
        subspaces = run.subspace_documents()
        self.assertEqual(len(measurements), 2)
        self.assertEqual(len(subspaces), 1)
        # The report directory holds both kinds; neither is claimed by the
        # other, which is the property a filename convention would not give.
        self.assertTrue(all("measurements/" in name for name, _ in measurements))
        self.assertTrue(all("report/" in name for name, _ in subspaces))

    def test_unrelated_json_is_ignored(self) -> None:
        path = runfixture.build(self.root)
        (path / "report" / "coverage.json").write_text(
            '{"note": "no results"}', encoding="utf-8"
        )
        run = runs.load(path)
        self.assertEqual(len(run.measurement_documents()), 2)
        self.assertEqual(len(run.subspace_documents()), 1)

    def test_malformed_result_json_is_an_error(self) -> None:
        path = runfixture.build(self.root)
        (path / "measurements" / "broken.json").write_text(
            "{not json", encoding="utf-8"
        )
        run = runs.load(path)
        with self.assertRaises(runs.AdapterError):
            run.measurement_documents()


class LocateTests(TempRun):
    def test_finds_the_single_run_under_a_root(self) -> None:
        path = runfixture.build(self.root)
        self.assertEqual(runs.resolve(self.root), path)
        self.assertEqual(runs.resolve(path), path)

    def test_refuses_to_guess_between_two_runs(self) -> None:
        runfixture.build(self.root, run_id="run-a")
        runfixture.build(self.root, run_id="run-b")
        with self.assertRaises(runs.AdapterError) as caught:
            runs.resolve(self.root)
        self.assertEqual(caught.exception.code, Exit.INVALID_RUN)

    def test_empty_root_is_an_error(self) -> None:
        with self.assertRaises(runs.AdapterError):
            runs.resolve(self.root)


if __name__ == "__main__":
    unittest.main()
