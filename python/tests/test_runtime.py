from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scalar_geometry.config import ConfigError, load
from scalar_geometry.runtime import Operations, RunError, run_experiment


class FakeOperations(Operations):
    def __init__(self, *, fail_analysis_once: bool = False) -> None:
        self.calls = {name: 0 for name in ("extract", "measure", "analyse", "report")}
        self.fail_analysis_once = fail_analysis_once

    def extract(self, config, destination):
        self.calls["extract"] += 1
        layer = destination / "layer_00"
        layer.mkdir(parents=True)
        (layer / "shard.json").write_text("{}\n", encoding="utf-8")
        (layer / "embeddings.f32").write_bytes(b"vectors")

    def measure(self, config, shards, measurements, directions, log):
        self.calls["measure"] += 1
        measurements.mkdir(parents=True)
        direction = directions / "layer_00"
        direction.mkdir(parents=True)
        (measurements / "layer_00.json").write_text(
            '{"results": []}\n', encoding="utf-8"
        )
        (direction / "directions.json").write_text("{}\n", encoding="utf-8")
        (direction / "directions.f32").write_bytes(b"directions")

    def analyse(self, config, directions, destination, log):
        self.calls["analyse"] += 1
        if self.fail_analysis_once and self.calls["analyse"] == 1:
            raise RunError("injected analysis failure")
        destination.write_text('{"groups": []}\n', encoding="utf-8")

    def report(self, config, measurements, destination, log):
        self.calls["report"] += 1
        destination.write_text("verified report\n", encoding="utf-8")


class RuntimeContract(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "scales.yaml").write_text("scales: []\n", encoding="utf-8")
        (self.root / "background.txt").write_text("background\n", encoding="utf-8")
        self.config_path = self.root / "experiment.yaml"
        self.config_path.write_text(
            yaml.safe_dump(
                {
                    "format": "semantic-scale-geometry/experiment/1",
                    "scales": "scales.yaml",
                    "background": "background.txt",
                    "model": {
                        "id": "example/model",
                        "revision": "a" * 40,
                        "device": "cpu",
                        "pooling": "mean",
                        "max_length": 48,
                        "batch_size": 16,
                    },
                    "analysis": {
                        "removals": [0, 1, 3],
                        "random_draws": 100,
                        "bootstrap_draws": 50,
                        "seed": 7,
                        "null_replicates": 10,
                        "subspace_condition": "carrier",
                    },
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_complete_run_is_immutable_and_reused(self):
        operations = FakeOperations()
        first = run_experiment(self.config_path, self.root / "runs", operations)
        second = run_experiment(self.config_path, self.root / "runs", operations)

        self.assertFalse(first.reused)
        self.assertTrue(second.reused)
        self.assertEqual(first.run_id, second.run_id)
        self.assertEqual(
            operations.calls,
            {"extract": 1, "measure": 1, "analyse": 1, "report": 1},
        )
        status = json.loads((first.run_dir / "status.json").read_text())
        self.assertEqual(status["state"], "SUCCEEDED")
        self.assertEqual(status["created_at"], status["created_at"].strip())
        self.assertEqual(status["completed_at"], status["updated_utc"])
        self.assertGreaterEqual(status["duration_seconds"], 0)
        self.assertIsNone(status["error_code"])
        events = [
            json.loads(line)
            for line in (first.run_dir / "logs.jsonl").read_text().splitlines()
        ]
        self.assertEqual(events[-1]["event"], "run_reused")

    def test_failed_run_resumes_completed_stages(self):
        operations = FakeOperations(fail_analysis_once=True)
        with self.assertRaisesRegex(RunError, "injected analysis failure"):
            run_experiment(self.config_path, self.root / "runs", operations)

        failed_status = next((self.root / "runs").glob("*/status.json"))
        failed = json.loads(failed_status.read_text())
        self.assertEqual(failed["state"], "FAILED")
        self.assertEqual(failed["error_code"], "RunError")

        result = run_experiment(self.config_path, self.root / "runs", operations)
        self.assertEqual(
            operations.calls,
            {"extract": 1, "measure": 1, "analyse": 2, "report": 1},
        )
        self.assertEqual(
            json.loads((result.run_dir / "status.json").read_text())["state"],
            "SUCCEEDED",
        )

    def test_modified_output_prevents_false_reuse(self):
        result = run_experiment(self.config_path, self.root / "runs", FakeOperations())
        (result.run_dir / "report" / "summary.txt").write_text(
            "modified\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(RunError, "modified immutable outputs"):
            run_experiment(self.config_path, self.root / "runs", FakeOperations())

    def test_run_identity_changes_when_an_input_changes(self):
        first = run_experiment(self.config_path, self.root / "runs", FakeOperations())
        (self.root / "background.txt").write_text("changed\n", encoding="utf-8")
        second = run_experiment(self.config_path, self.root / "runs", FakeOperations())
        self.assertNotEqual(first.run_id, second.run_id)

    def test_uncommitted_stage_output_is_recovered(self):
        operations = FakeOperations()
        config = load(self.config_path)
        from scalar_geometry import manifest

        identity = manifest.run_id(
            config.normalized,
            {
                "scales_sha256": manifest.file_digest(config.scales_path),
                "background_sha256": manifest.file_digest(config.background_path),
            },
        )
        orphan = self.root / "runs" / identity / "shards"
        orphan.mkdir(parents=True)
        (orphan / "partial").write_text("interrupted", encoding="utf-8")

        result = run_experiment(self.config_path, self.root / "runs", operations)
        self.assertEqual(result.run_id, identity)
        self.assertFalse((result.run_dir / "shards" / "partial").exists())
        self.assertEqual(operations.calls["extract"], 1)


class ConfigurationContract(unittest.TestCase):
    def test_model_revision_must_be_an_exact_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scales.yaml").write_text("scales: []\n", encoding="utf-8")
            (root / "background.txt").write_text("background\n", encoding="utf-8")
            config = {
                "format": "semantic-scale-geometry/experiment/1",
                "scales": "scales.yaml",
                "background": "background.txt",
                "model": {"id": "example/model", "revision": "main"},
            }
            path = root / "experiment.yaml"
            path.write_text(yaml.safe_dump(config), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "40-character commit SHA"):
                load(path)
