"""`sg-publish`: upload one completed run and load its ledger rows.

Order matters. The artifacts go up first and the ledger rows second, so the
ledger never points at a prefix that does not exist yet. The reverse order
would produce a row whose `artifact_uri` is a promise.

The whole operation is idempotent. Objects are compared before being written
and identical ones are skipped; rows are merged on their natural key rather
than appended. Running this twice on the same run directory is a no-op, which
is the property that makes a retry safe to take.

Exit codes are in errors.py. Logs are one JSON object per line on stderr; the
single line on stdout is the machine-readable result, so a wrapper can read it
without parsing logs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import ledger as ledger_module
from . import records, runs, storage
from .errors import AdapterError, Exit
from .logs import Log

TABLE_RUNS = "runs"
TABLE_MEASUREMENTS = "scale_measurements"
TABLE_SUBSPACE = "subspace_summaries"


def publish(
    run: runs.RunDirectory,
    store: storage.ObjectStore | None,
    sink: ledger_module.Ledger | None,
    log: Log,
    alpha: float = records.DEFAULT_FDR_ALPHA,
    prefix: str = "runs",
    verify: bool = True,
) -> dict:
    """Upload the run and merge its rows. Pure orchestration, no clients.

    `store` and `sink` are injected, which is what lets the tests exercise this
    function end to end against fakes, with no credentials and no network.
    Either may be None to skip that half.
    """
    runs.require_succeeded(run)

    missing = run.missing_provenance()
    if missing:
        # Not fatal. A run that cannot say which model revision produced it is
        # still a run; it is just one whose ledger row will say so in nulls.
        log.warning("provenance fields absent, published as null", fields=missing)

    summary = None
    artifact_uri = None
    if store is not None:
        files = run.files()
        if not files:
            raise AdapterError(Exit.INVALID_RUN, "run directory holds no files")
        summary = storage.upload_run(
            store, run.run_id, run.path, files, log, prefix=prefix
        )
        artifact_uri = store.uri(summary.prefix)
        log.info(
            "artifacts published",
            prefix=summary.prefix,
            uploaded=summary.uploaded,
            skipped=summary.skipped,
            bytes=summary.bytes_uploaded,
        )

    measurement_documents = run.measurement_documents()
    subspace_documents = run.subspace_documents()
    measurement_rows = records.measurement_rows(run, measurement_documents, alpha=alpha)
    subspace_rows = records.subspace_rows(run, subspace_documents)

    for rows, key, what in (
        (measurement_rows, records.MEASUREMENT_KEY, TABLE_MEASUREMENTS),
        (subspace_rows, records.SUBSPACE_KEY, TABLE_SUBSPACE),
    ):
        repeated = records.duplicate_keys(rows, key)
        if repeated:
            raise AdapterError(
                Exit.INVALID_RUN,
                f"{what}: {len(repeated)} row(s) share a merge key",
                table=what,
                examples=[list(identity) for identity in repeated[:5]],
            )

    result = {
        "run_id": run.run_id,
        "state": run.state,
        "artifact_uri": artifact_uri,
        "measurement_documents": [name for name, _ in measurement_documents],
        "subspace_documents": [name for name, _ in subspace_documents],
        "measurement_rows": len(measurement_rows),
        "subspace_rows": len(subspace_rows),
        "objects_uploaded": summary.uploaded if summary else 0,
        "objects_skipped": summary.skipped if summary else 0,
        "fdr_alpha": alpha,
        "missing_provenance": missing,
        "loaded": {},
    }

    if sink is None:
        log.info("ledger load skipped", reason="no sink configured")
        return result

    run_record = records.run_row(run, artifact_uri)
    if run_record["created_at"] is None:
        raise AdapterError(Exit.INVALID_RUN, "status.json declares no creation time")

    loads = [
        (TABLE_RUNS, [run_record], ("run_id",), None),
        (
            TABLE_MEASUREMENTS,
            measurement_rows,
            records.MEASUREMENT_KEY,
            [row["layer"] for row in measurement_rows if row["layer"] is not None],
        ),
        (
            TABLE_SUBSPACE,
            subspace_rows,
            records.SUBSPACE_KEY,
            [row["layer"] for row in subspace_rows if row["layer"] is not None],
        ),
    ]

    for table, rows, key, layers in loads:
        outcome = sink.merge(table, rows, key)
        result["loaded"][table] = {
            "rows": outcome.rows,
            "affected": outcome.affected,
            "skipped": outcome.skipped,
        }
        log.info(
            "ledger merged",
            table=table,
            rows=outcome.rows,
            affected=outcome.affected,
            skipped=outcome.skipped,
        )

        if not verify or outcome.skipped:
            continue
        present = sink.count_rows(table, run.run_id, layers)
        if present != outcome.rows:
            # The merge reported success and the table disagrees. Nothing here
            # can tell which is right, so the adapter stops rather than report
            # a load that may be partial.
            raise AdapterError(
                Exit.VERIFICATION_FAILED,
                f"{table} holds {present} row(s) for this run, expected {outcome.rows}",
                table=table,
                expected=outcome.rows,
                present=present,
            )
        result["loaded"][table]["verified"] = present

    return result


def _store_from(args: argparse.Namespace) -> storage.ObjectStore | None:
    if not args.bucket:
        return None
    return storage.GcsStore(args.bucket, project=args.gcs_project or None)


def _ledger_from(args: argparse.Namespace, log: Log) -> ledger_module.Ledger | None:
    if not args.bq_dataset:
        return None
    if not args.bq_project:
        raise AdapterError(Exit.USAGE, "--bq-project is required with --bq-dataset")
    return ledger_module.BigQueryLedger(
        project=args.bq_project,
        dataset=args.bq_dataset,
        location=args.bq_location or None,
        max_bytes_billed=args.max_bytes_billed,
        log=log,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sg-publish",
        description="Publish one completed run to Cloud Storage and BigQuery.",
    )
    parser.add_argument(
        "run",
        help="path to runs/<run-id>, or to the run root when it holds exactly one run",
    )
    parser.add_argument(
        "--bucket",
        default=os.environ.get("ARTIFACT_BUCKET", ""),
        help="artifact bucket; empty skips the upload",
    )
    parser.add_argument(
        "--prefix",
        default=os.environ.get("ARTIFACT_PREFIX", "runs"),
        help="top-level object prefix (default: runs)",
    )
    parser.add_argument("--gcs-project", default=os.environ.get("GCS_PROJECT", ""))
    parser.add_argument(
        "--bq-dataset",
        default=os.environ.get("BQ_DATASET", ""),
        help="ledger dataset; empty skips the load",
    )
    parser.add_argument("--bq-project", default=os.environ.get("BQ_PROJECT", ""))
    parser.add_argument("--bq-location", default=os.environ.get("BQ_LOCATION", ""))
    parser.add_argument(
        "--max-bytes-billed",
        type=int,
        default=int(os.environ.get("BQ_MAX_BYTES_BILLED", 1073741824)),
        help="byte ceiling for every BigQuery job this issues",
    )
    parser.add_argument(
        "--fdr-alpha",
        type=float,
        default=float(os.environ.get("FDR_ALPHA", records.DEFAULT_FDR_ALPHA)),
        help="level for the Benjamini-Hochberg decision stored in `significant`",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="skip the post-merge row count (one query per table)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="read the run, build every row, contact nothing",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log = Log(component="sg-publish")

    try:
        run = runs.load(runs.resolve(Path(args.run)))
        log = log.bind(run_id=run.run_id)

        if args.dry_run:
            store: storage.ObjectStore | None = None
            sink: ledger_module.Ledger | None = None
            log.info("dry run: nothing will be uploaded or loaded")
        else:
            store = _store_from(args)
            sink = _ledger_from(args, log)
            if store is None and sink is None:
                raise AdapterError(
                    Exit.USAGE,
                    "no bucket and no dataset: there is nothing to do",
                )

        result = publish(
            run,
            store,
            sink,
            log,
            alpha=args.fdr_alpha,
            prefix=args.prefix,
            verify=not args.no_verify,
        )
        result["dry_run"] = args.dry_run
        sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
        log.info(
            "publish complete", **{k: v for k, v in result.items() if k != "loaded"}
        )
        return int(Exit.OK)

    except AdapterError as error:
        log.error(str(error), exit_code=int(error.code), **error.context)
        return int(error.code)
    except KeyboardInterrupt:
        log.error("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
