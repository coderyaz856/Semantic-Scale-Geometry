"""End to end against fakes: order, idempotence, verification, exit codes."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _bootstrap  # noqa: F401  - import path, must come first
import runfixture
from fakes import FakeLedger, FakeStore
from sg_cloud import publish as publish_module
from sg_cloud import runs
from sg_cloud.errors import AdapterError, Exit
from sg_cloud.logs import Log


def quiet_log() -> Log:
    return Log(stream=io.StringIO(), component="test")


class PublishTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.run_dir = runs.load(runfixture.build(self.root))
        self.store = FakeStore()
        self.sink = FakeLedger()

    def publish(self, **kwargs):
        return publish_module.publish(
            self.run_dir, self.store, self.sink, quiet_log(), **kwargs
        )

    def test_artifacts_go_up_before_the_ledger_points_at_them(self) -> None:
        result = self.publish()
        prefix = f"runs/{runfixture.RUN_ID}"
        self.assertEqual(result["artifact_uri"], f"gs://fake-bucket/{prefix}")
        row = self.sink.tables["runs"][(runfixture.RUN_ID,)]
        self.assertEqual(row["artifact_uri"], f"gs://fake-bucket/{prefix}")
        # The row's prefix exists, which is the whole point of the ordering.
        self.assertTrue(any(key.startswith(prefix + "/") for key in self.store.objects))

    def test_all_three_tables_are_loaded(self) -> None:
        result = self.publish()
        self.assertEqual(result["measurement_rows"], 8)
        self.assertEqual(result["subspace_rows"], 1)
        self.assertEqual(len(self.sink.tables["runs"]), 1)
        self.assertEqual(len(self.sink.tables["scale_measurements"]), 8)
        self.assertEqual(len(self.sink.tables["subspace_summaries"]), 1)

    def test_publishing_twice_leaves_exactly_one_set_of_rows(self) -> None:
        self.publish()
        before = {table: dict(rows) for table, rows in self.sink.tables.items()}
        second = self.publish()
        self.assertEqual(second["objects_uploaded"], 0)
        self.assertGreater(second["objects_skipped"], 0)
        self.assertEqual(self.sink.tables, before)

    def test_a_failed_run_is_refused_before_anything_is_written(self) -> None:
        failed = runs.load(
            runfixture.build(self.root, run_id="run-failed", state="FAILED")
        )
        with self.assertRaises(AdapterError) as caught:
            publish_module.publish(failed, self.store, self.sink, quiet_log())
        self.assertEqual(caught.exception.code, Exit.RUN_NOT_SUCCEEDED)
        self.assertEqual(self.store.objects, {})
        self.assertEqual(self.sink.tables, {})

    def test_a_conflicting_object_stops_the_ledger_write(self) -> None:
        self.store.seed_different(f"runs/{runfixture.RUN_ID}/config.yaml", size=99)
        with self.assertRaises(AdapterError) as caught:
            self.publish()
        self.assertEqual(caught.exception.code, Exit.OBJECT_CONFLICT)
        self.assertEqual(self.sink.tables, {})

    def test_duplicate_merge_keys_are_refused(self) -> None:
        # Two documents covering one layer: a real hazard, because the merge
        # would keep whichever row happened to be written last.
        duplicate = self.run_dir.path / "measurements" / "layer00-again.json"
        source = self.run_dir.path / "measurements" / "layer00.json"
        duplicate.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        with self.assertRaises(AdapterError) as caught:
            self.publish()
        self.assertEqual(caught.exception.code, Exit.INVALID_RUN)
        self.assertEqual(self.sink.tables, {})

    def test_a_disagreeing_row_count_fails_verification(self) -> None:
        class Forgetful(FakeLedger):
            def count_rows(self, table, run_id, layers=None):
                return 0

        with self.assertRaises(AdapterError) as caught:
            publish_module.publish(self.run_dir, self.store, Forgetful(), quiet_log())
        self.assertEqual(caught.exception.code, Exit.VERIFICATION_FAILED)

    def test_missing_provenance_is_reported_and_does_not_block(self) -> None:
        manifest = dict(runfixture.MANIFEST)
        del manifest["model_revision"]
        run = runs.load(
            runfixture.build(self.root, run_id="run-thin", manifest=manifest)
        )
        result = publish_module.publish(run, self.store, self.sink, quiet_log())
        self.assertEqual(result["missing_provenance"], ["model_revision"])
        self.assertIsNone(self.sink.tables["runs"][("run-thin",)]["model_revision"])

    def test_upload_without_a_ledger_is_allowed(self) -> None:
        result = publish_module.publish(self.run_dir, self.store, None, quiet_log())
        self.assertEqual(result["loaded"], {})
        self.assertGreater(result["objects_uploaded"], 0)

    def test_dry_run_contacts_nothing_and_still_counts_rows(self) -> None:
        result = publish_module.publish(self.run_dir, None, None, quiet_log())
        self.assertIsNone(result["artifact_uri"])
        self.assertEqual(result["measurement_rows"], 8)
        self.assertEqual(result["objects_uploaded"], 0)


class CommandLineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.path = runfixture.build(self.root)
        # The command logs to the real stderr, which would bury the test
        # output. Captured rather than silenced, so an assertion could read it.
        self.stderr = io.StringIO()
        patch = mock.patch("sys.stderr", self.stderr)
        patch.start()
        self.addCleanup(patch.stop)

    def test_dry_run_prints_one_json_result_and_exits_zero(self) -> None:
        buffer = io.StringIO()
        with mock.patch("sys.stdout", buffer):
            code = publish_module.main([str(self.path), "--dry-run"])
        self.assertEqual(code, int(Exit.OK))
        payload = json.loads(buffer.getvalue())
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["run_id"], runfixture.RUN_ID)
        self.assertEqual(payload["measurement_rows"], 8)

    def test_a_run_root_with_one_run_is_accepted(self) -> None:
        buffer = io.StringIO()
        with mock.patch("sys.stdout", buffer):
            code = publish_module.main([str(self.root), "--dry-run"])
        self.assertEqual(code, int(Exit.OK))
        self.assertEqual(json.loads(buffer.getvalue())["run_id"], runfixture.RUN_ID)

    def test_no_destination_is_a_usage_error(self) -> None:
        # Both given as empty rather than omitted, because the defaults come
        # from the environment and a test must not depend on the shell it runs
        # in.
        code = publish_module.main([str(self.path), "--bucket", "", "--bq-dataset", ""])
        self.assertEqual(code, int(Exit.USAGE))

    def test_a_failed_run_exits_with_its_own_code(self) -> None:
        failed = runfixture.build(self.root, run_id="run-bad", state="FAILED")
        self.assertEqual(
            publish_module.main([str(failed), "--dry-run"]),
            int(Exit.RUN_NOT_SUCCEEDED),
        )

    def test_a_missing_directory_is_an_invalid_run(self) -> None:
        self.assertEqual(
            publish_module.main([str(self.root / "absent"), "--dry-run"]),
            int(Exit.INVALID_RUN),
        )


if __name__ == "__main__":
    unittest.main()
