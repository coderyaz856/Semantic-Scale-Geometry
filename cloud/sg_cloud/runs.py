"""Reading and validating a completed local run directory.

The directory contract is the one frozen in docs/production-roadmap.md:

    runs/<run-id>/
      config.yaml
      manifest.json
      status.json
      logs.jsonl
      shards/
      directions/
      measurements/
      report/

The runtime and cloud adapter share this contract. Every field the adapter
needs is listed once, in ``STATUS_FIELDS`` and ``MANIFEST_FIELDS``. The first
path for each manifest field is the runtime's versioned spelling; later paths
retain compatibility with early platform fixtures. A field that cannot be
found is published as null rather than inferred: a wrong provenance value is
worse than a missing one, because only the missing one is visible.

Result documents are classified by shape rather than by filename, for the same
reason: the producer is not written yet, and a layout that depends on a file
being called the right thing is a layout that breaks on a rename.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import AdapterError, Exit

SUCCEEDED = "SUCCEEDED"

#: Files that must exist for a directory to be a run at all.
REQUIRED_FILES = ("config.yaml", "manifest.json", "status.json")

#: Subdirectories that may hold result documents, in the order searched.
RESULT_DIRECTORIES = ("measurements", "report")

#: Ledger field -> candidate keys in status.json, first match wins.
STATUS_FIELDS: dict[str, tuple[str, ...]] = {
    "run_id": ("run_id", "runId"),
    "state": ("state", "status"),
    "created_at": ("created_at", "created_utc"),
    "completed_at": ("completed_at", "finished_at"),
    "error_code": ("error_code", "error"),
    "duration_seconds": ("duration_seconds", "elapsed_seconds"),
}

#: Ledger field -> candidate paths in manifest.json. A dotted path descends
#: through nested objects.
MANIFEST_FIELDS: dict[str, tuple[str, ...]] = {
    "git_commit": ("git_commit",),
    "config_digest": ("config_digest",),
    "model_id": ("config.model.id", "model_id", "model.id"),
    "model_revision": (
        "config.model.revision",
        "model_revision",
        "model.revision",
        "config.model_revision",
    ),
    "scales_digest": (
        "inputs.scales_sha256",
        "scales_digest",
        "inputs.scales_digest",
    ),
    "background_digest": (
        "inputs.background_sha256",
        "background_digest",
        "inputs.background_digest",
    ),
    "container_digest": ("container_digest",),
    "device": ("config.model.device", "device", "config.device"),
    "duration_seconds": ("duration_seconds", "timings.total_seconds"),
}


def _dig(document: Any, path: str) -> Any:
    """Follow a dotted path, returning None at the first missing step."""
    current = document
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _first(document: dict, candidates: Sequence[str]) -> Any:
    for candidate in candidates:
        value = _dig(document, candidate)
        if value is not None:
            return value
    return None


def _read_json(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise AdapterError(
            Exit.INVALID_RUN, f"cannot read {path.name}: {error}"
        ) from error
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise AdapterError(
            Exit.INVALID_RUN, f"{path.name} is not valid JSON: {error}"
        ) from error
    if not isinstance(document, dict):
        raise AdapterError(Exit.INVALID_RUN, f"{path.name} must hold a JSON object")
    return document


def normalize_timestamp(value: Any) -> str | None:
    """Return an RFC 3339 string in UTC, or None.

    BigQuery accepts several timestamp spellings on load, which is exactly why
    this normalizes instead of passing the value through: a ledger whose
    timestamps are sometimes offset-naive cannot be ordered.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        moment = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as error:
            raise AdapterError(
                Exit.INVALID_RUN, f"unparseable timestamp {value!r}"
            ) from error
        if moment.tzinfo is None:
            # A naive timestamp from a container is UTC in practice. Recording
            # that assumption here is better than letting it vary per reader.
            moment = moment.replace(tzinfo=timezone.utc)
    else:
        raise AdapterError(
            Exit.INVALID_RUN, f"unusable timestamp type {type(value).__name__}"
        )
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class RunDirectory:
    """A validated local run directory."""

    path: Path
    run_id: str
    state: str
    status: dict
    manifest: dict

    @property
    def succeeded(self) -> bool:
        return self.state == SUCCEEDED

    def status_field(self, name: str) -> Any:
        return _first(self.status, STATUS_FIELDS[name])

    def manifest_field(self, name: str) -> Any:
        return _first(self.manifest, MANIFEST_FIELDS[name])

    def missing_provenance(self) -> list[str]:
        """Ledger fields no source could supply. Reported, never invented."""
        return [name for name in MANIFEST_FIELDS if self.manifest_field(name) is None]

    def files(self) -> list[Path]:
        """Every regular file in the run directory, in a stable order.

        Sorted so that two uploads of the same directory visit the same objects
        in the same sequence, which makes a partial upload resumable and its
        logs comparable.
        """
        return sorted(p for p in self.path.rglob("*") if p.is_file())

    def relative(self, file: Path) -> str:
        return file.relative_to(self.path).as_posix()

    def _result_documents(self) -> Iterator[tuple[Path, Any]]:
        for directory in RESULT_DIRECTORIES:
            root = self.path / directory
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*.json")):
                try:
                    yield path, json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as error:
                    raise AdapterError(
                        Exit.INVALID_RUN,
                        f"cannot read result document {self.relative(path)}: {error}",
                    ) from error

    def measurement_documents(self) -> list[tuple[str, dict]]:
        """Documents shaped like a measurement file: an object with `results`."""
        found = []
        for path, document in self._result_documents():
            if isinstance(document, dict) and isinstance(document.get("results"), list):
                found.append((self.relative(path), document))
        return found

    def subspace_documents(self) -> list[tuple[str, list]]:
        """Documents shaped like a subspace analysis: a list of groups."""
        found = []
        for path, document in self._result_documents():
            if (
                isinstance(document, list)
                and document
                and isinstance(document[0], dict)
                and "observed" in document[0]
            ):
                found.append((self.relative(path), document))
        return found


def load(path: Path | str) -> RunDirectory:
    """Read and validate a run directory. Raises AdapterError, never guesses."""
    root = Path(path)
    if not root.is_dir():
        raise AdapterError(Exit.INVALID_RUN, f"not a directory: {root}")

    missing = [name for name in REQUIRED_FILES if not (root / name).is_file()]
    if missing:
        raise AdapterError(
            Exit.INVALID_RUN,
            f"run directory is missing {', '.join(missing)}",
            missing=missing,
        )

    status = _read_json(root / "status.json")
    manifest = _read_json(root / "manifest.json")

    run_id = _first(status, STATUS_FIELDS["run_id"]) or root.name
    state = _first(status, STATUS_FIELDS["state"])
    if not isinstance(state, str) or not state:
        raise AdapterError(Exit.INVALID_RUN, "status.json declares no run state")

    if not isinstance(run_id, str) or "/" in run_id or run_id in {".", ".."}:
        # The run id becomes an object prefix, so a separator in it would
        # silently reshape the bucket layout.
        raise AdapterError(Exit.INVALID_RUN, f"unusable run id {run_id!r}")

    return RunDirectory(
        path=root,
        run_id=run_id,
        state=state,
        status=status,
        manifest=manifest,
    )


def locate(run_root: Path | str) -> Path:
    """Find the run directory under a run root.

    The frozen `sg run` contract does not say that the command prints its run
    id, so the wrapper has to find the directory it wrote. In a container that
    starts with an empty run root there is exactly one candidate, which is the
    only case this treats as unambiguous. More than one means the root was
    reused, and then the newest is a guess, so the caller is told it is one.
    """
    root = Path(run_root)
    if not root.is_dir():
        raise AdapterError(Exit.INVALID_RUN, f"run root does not exist: {root}")

    candidates = [
        p
        for p in sorted(root.iterdir())
        if p.is_dir() and (p / "status.json").is_file()
    ]
    if not candidates:
        raise AdapterError(
            Exit.INVALID_RUN,
            f"no run directory with a status.json under {root}",
            run_root=str(root),
        )
    if len(candidates) > 1:
        raise AdapterError(
            Exit.INVALID_RUN,
            f"{len(candidates)} run directories under {root}; name the one to publish",
            candidates=[p.name for p in candidates],
        )
    return candidates[0]


def resolve(path: Path | str) -> Path:
    """Accept either a run directory or the root one was written under."""
    candidate = Path(path)
    if (candidate / "status.json").is_file():
        return candidate
    return locate(candidate)


def require_succeeded(run: RunDirectory) -> None:
    """Refuse to publish anything but a successful run.

    A failed run's logs are worth keeping, but they are not a measurement, and
    the ledger is read as a list of measurements.
    """
    if not run.succeeded:
        raise AdapterError(
            Exit.RUN_NOT_SUCCEEDED,
            f"run {run.run_id} is in state {run.state}, not {SUCCEEDED}",
            run_id=run.run_id,
            state=run.state,
        )
