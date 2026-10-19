"""Exit codes.

A caller that can only distinguish success from failure cannot retry
intelligently. These codes separate the three cases that need different
responses: do not retry (the input is wrong), retry later (the service was
unavailable), and stop and look (the destination disagrees with the source).
"""

from __future__ import annotations

from enum import IntEnum


class Exit(IntEnum):
    """Process exit codes. Stable: scripts and the job wrapper depend on them."""

    OK = 0

    #: Arguments were unusable. Retrying changes nothing.
    USAGE = 2

    #: The run directory is missing files or malformed. Not retryable.
    INVALID_RUN = 3

    #: The run exists and is well formed but did not succeed, so there is
    #: nothing to publish. Not retryable, and not an error in this adapter.
    RUN_NOT_SUCCEEDED = 4

    #: An object already exists at the destination with different content. The
    #: per-run prefix is immutable, so this is never resolved by retrying; it
    #: means two different runs claimed one run id.
    OBJECT_CONFLICT = 5

    #: Upload failed. Retryable: the operation is idempotent by content.
    UPLOAD_FAILED = 6

    #: BigQuery load or merge failed. Retryable for the same reason.
    LEDGER_FAILED = 7

    #: Upload and load reported success, but the post-check disagreed.
    VERIFICATION_FAILED = 8


class AdapterError(Exception):
    """An error that carries the exit code the process should use."""

    def __init__(self, code: Exit, message: str, **context: object) -> None:
        super().__init__(message)
        self.code = code
        self.context = context
