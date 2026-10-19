"""`sg-salvage`: keep the evidence from a run that failed.

A failed run is not published to the ledger, because the ledger is read as a
list of measurements. Its logs are still the only record of why it failed, and
in a job whose filesystem is memory-backed they disappear the moment the task
exits. So they go to a separate prefix, `failures/`, which no analysis query
reads and no lifecycle rule treats as a result.

Upload only, no ledger write, and a failure to salvage is reported without
changing the exit code of the run it is salvaging: losing the logs is bad, but
replacing the original failure with an upload error would be worse.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .errors import AdapterError, Exit
from .logs import Log
from .storage import GcsStore, upload_run

FAILURE_PREFIX = "failures"

#: Names worth keeping from a run that did not finish. Shard payloads are not
#: among them: they are large, regenerable and not evidence of anything.
SALVAGE_NAMES = ("status.json", "manifest.json", "logs.jsonl", "config.yaml")
SALVAGE_SUFFIXES = (".log", ".jsonl", ".json", ".yaml", ".txt")
MAX_SALVAGE_BYTES = 16 * 1024 * 1024


def salvageable(root: Path) -> list[Path]:
    """Small text artifacts under `root`, in path order, bounded in total size."""
    chosen: list[Path] = []
    budget = MAX_SALVAGE_BYTES
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if (
            path.name not in SALVAGE_NAMES
            and path.suffix.lower() not in SALVAGE_SUFFIXES
        ):
            continue
        size = path.stat().st_size
        if size > budget:
            continue
        budget -= size
        chosen.append(path)
    return chosen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sg-salvage",
        description="Upload the logs and status of a run that did not succeed.",
    )
    parser.add_argument("run", help="path to a run directory, complete or not")
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--run-id", default="", help="defaults to the directory name")
    parser.add_argument("--prefix", default=FAILURE_PREFIX)
    parser.add_argument("--gcs-project", default="")
    args = parser.parse_args(argv)

    log = Log(component="sg-salvage")
    root = Path(args.run)
    run_id = args.run_id or root.name

    try:
        if not root.is_dir():
            raise AdapterError(Exit.INVALID_RUN, f"not a directory: {root}")
        files = salvageable(root)
        if not files:
            log.warning("nothing to salvage", path=str(root))
            return int(Exit.OK)
        store = GcsStore(args.bucket, project=args.gcs_project or None)
        summary = upload_run(store, run_id, root, files, log, prefix=args.prefix)
        sys.stdout.write(store.uri(summary.prefix) + "\n")
        log.info(
            "failure evidence salvaged",
            prefix=summary.prefix,
            uploaded=summary.uploaded,
            skipped=summary.skipped,
        )
        return int(Exit.OK)
    except AdapterError as error:
        log.error(str(error), exit_code=int(error.code), **error.context)
        return int(error.code)


if __name__ == "__main__":
    raise SystemExit(main())
