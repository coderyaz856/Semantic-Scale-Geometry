"""Structured logs.

Cloud Logging parses one JSON object per line on stderr and promotes known
fields, so `severity` and `message` use the names it expects. Everything else
travels as a field rather than being formatted into the message, because a
number inside a sentence cannot be filtered on.

Writing to stderr keeps stdout free for a machine-readable result, which is
what the job wrapper reads.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, TextIO


class Log:
    def __init__(self, stream: TextIO | None = None, **base: Any) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._base = dict(base)

    def bind(self, **fields: Any) -> Log:
        """A child logger that repeats these fields on every line."""
        merged = dict(self._base)
        merged.update(fields)
        return Log(self._stream, **merged)

    def _emit(self, severity: str, message: str, fields: dict[str, Any]) -> None:
        record: dict[str, Any] = {
            "severity": severity,
            "message": message,
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        record.update(self._base)
        record.update(fields)
        # default=str rather than a failure: a log line must not be the thing
        # that breaks a successful publish.
        self._stream.write(json.dumps(record, default=str) + "\n")
        self._stream.flush()

    def info(self, message: str, **fields: Any) -> None:
        self._emit("INFO", message, fields)

    def warning(self, message: str, **fields: Any) -> None:
        self._emit("WARNING", message, fields)

    def error(self, message: str, **fields: Any) -> None:
        self._emit("ERROR", message, fields)
