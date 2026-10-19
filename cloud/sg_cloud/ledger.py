"""Loading compact records into the BigQuery ledger, idempotently.

The shape of a load is: write the rows to a per-run staging table, merge the
staging table into the target on the natural key, drop the staging table.

Why not stream the rows in directly? Because a retry after a partial failure
would then insert them twice, and there is no key to deduplicate on afterwards.
A load job followed by a MERGE is idempotent in the only sense that matters
here: running it twice with the same rows leaves the table exactly as running
it once did. It is also the cheap path, because load jobs are free and the
MERGE prunes to the partitions it rewrites, `layer` being both part of the key
and the partitioning column.

Every query carries a byte ceiling. A query that would scan more than the
ceiling is rejected by BigQuery rather than billed, which is the only cost
control in this file that does not depend on anyone remembering it.

One limit is worth stating rather than discovering: the MERGE carries no
constant predicate on `layer`, so it does not satisfy a required partition
filter. Setting `require_partition_filter = true` in Terraform therefore means
changing this file too, which is why that variable defaults to false.

``BigQueryLedger`` imports its client inside the constructor. The SQL builders
and the identifier validation are importable and tested without the SDK.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .errors import AdapterError, Exit
from .logs import Log

#: BigQuery identifiers, and the only thing this module will interpolate into
#: SQL. Values always travel as rows in a staging table, never as literals.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")

#: Staging tables expire on their own, so a crashed adapter cannot leave
#: billable storage behind indefinitely.
STAGING_EXPIRY_HOURS = 6


def check_identifier(name: str, what: str) -> str:
    if not _IDENTIFIER.match(name):
        raise AdapterError(Exit.USAGE, f"unusable {what}: {name!r}")
    return name


def staging_table_name(table: str, run_id: str) -> str:
    """A deterministic, legal staging table name for one run and target.

    Deterministic so that a retry reuses the same staging table instead of
    accumulating one per attempt. The digest carries the run id because a run
    id may contain characters a table name may not, and truncating it would
    let two runs share a staging table.
    """
    check_identifier(table, "table name")
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:16]
    return f"_stage_{table}_{digest}"


def merge_sql(
    project: str,
    dataset: str,
    table: str,
    staging: str,
    columns: Sequence[str],
    key: Sequence[str],
) -> str:
    """The MERGE that makes a reload a no-op.

    Updating matched rows rather than leaving them alone is deliberate: a
    second publish of the same run after a fixed report should correct the
    ledger, not be silently ignored. Since the rows are derived from the run's
    own files, an update either changes nothing or reflects a real change in
    the run directory.
    """
    check_identifier(dataset, "dataset id")
    check_identifier(table, "table name")
    check_identifier(staging, "staging table name")
    for column in columns:
        check_identifier(column, "column name")
    for column in key:
        if column not in columns:
            raise AdapterError(
                Exit.USAGE, f"key column {column!r} is not among the row columns"
            )

    target = f"`{project}.{dataset}.{table}`"
    source = f"`{project}.{dataset}.{staging}`"
    on = " AND ".join(f"T.{c} = S.{c}" for c in key)
    updates = ", ".join(f"{c} = S.{c}" for c in columns if c not in key)
    inserted = ", ".join(columns)
    values = ", ".join(f"S.{c}" for c in columns)

    matched = f"WHEN MATCHED THEN UPDATE SET {updates}\n" if updates else ""
    return (
        f"MERGE {target} AS T\n"
        f"USING {source} AS S\n"
        f"ON {on}\n"
        f"{matched}"
        f"WHEN NOT MATCHED THEN INSERT ({inserted}) VALUES ({values})"
    )


@dataclass
class LoadResult:
    table: str
    rows: int
    affected: int | None = None
    skipped: bool = False
    details: dict = field(default_factory=dict)


class Ledger(Protocol):
    """What the adapter needs from a ledger. The fake in cloud/tests
    implements exactly this, which is why the tests need no credentials."""

    def merge(self, table: str, rows: list[dict], key: Sequence[str]) -> LoadResult: ...

    def count_rows(
        self, table: str, run_id: str, layers: Sequence[int] | None = None
    ) -> int: ...


class BigQueryLedger:
    def __init__(
        self,
        project: str,
        dataset: str,
        location: str | None = None,
        max_bytes_billed: int | None = None,
        log: Log | None = None,
    ) -> None:
        from google.cloud import bigquery  # imported here, not at module scope

        self._bq = bigquery
        self._project = project
        self._dataset = check_identifier(dataset, "dataset id")
        self._location = location
        self._max_bytes_billed = max_bytes_billed
        self._log = log or Log()
        self._client = bigquery.Client(project=project, location=location)

    def _reference(self, table: str) -> str:
        return (
            f"{self._project}.{self._dataset}.{check_identifier(table, 'table name')}"
        )

    def _query(self, sql: str) -> Any:
        config = self._bq.QueryJobConfig()
        if self._max_bytes_billed is not None:
            config.maximum_bytes_billed = self._max_bytes_billed
        job = self._client.query(sql, job_config=config, location=self._location)
        job.result()
        return job

    def merge(self, table: str, rows: list[dict], key: Sequence[str]) -> LoadResult:
        if not rows:
            return LoadResult(table=table, rows=0, skipped=True)

        run_ids = {row.get("run_id") for row in rows}
        if len(run_ids) != 1 or None in run_ids:
            raise AdapterError(
                Exit.USAGE,
                f"a merge into {table} must carry exactly one run_id",
                run_ids=sorted(str(r) for r in run_ids),
            )
        run_id = next(iter(run_ids))
        staging = staging_table_name(table, str(run_id))

        target = self._client.get_table(self._reference(table))
        columns = [column.name for column in target.schema]
        unknown = sorted(set(rows[0]) - set(columns))
        if unknown:
            # Fail here rather than let BigQuery silently reject a field, which
            # would leave the table short of a column nobody notices.
            raise AdapterError(
                Exit.LEDGER_FAILED,
                f"{table} has no column(s) {', '.join(unknown)}",
                table=table,
                unknown=unknown,
            )

        try:
            staging_ref = self._bq.Table(self._reference(staging), schema=target.schema)
            staging_ref.expires = self._expiry()
            self._client.delete_table(staging_ref, not_found_ok=True)
            self._client.create_table(staging_ref)

            config = self._bq.LoadJobConfig(
                schema=target.schema,
                write_disposition="WRITE_TRUNCATE",
            )
            load = self._client.load_table_from_json(
                [{column: row.get(column) for column in columns} for row in rows],
                staging_ref,
                job_config=config,
                location=self._location,
            )
            load.result()

            job = self._query(
                merge_sql(self._project, self._dataset, table, staging, columns, key)
            )
            affected = getattr(job, "num_dml_affected_rows", None)
        except AdapterError:
            raise
        except Exception as error:
            raise AdapterError(
                Exit.LEDGER_FAILED,
                f"loading {table} failed: {error}",
                table=table,
            ) from error
        finally:
            # Leaving staging tables behind is a slow, quiet cost. They also
            # expire on their own, in case this never runs.
            try:
                self._client.delete_table(self._reference(staging), not_found_ok=True)
            except Exception as error:
                self._log.warning(
                    "staging table not dropped", table=staging, error=str(error)
                )

        return LoadResult(table=table, rows=len(rows), affected=affected)

    def count_rows(
        self, table: str, run_id: str, layers: Sequence[int] | None = None
    ) -> int:
        """Rows in `table` for one run, read back after the merge.

        Every value is a query parameter, so a run id is never interpolated
        into SQL. The optional layer list prunes the scan to the partitions the
        merge touched, and is also what lets this query run against a table
        that requires a partition filter.
        """
        parameters: list[Any] = [
            self._bq.ScalarQueryParameter("run_id", "STRING", run_id)
        ]
        predicate = "run_id = @run_id"
        if layers:
            parameters.append(
                self._bq.ArrayQueryParameter("layers", "INT64", sorted(set(layers)))
            )
            predicate += " AND layer IN UNNEST(@layers)"

        config = self._bq.QueryJobConfig(query_parameters=parameters)
        if self._max_bytes_billed is not None:
            config.maximum_bytes_billed = self._max_bytes_billed
        sql = f"SELECT COUNT(*) AS n FROM `{self._reference(table)}` WHERE {predicate}"
        job = self._client.query(sql, job_config=config, location=self._location)
        for row in job.result():
            return int(row["n"])
        return 0

    def _expiry(self):
        from datetime import datetime, timedelta, timezone

        return datetime.now(tz=timezone.utc) + timedelta(hours=STAGING_EXPIRY_HOURS)
