"""The SQL and the names, tested without a client.

These are the parts of the ledger that can be wrong in a way no local run would
reveal: a MERGE that updates a key column, an identifier that reaches SQL
unchecked, a staging table that two runs share.
"""

from __future__ import annotations

import unittest

import _bootstrap  # noqa: F401  - import path, must come first
from sg_cloud.errors import AdapterError, Exit
from sg_cloud.ledger import check_identifier, merge_sql, staging_table_name

COLUMNS = ["run_id", "layer", "scale_id", "value"]
KEY = ["run_id", "layer", "scale_id"]


class IdentifierTests(unittest.TestCase):
    def test_plain_names_pass(self) -> None:
        self.assertEqual(
            check_identifier("scale_measurements", "table"), "scale_measurements"
        )

    def test_anything_that_could_end_a_statement_is_refused(self) -> None:
        for name in ("runs; DROP TABLE x", "runs`", "a-b", "1runs", "", "a" * 200):
            with self.subTest(name=name), self.assertRaises(AdapterError) as caught:
                check_identifier(name, "table")
            self.assertEqual(caught.exception.code, Exit.USAGE)


class StagingNameTests(unittest.TestCase):
    def test_deterministic_so_a_retry_reuses_one_table(self) -> None:
        self.assertEqual(
            staging_table_name("runs", "r-1"),
            staging_table_name("runs", "r-1"),
        )

    def test_two_runs_never_share_a_staging_table(self) -> None:
        self.assertNotEqual(
            staging_table_name("runs", "r-1"),
            staging_table_name("runs", "r-2"),
        )

    def test_the_name_is_legal_whatever_the_run_id_contains(self) -> None:
        name = staging_table_name("runs", "../weird id/with separators")
        check_identifier(name, "table")
        self.assertTrue(name.startswith("_stage_runs_"))


class MergeSqlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sql = merge_sql("p", "d", "scale_measurements", "_stage", COLUMNS, KEY)

    def test_matches_on_every_key_column(self) -> None:
        for column in KEY:
            self.assertIn(f"T.{column} = S.{column}", self.sql)

    def test_updates_only_non_key_columns(self) -> None:
        self.assertIn("UPDATE SET value = S.value", self.sql)
        for column in KEY:
            self.assertNotIn(f"{column} = S.{column},", self.sql)

    def test_inserts_every_column_when_unmatched(self) -> None:
        self.assertIn("INSERT (run_id, layer, scale_id, value)", self.sql)
        self.assertIn("VALUES (S.run_id, S.layer, S.scale_id, S.value)", self.sql)

    def test_fully_qualified_names_so_the_default_dataset_is_irrelevant(self) -> None:
        self.assertIn("`p.d.scale_measurements`", self.sql)
        self.assertIn("`p.d._stage`", self.sql)

    def test_a_key_outside_the_columns_is_refused(self) -> None:
        with self.assertRaises(AdapterError):
            merge_sql("p", "d", "t", "_stage", COLUMNS, ["run_id", "absent"])

    def test_a_table_of_nothing_but_key_columns_still_merges(self) -> None:
        # No UPDATE clause is possible, and an empty `SET` would be a syntax
        # error, so the statement has to drop that branch entirely.
        sql = merge_sql("p", "d", "t", "_stage", ["run_id"], ["run_id"])
        self.assertNotIn("WHEN MATCHED", sql)
        self.assertIn("WHEN NOT MATCHED THEN INSERT (run_id) VALUES (S.run_id)", sql)


if __name__ == "__main__":
    unittest.main()
