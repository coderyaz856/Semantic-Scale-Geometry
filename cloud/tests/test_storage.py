"""Uploading: idempotent by content, and refusing to overwrite."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  - import path, must come first
import runfixture
from fakes import FakeStore
from sg_cloud import runs, storage
from sg_cloud.errors import AdapterError, Exit
from sg_cloud.logs import Log


class _Quiet(Log):
    """A logger that keeps the test output readable and the lines inspectable."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str, dict]] = []
        super().__init__(stream=None)

    def _emit(self, severity: str, message: str, fields: dict) -> None:
        self.lines.append((severity, message, fields))

    def bind(self, **fields: object) -> _Quiet:
        return self


class UploadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.run_dir = runs.load(runfixture.build(self.root))
        self.store = FakeStore()
        self.log = _Quiet()

    def test_every_file_lands_under_the_run_prefix(self) -> None:
        summary = storage.upload_run(
            self.store,
            self.run_dir.run_id,
            self.run_dir.path,
            self.run_dir.files(),
            self.log,
        )
        self.assertEqual(summary.prefix, f"runs/{runfixture.RUN_ID}")
        self.assertEqual(summary.uploaded, len(self.run_dir.files()))
        self.assertEqual(summary.skipped, 0)
        self.assertTrue(
            all(key.startswith(summary.prefix + "/") for key in self.store.objects)
        )
        self.assertIn(f"runs/{runfixture.RUN_ID}/status.json", self.store.objects)
        self.assertIn(
            f"runs/{runfixture.RUN_ID}/shards/layer00.f32", self.store.objects
        )

    def test_a_second_upload_writes_nothing(self) -> None:
        first = storage.upload_run(
            self.store,
            self.run_dir.run_id,
            self.run_dir.path,
            self.run_dir.files(),
            self.log,
        )
        second = storage.upload_run(
            self.store,
            self.run_dir.run_id,
            self.run_dir.path,
            self.run_dir.files(),
            self.log,
        )
        self.assertEqual(second.uploaded, 0)
        self.assertEqual(second.skipped, first.uploaded)
        self.assertEqual(second.bytes_uploaded, 0)

    def test_an_interrupted_upload_resumes(self) -> None:
        files = self.run_dir.files()
        storage.upload_run(
            self.store, self.run_dir.run_id, self.run_dir.path, files[:3], self.log
        )
        summary = storage.upload_run(
            self.store, self.run_dir.run_id, self.run_dir.path, files, self.log
        )
        self.assertEqual(summary.skipped, 3)
        self.assertEqual(summary.uploaded, len(files) - 3)

    def test_different_content_at_the_same_key_stops_the_publish(self) -> None:
        key = f"runs/{runfixture.RUN_ID}/status.json"
        self.store.seed_different(key, size=12345)
        with self.assertRaises(AdapterError) as caught:
            storage.upload_run(
                self.store,
                self.run_dir.run_id,
                self.run_dir.path,
                self.run_dir.files(),
                self.log,
            )
        self.assertEqual(caught.exception.code, Exit.OBJECT_CONFLICT)
        self.assertEqual(caught.exception.context["object"], key)

    def test_a_failed_upload_is_retryable_not_fatal_to_the_prefix(self) -> None:
        key = f"runs/{runfixture.RUN_ID}/logs.jsonl"
        self.store.fail_on.add(key)
        with self.assertRaises(AdapterError) as caught:
            storage.upload_run(
                self.store,
                self.run_dir.run_id,
                self.run_dir.path,
                self.run_dir.files(),
                self.log,
            )
        self.assertEqual(caught.exception.code, Exit.UPLOAD_FAILED)
        # What did land is intact, so the retry skips it rather than conflicting.
        self.store.fail_on.clear()
        summary = storage.upload_run(
            self.store,
            self.run_dir.run_id,
            self.run_dir.path,
            self.run_dir.files(),
            self.log,
        )
        self.assertGreater(summary.skipped, 0)

    def test_content_types_describe_the_payloads(self) -> None:
        storage.upload_run(
            self.store,
            self.run_dir.run_id,
            self.run_dir.path,
            self.run_dir.files(),
            self.log,
        )
        prefix = f"runs/{runfixture.RUN_ID}"
        self.assertEqual(
            self.store.content_types[f"{prefix}/status.json"], "application/json"
        )
        self.assertEqual(
            self.store.content_types[f"{prefix}/logs.jsonl"], "application/x-ndjson"
        )
        self.assertEqual(
            self.store.content_types[f"{prefix}/shards/layer00.f32"],
            "application/octet-stream",
        )

    def test_digest_is_the_form_the_service_reports(self) -> None:
        # Base64, not hex: comparing a hex digest against a bucket's md5Hash
        # would make every object look different and overwrite every one.
        digest = storage.file_md5(self.run_dir.path / "status.json")
        self.assertEqual(len(digest), 24)
        self.assertTrue(digest.endswith("=="))


if __name__ == "__main__":
    unittest.main()
