"""Contracts between the runtime, the report and the cloud ledger.

Each side has its own tests. These check the seams: that the ledger reads the
provenance the runtime writes, and that the ledger's significance decision is
the report's, on the results the repository actually reports.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY / "cloud"))
sys.path.insert(0, str(REPOSITORY / "python"))
sys.path.insert(0, str(REPOSITORY / "tools"))

import report  # noqa: E402
from scalar_geometry import manifest  # noqa: E402
from sg_cloud import records, runs  # noqa: E402

SAVED_LAYERS = sorted((REPOSITORY / "results").glob("minilm_layer*.json"))


class LedgerAgreesWithTheReport(unittest.TestCase):
    def test_saved_results_exist(self) -> None:
        self.assertTrue(SAVED_LAYERS)

    def test_significance_matches_the_report_on_every_saved_layer(self) -> None:
        # The ledger's `significant` column is only trustworthy if it is the
        # same decision the published tables were built from. Compared per
        # layer and per preprocessing setting over the contextual family,
        # which is the one the report corrects over.
        run = SimpleNamespace(run_id="contract")
        for path in SAVED_LAYERS:
            document = json.loads(path.read_text(encoding="utf-8"))
            rows = records.measurement_rows(run, [(path.name, document)])
            reported = report.load([str(path)])
            layer = document["layer"]
            for removal in sorted({r["remove_top"] for r in reported}):
                with self.subTest(layer=layer, remove_top=removal):
                    expected = report.bh_survivors(
                        report.select(reported, layer, removal)
                    )
                    decided = {
                        r["scale_id"]
                        for r in rows
                        if r["significant"]
                        and r["remove_top"] == removal
                        and records.condition_group(r["condition"]) == "contextual"
                    }
                    self.assertEqual(decided, expected)


class RuntimeCloudContract(unittest.TestCase):
    def test_runtime_manifest_maps_to_complete_cloud_provenance(self) -> None:
        run_id = "0123456789abcdef0123"
        model_id = "sentence-transformers/all-MiniLM-L6-v2"
        revision = "a" * 40
        scales_digest = "b" * 64
        background_digest = "c" * 64
        config = {
            "format": "semantic-scale-geometry/experiment/1",
            "scales": "/app/data/scales/scales.yaml",
            "background": "/app/data/background/wikitext2.txt",
            "model": {
                "id": model_id,
                "revision": revision,
                "device": "cpu",
                "pooling": "mean",
                "max_length": 48,
                "batch_size": 16,
            },
            "analysis": {},
            "execution": {},
        }
        document = manifest.build(
            config,
            identity=run_id,
            inputs={
                "scales_sha256": scales_digest,
                "background_sha256": background_digest,
            },
        )

        with tempfile.TemporaryDirectory() as temporary:
            run_directory = Path(temporary) / run_id
            run_directory.mkdir()
            (run_directory / "config.yaml").write_text(
                "format: test\n", encoding="utf-8"
            )
            manifest.write(document, run_directory / "manifest.json")
            (run_directory / "status.json").write_text(
                json.dumps(
                    {
                        "format": "semantic-scale-geometry/run-status/1",
                        "run_id": run_id,
                        "state": "SUCCEEDED",
                        "created_at": "2026-10-19T00:00:00Z",
                        "completed_at": "2026-10-19T00:01:00Z",
                        "duration_seconds": 60.0,
                        "error_code": None,
                        "updated_utc": "2026-10-19T00:01:00Z",
                    }
                ),
                encoding="utf-8",
            )

            row = records.run_row(runs.load(run_directory), "gs://bucket/runs/id")

        self.assertEqual(row["model_id"], model_id)
        self.assertEqual(row["model_revision"], revision)
        self.assertEqual(row["scales_digest"], scales_digest)
        self.assertEqual(row["background_digest"], background_digest)
        self.assertEqual(row["device"], "cpu")


if __name__ == "__main__":
    unittest.main()
