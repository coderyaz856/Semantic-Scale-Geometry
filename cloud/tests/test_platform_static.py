"""Static checks over the platform files.

These are the checks that do not need a cloud, a daemon or a Terraform binary,
and two of them are worth more than the tooling they stand in for.

The first is the schema cross-check: every column the adapter writes must exist
in the Terraform schema for that table, and every required column must be
written. A rename on one side of that boundary is otherwise discovered by a
failed load job in the cloud, which is the most expensive place to discover it.

The second is the scan for things that must never be committed: a hard-coded
project, an address, a key, a local path. `terraform validate` would pass
happily with all four.

The scan deliberately skips this directory, because the patterns it looks for
are written out here.
"""

from __future__ import annotations

import base64
import json
import re
import tempfile
import unittest
from pathlib import Path

import _bootstrap
import runfixture
import yaml
from sg_cloud import records, runs

REPO = _bootstrap.REPO
INFRA = REPO / "infra"
SCHEMAS = INFRA / "bigquery"
DEPLOY = REPO / ".github" / "workflows" / "deploy.yml"

#: Owned by this track, and therefore scanned. cloud/tests is excluded: the
#: forbidden patterns are listed in this file.
SCANNED = (
    INFRA,
    REPO / "cloud" / "sg_cloud",
    REPO / "cloud" / "entrypoint.sh",
    REPO / "cloud" / "requirements.txt",
    REPO / "docker",
    DEPLOY,
    REPO / "docs" / "cloud-deployment.md",
)

#: Values that identify a person, an account or a machine. The target project
#: is among them: it belongs in a variable file outside the repository, not in
#: a module that another project is supposed to be able to deploy. It is read
#: from that ignored file when present, so the scan never publishes it either.
FORBIDDEN = (
    "@gmail.com",
    "BEGIN PRIVATE KEY",
    "BEGIN RSA PRIVATE KEY",
    "credentials_json",
    "service_account_key",
    "C:/Users",
    "C:\\\\Users",
    "/home/",
    "/Users/",
    "F:/",
)

#: Attribution that must not appear in tracked files, this one included, so the
#: needles are stored encoded.
FORBIDDEN_ATTRIBUTION = tuple(
    base64.b64decode(encoded).decode("ascii")
    for encoded in (
        "Y2xhdWRl",
        "Y29kZXg=",
        "Y29waWxvdA==",
        "YWktZ2VuZXJhdGVk",
        "YWkgZ2VuZXJhdGVk",
        "Y2hhdGdwdA==",
        "b3BlbmFp",
        "YW50aHJvcGlj",
    )
)


def local_project_ids() -> tuple[str, ...]:
    """The project named in the ignored variables file, if there is one."""
    variables = INFRA / "terraform.tfvars"
    if not variables.is_file():
        return ()
    text = variables.read_text(encoding="utf-8", errors="replace")
    return tuple(re.findall(r'^\s*project_id\s*=\s*"([^"]+)"', text, re.MULTILINE))


TEXT_SUFFIXES = {
    ".tf",
    ".json",
    ".yml",
    ".yaml",
    ".sh",
    ".py",
    ".md",
    ".txt",
    ".example",
    ".dockerignore",
    "",
}


def scanned_files() -> list[Path]:
    found: list[Path] = []
    for target in SCANNED:
        if target.is_file():
            found.append(target)
        elif target.is_dir():
            found.extend(
                p
                for p in sorted(target.rglob("*"))
                if p.is_file()
                and p.suffix in TEXT_SUFFIXES
                and "__pycache__" not in p.parts
                and ".terraform" not in p.parts
            )
    return found


def terraform_files() -> list[Path]:
    return sorted(INFRA.glob("*.tf"))


class LayoutTests(unittest.TestCase):
    def test_every_expected_file_is_present(self) -> None:
        for path in (
            INFRA / "versions.tf",
            INFRA / "variables.tf",
            INFRA / "apis.tf",
            INFRA / "storage.tf",
            INFRA / "bigquery.tf",
            INFRA / "iam.tf",
            INFRA / "cloud_run.tf",
            INFRA / "outputs.tf",
            INFRA / "terraform.tfvars.example",
            SCHEMAS / "runs.json",
            SCHEMAS / "scale_measurements.json",
            SCHEMAS / "subspace_summaries.json",
            REPO / "cloud" / "entrypoint.sh",
            REPO / "cloud" / "requirements.txt",
            REPO / "docker" / "job.Dockerfile",
            REPO / "docker" / "adapter.Dockerfile",
            DEPLOY,
            REPO / "docs" / "cloud-deployment.md",
        ):
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file(), f"missing {path}")

    def test_runtime_owned_paths_are_untouched_by_this_track(self) -> None:
        # The cloud track owns no file under these paths. Asserting it here
        # means an accidental edit shows up as a failing test rather than as a
        # merge conflict on the runtime branch.
        for path in ("README.md", "pyproject.toml", "Dockerfile"):
            self.assertTrue((REPO / path).is_file(), path)
        self.assertFalse((REPO / "infra" / "pyproject.toml").exists())


class SecretScanTests(unittest.TestCase):
    def test_no_identifiers_addresses_keys_or_local_paths(self) -> None:
        for path in scanned_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            for needle in FORBIDDEN + local_project_ids():
                with self.subTest(path=path.name, needle=needle):
                    self.assertNotIn(needle, text)

    def test_no_assistant_or_agent_attribution(self) -> None:
        for path in scanned_files():
            lowered = path.read_text(encoding="utf-8", errors="replace").lower()
            for needle in FORBIDDEN_ATTRIBUTION:
                with self.subTest(path=path.name, needle=needle):
                    self.assertNotIn(needle, lowered)

    def test_the_example_variables_file_holds_a_placeholder(self) -> None:
        text = (INFRA / "terraform.tfvars.example").read_text(encoding="utf-8")
        self.assertIn("replace-with-your-project-id", text)

    def test_no_trailing_whitespace_or_tabs_in_terraform(self) -> None:
        for path in terraform_files():
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                with self.subTest(path=path.name, line=number):
                    self.assertNotIn("\t", line)
                    self.assertEqual(line, line.rstrip())


class TerraformTests(unittest.TestCase):
    """What can be checked without the binary. Not a substitute for
    `terraform validate`, and docs/cloud-deployment.md says so."""

    @staticmethod
    def _decomment(text: str) -> str:
        """Drop comments and heredoc descriptions, keep the code."""
        text = re.sub(r"#[^\n]*", "", text)
        return re.sub(r"<<-?EOT.*?EOT", '""', text, flags=re.DOTALL)

    @classmethod
    def _strip(cls, text: str) -> str:
        """Also blank string literals, for the structural checks.

        Not used for the variable checks: a reference inside an interpolated
        string is still a reference, and blanking it would report a used
        variable as unused.
        """
        return re.sub(r'"(?:[^"\\]|\\.)*"', '""', cls._decomment(text))

    def test_braces_and_brackets_balance(self) -> None:
        for path in terraform_files():
            stripped = self._strip(path.read_text(encoding="utf-8"))
            with self.subTest(path=path.name):
                self.assertEqual(stripped.count("{"), stripped.count("}"), "braces")
                self.assertEqual(stripped.count("["), stripped.count("]"), "brackets")

    def test_every_referenced_variable_is_declared(self) -> None:
        declared = set(
            re.findall(
                r'variable\s+"([a-z0-9_]+)"',
                (INFRA / "variables.tf").read_text("utf-8"),
            )
        )
        self.assertIn("project_id", declared)
        for path in terraform_files():
            body = self._decomment(path.read_text("utf-8"))
            for name in set(re.findall(r"var\.([a-z0-9_]+)", body)):
                with self.subTest(path=path.name, variable=name):
                    self.assertIn(name, declared)

    def test_every_declared_variable_is_used(self) -> None:
        body = "".join(self._decomment(p.read_text("utf-8")) for p in terraform_files())
        declared = re.findall(
            r'variable\s+"([a-z0-9_]+)"', (INFRA / "variables.tf").read_text("utf-8")
        )
        for name in declared:
            with self.subTest(variable=name):
                self.assertIn(f"var.{name}", body, f"{name} is declared and never used")

    def test_every_variable_is_described_and_typed(self) -> None:
        text = (INFRA / "variables.tf").read_text("utf-8")
        blocks = re.findall(
            r'variable\s+"([a-z0-9_]+)"\s*\{(.*?)\n\}', text, flags=re.DOTALL
        )
        self.assertGreater(len(blocks), 20)
        for name, body in blocks:
            with self.subTest(variable=name):
                self.assertIn("description", body)
                self.assertIn("type", body)

    def test_no_service_account_keys_are_created(self) -> None:
        body = "".join(p.read_text("utf-8") for p in terraform_files())
        self.assertNotIn("google_service_account_key", body)

    def test_required_labels_are_defined_once(self) -> None:
        locals_text = (INFRA / "locals.tf").read_text("utf-8")
        for label in ("project", "component", "environment"):
            self.assertRegex(locals_text, rf"{label}\s*=")

    def test_every_labelled_resource_uses_the_shared_labels(self) -> None:
        # A label applied by hand in one file is a label that will disagree
        # with the others later. locals.tf is where the set is defined, so it
        # is the one file that does not refer to it.
        for path in terraform_files():
            if path.name == "locals.tf":
                continue
            text = path.read_text("utf-8")
            for match in re.finditer(
                r"^\s*labels\s*=\s*(.+)$", text, flags=re.MULTILINE
            ):
                with self.subTest(path=path.name):
                    self.assertIn("local.labels", match.group(1))

    def test_apis_are_not_disabled_on_destroy(self) -> None:
        # Tearing down this module must not disable an API another component
        # in the project depends on.
        text = (INFRA / "apis.tf").read_text("utf-8")
        self.assertRegex(text, r"disable_on_destroy\s*=\s*false")


class SchemaContractTests(unittest.TestCase):
    """The adapter and the tables must agree, column for column."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.schemas = {
            path.stem: json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(SCHEMAS.glob("*.json"))
        }
        cls._temp = tempfile.TemporaryDirectory()
        root = Path(cls._temp.name)
        cls.run_dir = runs.load(runfixture.build(root))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp.cleanup()

    def columns(self, table: str) -> dict[str, dict]:
        return {column["name"]: column for column in self.schemas[table]}

    def test_every_schema_is_well_formed(self) -> None:
        for table, schema in self.schemas.items():
            with self.subTest(table=table):
                self.assertIsInstance(schema, list)
                for column in schema:
                    self.assertIn(column["mode"], ("REQUIRED", "NULLABLE"))
                    self.assertIn(
                        column["type"],
                        ("STRING", "INTEGER", "FLOAT", "BOOLEAN", "TIMESTAMP"),
                    )
                    # A column without a description is a column whose meaning
                    # lives in somebody's memory.
                    self.assertGreater(len(column["description"]), 40, column["name"])

    def test_run_row_matches_the_runs_table(self) -> None:
        row = records.run_row(self.run_dir, "gs://bucket/runs/x")
        self.assertEqual(set(row), set(self.columns("runs")))

    def test_measurement_rows_match_their_table(self) -> None:
        rows = records.measurement_rows(
            self.run_dir, self.run_dir.measurement_documents()
        )
        self.assertEqual(set(rows[0]), set(self.columns("scale_measurements")))

    def test_subspace_rows_match_their_table(self) -> None:
        rows = records.subspace_rows(self.run_dir, self.run_dir.subspace_documents())
        self.assertEqual(set(rows[0]), set(self.columns("subspace_summaries")))

    def test_required_columns_are_never_null_in_a_well_formed_run(self) -> None:
        cases = (
            ("runs", [records.run_row(self.run_dir, "gs://bucket/runs/x")]),
            (
                "scale_measurements",
                records.measurement_rows(
                    self.run_dir, self.run_dir.measurement_documents()
                ),
            ),
            (
                "subspace_summaries",
                records.subspace_rows(self.run_dir, self.run_dir.subspace_documents()),
            ),
        )
        for table, rows in cases:
            required = [
                n for n, c in self.columns(table).items() if c["mode"] == "REQUIRED"
            ]
            for row in rows:
                for name in required:
                    with self.subTest(table=table, column=name):
                        self.assertIsNotNone(row[name])

    def test_merge_keys_are_required_columns(self) -> None:
        # A nullable key column would make the merge non-deterministic, since
        # NULL does not equal NULL.
        for table, key in (
            ("scale_measurements", records.MEASUREMENT_KEY),
            ("subspace_summaries", records.SUBSPACE_KEY),
            ("runs", ("run_id",)),
        ):
            columns = self.columns(table)
            for name in key:
                with self.subTest(table=table, column=name):
                    self.assertEqual(columns[name]["mode"], "REQUIRED")

    def test_tables_in_terraform_match_the_schema_files(self) -> None:
        text = (INFRA / "bigquery.tf").read_text("utf-8")
        for table in self.schemas:
            with self.subTest(table=table):
                self.assertRegex(text, rf'table_id\s*=\s*"{table}"')
                self.assertIn(f"bigquery/{table}.json", text)

    def test_the_partitioning_column_exists_in_each_partitioned_table(self) -> None:
        for table in ("scale_measurements", "subspace_summaries"):
            self.assertIn("layer", self.columns(table))
        self.assertEqual(self.columns("runs")["created_at"]["type"], "TIMESTAMP")


class DeployWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = DEPLOY.read_text(encoding="utf-8")
        cls.document = yaml.safe_load(cls.text)

    def test_the_workflow_parses(self) -> None:
        self.assertIsInstance(self.document, dict)
        self.assertIn("jobs", self.document)

    def test_it_runs_only_when_asked(self) -> None:
        # `on` is parsed as the boolean True by YAML 1.1, which is a trap worth
        # a test of its own rather than a surprise in a later edit.
        triggers = self.document.get("on", self.document.get(True))
        self.assertIsInstance(triggers, dict)
        self.assertIn("workflow_dispatch", triggers)
        self.assertNotIn("push", triggers)
        self.assertNotIn("pull_request", triggers)

    def test_federation_not_a_key(self) -> None:
        self.assertIn("workload_identity_provider", self.text)
        self.assertNotIn("credentials_json", self.text)
        self.assertNotIn("service-account-key", self.text)

    def test_the_id_token_permission_is_requested_once_and_narrowly(self) -> None:
        permissions = []
        for job in self.document["jobs"].values():
            if "permissions" in job:
                permissions.append(job["permissions"])
        self.assertTrue(permissions)
        for permission in permissions:
            self.assertEqual(permission.get("id-token"), "write")
            self.assertEqual(permission.get("contents"), "read")

    def test_every_action_is_pinned_to_an_immutable_commit(self) -> None:
        for match in re.finditer(r"uses:\s*(\S+)", self.text):
            reference = match.group(1)
            with self.subTest(action=reference):
                self.assertRegex(reference, r"@[0-9a-f]{40}$")

    def test_retries_default_to_zero_until_resume_is_durable(self) -> None:
        variables = (INFRA / "variables.tf").read_text(encoding="utf-8")
        block = re.search(
            r'variable "job_max_retries"\s*\{(.*?)\n\}',
            variables,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(block)
        self.assertRegex(block.group(1), r"default\s*=\s*0")

    def test_registry_cleanup_only_deletes_untagged_images(self) -> None:
        registry = (INFRA / "artifact_registry.tf").read_text(encoding="utf-8")
        self.assertRegex(registry, r'tag_state\s*=\s*"UNTAGGED"')

    def test_terraform_ignores_only_the_fields_the_deployment_owns(self) -> None:
        # Ignoring `env` would hide every environment change from `apply`, and
        # indexing into it is invalid because the provider models it as a set.
        # The deployment owns the image and the arguments; nothing else.
        cloud_run = (INFRA / "cloud_run.tf").read_text(encoding="utf-8")
        block = re.search(r"ignore_changes\s*=\s*\[(.*?)\]\s*\n", cloud_run, re.DOTALL)
        self.assertIsNotNone(block)
        ignored = {
            line.strip().rstrip(",")
            for line in block.group(1).splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        container = "template[0].template[0].containers[0]"
        self.assertEqual(ignored, {f"{container}.image", f"{container}.args"})

    def test_the_digest_reaches_the_job_as_an_argument(self) -> None:
        self.assertIn(
            '--args="--container-digest=${{ steps.digest.outputs.digest }}"',
            self.text,
        )
        self.assertNotIn("--update-env-vars", self.text)
        self.assertNotIn("SG_CONTAINER_DIGEST", self.text)
        entrypoint = (REPO / "cloud" / "entrypoint.sh").read_text(encoding="utf-8")
        self.assertIn("--container-digest=*)", entrypoint)
        self.assertIn("export SG_CONTAINER_DIGEST", entrypoint)

    def test_the_image_tag_is_the_commit_sha(self) -> None:
        # Either spelling of the commit SHA is fine; a floating tag is not,
        # because a job deployed from one could not be traced to a source.
        self.assertRegex(self.text, r"\$\{GITHUB_SHA\}|github\.sha")
        self.assertNotIn(":latest", self.text)
        self.assertIn("IMAGE_PATH}:${GITHUB_SHA}", self.text)

    def test_the_job_is_deployed_by_digest(self) -> None:
        self.assertIn("IMAGE_PATH}@${{ steps.digest.outputs.digest }}", self.text)

    def test_the_workflow_starts_no_execution(self) -> None:
        # Building and deploying are free; executing is not. Nothing in this
        # workflow may cross that line.
        self.assertNotIn("run jobs execute", self.text)


if __name__ == "__main__":
    unittest.main()
