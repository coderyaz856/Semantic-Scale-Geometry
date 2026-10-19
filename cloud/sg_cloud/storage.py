"""Uploading a run directory under an immutable prefix.

Idempotence here is by content, not by bookkeeping. Before writing an object
the adapter asks what is already there:

  * nothing  -> upload it;
  * the same bytes (same size and MD5) -> skip it, and say so;
  * different bytes -> stop, with OBJECT_CONFLICT.

The third case is not a retryable failure. The prefix is a run id derived from
the configuration and the input identities, so two different byte streams under
one prefix mean two different runs claimed one identity, and overwriting
whichever arrived second would destroy the evidence that this happened.

The runtime service account is granted object creation and reading and is not
granted deletion, so the same rule is enforced by IAM even if this code is
wrong. See infra/iam.tf.

``GcsStore`` imports its client inside the constructor, so this module is
importable without the Google SDK installed and the tests run against
``FakeStore`` in cloud/tests.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .errors import AdapterError, Exit
from .logs import Log

#: Read in blocks: a shard payload is large and does not need to be resident.
_BLOCK = 1024 * 1024


@dataclass(frozen=True)
class ObjectInfo:
    size: int
    md5: str | None


def file_md5(path: Path) -> str:
    """Base64 MD5, matching the form Cloud Storage reports."""
    # MD5 because that is what Cloud Storage reports. It is an integrity
    # comparison against object metadata, not a security claim.
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_BLOCK), b""):
            digest.update(block)
    return base64.b64encode(digest.digest()).decode("ascii")


class ObjectStore(Protocol):
    """The three operations the adapter needs. Implemented by GcsStore and by
    the test fake, which is the point of stating it."""

    def info(self, key: str) -> ObjectInfo | None: ...

    def upload(self, key: str, path: Path, content_type: str | None = None) -> None: ...

    def uri(self, key: str) -> str: ...


class GcsStore:
    def __init__(self, bucket: str, project: str | None = None) -> None:
        from google.cloud import storage  # imported here, not at module scope

        self._bucket_name = bucket
        self._client = storage.Client(project=project)
        self._bucket = self._client.bucket(bucket)

    def info(self, key: str) -> ObjectInfo | None:
        blob = self._bucket.get_blob(key)
        if blob is None:
            return None
        return ObjectInfo(size=int(blob.size or 0), md5=blob.md5_hash)

    def upload(self, key: str, path: Path, content_type: str | None = None) -> None:
        blob = self._bucket.blob(key)
        # if_generation_match=0 makes the write conditional on the object not
        # existing, so a concurrent uploader cannot be overwritten in the
        # window between the check above and the write here.
        blob.upload_from_filename(
            str(path), content_type=content_type, if_generation_match=0
        )

    def uri(self, key: str) -> str:
        return f"gs://{self._bucket_name}/{key}"


#: Extension -> content type. Enough to make a browser and `gsutil cat`
#: behave; anything else is uploaded as an opaque stream, which a raw f32
#: payload is.
CONTENT_TYPES = {
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".f32": "application/octet-stream",
}


def content_type_for(path: Path) -> str | None:
    return CONTENT_TYPES.get(path.suffix.lower())


@dataclass
class UploadSummary:
    prefix: str
    uploaded: int
    skipped: int
    bytes_uploaded: int

    @property
    def total(self) -> int:
        return self.uploaded + self.skipped


def upload_run(
    store: ObjectStore,
    run_id: str,
    root: Path,
    files: list[Path],
    log: Log,
    prefix: str = "runs",
) -> UploadSummary:
    """Upload every file under `runs/<run_id>/`, verifying before replacing."""
    base = f"{prefix}/{run_id}"
    uploaded = 0
    skipped = 0
    written = 0

    for path in files:
        relative = path.relative_to(root).as_posix()
        key = f"{base}/{relative}"
        size = path.stat().st_size
        existing = store.info(key)

        if existing is not None:
            digest = file_md5(path)
            if existing.size == size and (
                existing.md5 is None or existing.md5 == digest
            ):
                skipped += 1
                log.info("object already present, skipped", object=key, bytes=size)
                continue
            raise AdapterError(
                Exit.OBJECT_CONFLICT,
                f"{key} exists with different content",
                object=key,
                existing_size=existing.size,
                local_size=size,
            )

        try:
            store.upload(key, path, content_type_for(path))
        except AdapterError:
            raise
        # The client library's exception hierarchy is not ours to enumerate,
        # and an unclassified upload failure is still a retryable one.
        except Exception as error:
            raise AdapterError(
                Exit.UPLOAD_FAILED,
                f"upload of {key} failed: {error}",
                object=key,
            ) from error

        uploaded += 1
        written += size
        log.info("uploaded", object=key, bytes=size)

    return UploadSummary(
        prefix=base, uploaded=uploaded, skipped=skipped, bytes_uploaded=written
    )
