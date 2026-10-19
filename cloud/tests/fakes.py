"""In-memory stand-ins for Cloud Storage and BigQuery.

They implement the two Protocols in sg_cloud.storage and sg_cloud.ledger and
nothing else, so a test that passes against them is a test of the adapter's
logic rather than of a mocking library. No credentials, no network, no SDK.

The fakes are deliberately strict where the real services are strict: the
store refuses to overwrite an existing object, because the real bucket's
service account has no delete permission, and a fake that silently allowed it
would hide the failure mode the adapter exists to prevent.
"""

from __future__ import annotations

from pathlib import Path

from sg_cloud.ledger import LoadResult
from sg_cloud.storage import ObjectInfo, file_md5


class FakeStore:
    def __init__(self, bucket: str = "fake-bucket") -> None:
        self.bucket = bucket
        self.objects: dict[str, tuple[int, str]] = {}
        self.content_types: dict[str, str | None] = {}
        self.uploads: list[str] = []
        self.fail_on: set[str] = set()

    def seed(self, key: str, path: Path) -> None:
        """Pretend an earlier run already uploaded this exact file."""
        self.objects[key] = (path.stat().st_size, file_md5(path))

    def seed_different(self, key: str, size: int = 1) -> None:
        self.objects[key] = (size, "0" * 24)

    def info(self, key: str) -> ObjectInfo | None:
        if key not in self.objects:
            return None
        size, digest = self.objects[key]
        return ObjectInfo(size=size, md5=digest)

    def upload(self, key: str, path: Path, content_type: str | None = None) -> None:
        if key in self.fail_on:
            raise RuntimeError("injected upload failure")
        if key in self.objects:
            raise AssertionError(f"overwrote {key}, which the real bucket would refuse")
        self.objects[key] = (path.stat().st_size, file_md5(path))
        self.content_types[key] = content_type
        self.uploads.append(key)

    def uri(self, key: str) -> str:
        return f"gs://{self.bucket}/{key}"


class FakeLedger:
    """A merge-on-key table set, which is the whole contract under test."""

    def __init__(self) -> None:
        self.tables: dict[str, dict[tuple, dict]] = {}
        self.merges: list[tuple[str, int]] = []
        self.fail_on: set[str] = set()

    def merge(self, table: str, rows: list[dict], key) -> LoadResult:
        if table in self.fail_on:
            raise RuntimeError("injected merge failure")
        if not rows:
            return LoadResult(table=table, rows=0, skipped=True)
        store = self.tables.setdefault(table, {})
        for row in rows:
            store[tuple(row.get(name) for name in key)] = dict(row)
        self.merges.append((table, len(rows)))
        return LoadResult(table=table, rows=len(rows), affected=len(rows))

    def count_rows(self, table: str, run_id: str, layers=None) -> int:
        rows = self.tables.get(table, {}).values()
        return sum(1 for row in rows if row.get("run_id") == run_id)
