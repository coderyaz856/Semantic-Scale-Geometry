# Cloud deployment

This document describes the cloud platform for the semantic scale geometry
measurement pipeline: what it creates, what it costs, what it is allowed to do,
and the single command that turns any of it on.

Nothing in this repository has been applied. No resource exists, no image has
been pushed, no job has run, and no credential has been created. Every command
that would change that is listed under [Commands that create billable
resources](#commands-that-create-billable-resources), and nowhere else.

## Contents

- [Architecture](#architecture)
- [The interface this is built against](#the-interface-this-is-built-against)
- [Terraform](#terraform)
- [BigQuery schema decisions](#bigquery-schema-decisions)
- [Identity and permissions](#identity-and-permissions)
- [The cloud adapter](#the-cloud-adapter)
- [Containers](#containers)
- [The deployment workflow](#the-deployment-workflow)
- [Cost controls](#cost-controls)
- [Validation](#validation)
- [Commands that create billable resources](#commands-that-create-billable-resources)
- [Teardown](#teardown)
- [Assumptions to reconcile](#assumptions-to-reconcile)

## Architecture

```text
  experiment configuration (gs://.../experiment.yaml)
            |
            v
  Cloud Run Job  (one task, no parallelism, hard timeout)
    cloud/entrypoint.sh
      1. sg_cloud.fetch      configuration -> /config/experiment.yaml
      2. sg run              --config /config/experiment.yaml --run-root /work/runs
      3. sg_cloud.publish    run directory -> bucket, then rows -> BigQuery
      3'. sg_cloud.salvage   on failure: logs -> failures/ prefix
            |
            +--> Cloud Storage   gs://<bucket>/runs/<run-id>/...   (immutable prefix)
            |
            +--> BigQuery        runs, scale_measurements, subspace_summaries
```

Three deliberate properties:

**The bucket holds the evidence, BigQuery holds the index.** Embeddings,
exported directions and rendered reports are files; a ledger that also stored
them would be a second copy of the thing it indexes, billed per byte scanned.
What goes into BigQuery is one row per run, one row per measured cell and one
row per group of directions.

**The run directory is the unit, and its prefix is immutable.** A run id is
derived from the normalized configuration and the input identities, so two
runs with one id would be a contradiction rather than an update. The adapter
checks every object before writing it and refuses on a content mismatch, and
the runtime identity has no delete permission, so the same rule holds even if
the adapter is wrong.

**A job, not a service.** The workload is a batch measurement with a beginning
and an end. A Cloud Run Job holds no idle capacity between executions, so
there is no minimum instance count to remember to set to zero.

## The interface this is built against

The runtime is being implemented on a separate branch. This platform targets
the frozen contract from `docs/production-roadmap.md`:

```text
sg run --config /config/experiment.yaml --run-root /work/runs
```

Exit code `0`, and:

```text
runs/<run-id>/
  config.yaml  manifest.json  status.json  logs.jsonl
  shards/  directions/  measurements/  report/
```

The adapter reads `status.json` and `manifest.json` for provenance, and treats
the result documents by shape rather than by filename: an object with a
`results` array is a measurement document, a list of groups each carrying an
`observed` object is a subspace analysis. The exact key spellings it accepts
are listed in one place, `STATUS_FIELDS` and `MANIFEST_FIELDS` in
`cloud/sg_cloud/runs.py`, and a field no source supplies is published as null
rather than inferred. See [Assumptions to reconcile](#assumptions-to-reconcile).

## Terraform

Everything lives in `infra/`, as a single root module with one file per
concern. There is no backend block: state stays local and uncommitted, because
a shared backend is itself a resource with a cost and a lifecycle, and it
should be a deliberate second step rather than a side effect of a first apply.

| file | contents |
| --- | --- |
| `versions.tf` | Terraform and provider constraints, provider default labels |
| `variables.tf` | every input; only `project_id` has no default |
| `locals.tf` | the three required labels, generated names |
| `apis.tf` | the six APIs this platform needs |
| `artifact_registry.tf` | Docker repository, immutable tags, cleanup policy |
| `storage.tf` | the private artifact bucket and its lifecycle rules |
| `bigquery.tf` | dataset and the three ledger tables |
| `bigquery/*.json` | table schemas, one description per column |
| `iam.tf` | the runtime service account and its narrow grants |
| `cloud_run.tf` | the job, its limits and its volumes |
| `outputs.tf` | names, and the one command that starts an execution |

Resource labels `project`, `component` and `environment` are set once in
`locals.tf` and applied through the provider's `default_labels` as well as
explicitly on the resources that support them.

Lifecycle rules on the bucket:

| rule | why |
| --- | --- |
| delete `*.f32` after 30 days | Shard and direction payloads regenerate from a pinned model, a pinned corpus and a seed. Their JSON sidecars are kept, so a deleted payload still leaves a record of what it held. |
| move to Nearline after 30 days | Reports and manifests are read rarely after a run is written up. |
| delete `runs/` after N days | Disabled by default. A rule that silently deletes the evidence behind a published number is worse than a storage bill. |
| abort incomplete uploads after 1 day | Interrupted resumable uploads are billed and do not appear in an object listing. |

Soft delete is disabled: soft-deleted objects are billed as storage, and
nothing in this platform is permitted to delete an object.

## BigQuery schema decisions

Three tables, with the columns and descriptions in `infra/bigquery/`. Every
partitioning and clustering choice below is there for a reason, and the reason
is usually the merge rather than the query.

### `runs`

- **Partitioned by `DATE(created_at)`.** A run happens at a time, and a ledger
  is queried by time: the last week, the last month. The column is `REQUIRED`
  in the schema and the adapter refuses to publish a run without it, because a
  null partition column would send every such row to one partition.
- **Clustered by `state`, `model_id`.** Both are low cardinality, and the
  common question is "which runs succeeded for this model". Clustering order
  follows that filter order.
- **Not requiring a partition filter.** The table holds one row per run. Even
  a full scan is a rounding error, and the inconvenience would be permanent.

### `scale_measurements`

- **Integer-range partitioned by `layer`**, interval 1, ceiling configurable
  (default 64. This is the choice worth explaining. There is no timestamp in
  the required schema, so the alternatives were ingestion-time partitioning or
  a column. `layer` wins on both axes that matter: it is the analysis axis, so
  nearly every query filters or groups by it; and it is part of the merge key,
  so an idempotent reload prunes to the partitions it rewrites instead of
  scanning the table. Ingestion-time partitioning would have bought
  retention-by-date, which this table does not need, since a row is a few
  dozen bytes and the payloads it describes have their own lifecycle rule.
  A model deeper than the ceiling still loads, but its deepest layers fall
  outside the declared partitions and stop being pruned.
- **Clustered by `run_id`, `condition`, `scale_id`.** `run_id` first because
  publishing, verifying and auditing are all per-run; `condition` next because
  directions are only comparable within one condition, so it is the next
  filter in almost every real query; `scale_id` last for the per-scale
  comparisons.
- **`require_partition_filter` defaults to false.** Turning it on is a real
  cost control and has a real consequence: the adapter's MERGE carries no
  constant predicate on `layer`, so enabling it means changing
  `cloud/sg_cloud/ledger.py` too. The variable documents that.

### `subspace_summaries`

- **Partitioned by `layer`** for the same reasons.
- **Clustered by `run_id`, `condition`, `remove_top`.** No `scale_id`: a row
  here summarises a whole group of directions rather than one scale.
- `dimension_gap` is stored as `matched_null_dimensions - effective_dimensions`
  and computed by the adapter from its own operands, because it is the number
  the result is usually quoted as and a stored difference must not be able to
  disagree with the values it is a difference of.

### Two columns that are not simple renames

**`significant`.** The measurement files contain p-values; the schema asks for
a decision. The adapter applies Benjamini-Hochberg over one family — the
scales sharing a run, model, layer, condition and `remove_top` — which is the
same family the repository's own report uses. Two warnings travel with it. If
the layer or the preprocessing setting was chosen after looking at results,
the real family is larger and the survivors are optimistic. And a scale that
does not survive has not been shown to lack a gradient: with six items the
test has little power, and the exact permutation p-value has a floor of 1/k!,
which for a five-item scale is 0.0083 however perfectly it is ordered. The
authoritative figure is the report in the run directory. If a measurement
document ever carries its own `significant` field, that field wins and the
adapter stops deciding.

**`context_stability`.** This is the standard deviation of the leave-one-out
statistic across carrier contexts: dispersion, not a score. Lower is more
stable, which is the opposite of what the column name suggests, so the
direction is stated in the column description as well as here. It is null for
single-context conditions and when bootstrapping was disabled.

## Identity and permissions

One service account, created by Terraform, with no key. Keys are not created
by this repository, and the workflow does not download one either.

| grant | scope | why this and not more |
| --- | --- | --- |
| `roles/storage.objectCreator` | the bucket | Create objects. |
| `roles/storage.objectViewer` | the bucket | Read them back, which is what the pre-upload check needs. Together with the absence of a delete permission, this makes the per-run prefix immutable at the IAM layer rather than by convention. `objectAdmin` would have been one role instead of two and would have allowed overwriting. |
| `roles/bigquery.dataEditor` | the dataset | Create the staging table, load it, merge, drop it. Scoped to the one dataset. |
| `roles/bigquery.jobUser` | the project | Load and query jobs are project-level operations; there is no dataset-scoped equivalent. This role can start jobs; it cannot read data by itself. |
| `roles/logging.logWriter` | the project | Structured logs from the task. |
| `roles/artifactregistry.reader` | the one repository | Pull the job image. |

The deploying identity used by the workflow is **not** created here. It needs
`roles/artifactregistry.writer` on the repository, `roles/run.developer` on the
job, and `roles/iam.serviceAccountUser` on the runtime service account. It does
not need to execute the job, so it should not have `roles/run.invoker`.

Workload Identity Federation is also not created here, deliberately: a pool
and a provider grant deploy-time authority to an external system, which
deserves its own review rather than arriving inside an unrelated apply. The
commands are in [Commands that create billable
resources](#commands-that-create-billable-resources) so they are reviewed
before they are run.

## The cloud adapter

`cloud/sg_cloud/`, with its own dependencies in `cloud/requirements.txt`. The
project's `pyproject.toml` is untouched: the measurement runtime stays
installable and testable without a cloud SDK, and the adapter stays
installable without torch.

| module | responsibility |
| --- | --- |
| `runs.py` | read and validate a run directory; find the run under a run root |
| `records.py` | build rows; the Benjamini-Hochberg decision |
| `storage.py` | upload under an immutable prefix, verifying before writing |
| `ledger.py` | staging load plus MERGE; SQL identifier validation |
| `publish.py` | orchestration and the `sg-publish` command |
| `fetch.py` | download the experiment configuration |
| `salvage.py` | keep the logs of a run that failed |
| `logs.py`, `errors.py` | structured logging; exit codes |

Idempotence has two halves. Objects are compared by size and MD5 before being
written: identical ones are skipped, a mismatch is a hard error. Rows are
loaded into a per-run staging table and merged on the natural key, so a retry
converges instead of duplicating. Running `sg-publish` twice on one run
directory uploads nothing and changes no row.

Merge keys:

| table | key |
| --- | --- |
| `runs` | `run_id` |
| `scale_measurements` | `run_id`, `model_id`, `layer`, `scale_id`, `condition`, `remove_top` |
| `subspace_summaries` | `run_id`, `model_id`, `layer`, `condition`, `remove_top` |

Rows sharing a key are refused rather than deduplicated: two documents
covering one layer is a run worth looking at, and a merge would silently keep
whichever row was written last.

Exit codes, from `cloud/sg_cloud/errors.py`:

| code | meaning | retry? |
| --- | --- | --- |
| 0 | published | — |
| 2 | unusable arguments | no |
| 3 | malformed run directory | no |
| 4 | run did not succeed | no |
| 5 | an object exists with different content | no: two runs claimed one id |
| 6 | upload failed | yes |
| 7 | BigQuery load or merge failed | yes |
| 8 | the merge reported success and the table disagreed | no: look |

Logs are one JSON object per line on stderr, with `severity` and `message`
named as Cloud Logging expects. The single line on stdout is the machine
readable result, so a wrapper never parses logs.

A local dry run reads everything, builds every row and contacts nothing:

```sh
PYTHONPATH=cloud python -m sg_cloud.publish <path-to-run-dir> --dry-run
```

## Containers

The root `Dockerfile` is the local measurement image and is left alone.
`docker/` adds two:

**`docker/job.Dockerfile`** is what the Cloud Run Job runs. Four stages' worth
of separation: the Rust kernel is built and tested in a builder stage; the
adapter's dependencies are resolved in their own stage and installed into
their own virtual environment, so a client library cannot conflict with the
runtime's dependency set; the final image copies both results, runs as uid
10001, and owns nothing it executes, since `/app` is stripped of write
permission. `/work` and `/config` are the only writable paths, and in the job
both are memory-backed volumes.

**`docker/adapter.Dockerfile`** is the adapter alone, for publishing a run
produced elsewhere. It exists because the two jobs have different dependency
sets and different trust requirements: the identity that publishes never needs
the identity that measures.

Both validate at build time. The job image invokes every entry point the
wrapper will call and imports both client libraries; the adapter image also
runs the test suite, which it can because the suite needs no credentials. A
broken image then fails the build rather than failing one task at a time in
the cloud.

Base images are pinned by version tag, not by digest. A digest that has not
been observed is a fiction, and nothing here has been built yet. The
deployment workflow records the digest of what it actually pushed, and sets
`SG_CONTAINER_DIGEST` on the job so a run manifest can name the exact image
that produced it.

Build context is the repository root. BuildKit reads
`docker/<name>.Dockerfile.dockerignore`, which is how a local working tree of
compiled artifacts and virtual environments is excluded without adding a file
to the repository root.

## The deployment workflow

`.github/workflows/deploy.yml`, manual only (`workflow_dispatch`), separate
from `ci.yml`. It authenticates through Workload Identity Federation,
requests `id-token: write` and `contents: read` and nothing else, and no
repository secret is involved — which is worth saying, because the usual shape
of this workflow keeps a service-account key in one.

Required repository **variables**:

| variable | value |
| --- | --- |
| `GCP_PROJECT_ID` | target project |
| `GCP_REGION` | region of the registry and the job |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | `projects/<number>/locations/global/workloadIdentityPools/<pool>/providers/<provider>` |
| `GCP_DEPLOY_SERVICE_ACCOUNT` | the account the workflow impersonates |
| `GCP_ARTIFACT_REPOSITORY` | Artifact Registry repository id |
| `GCP_JOB_NAME` | Cloud Run Job name |

Required repository **secrets**: none.

The workflow checks those variables before building, since failing after a
push leaves a tagged image nothing points at. Images are tagged with the
commit SHA and tags are immutable in the registry, so a repeated deployment of
one commit finds the existing image and reuses it instead of failing on a
push. The job is updated **by digest**, so it runs the exact bytes that were
published. It does not start an execution; that is a separate, billable step.

## Cost controls

The project has a $10 monthly Cloud Run spend cap, and BigQuery is outside it.
One thing to be clear about: a budget is an alert, not a brake. Nothing stops
spending when a threshold is crossed. So the controls that matter are the ones
expressed as resource attributes and query parameters.

### Cloud Run

| control | where | default |
| --- | --- | --- |
| task timeout | `job_task_timeout` | 1800s |
| retries | `job_max_retries` | 1 |
| CPU limit | `job_cpu` | 1 |
| memory limit | `job_memory` | 2Gi |
| tasks, parallelism | `cloud_run.tf` | 1, 1 |
| minimum instances | not applicable | a job holds no idle capacity |

The timeout is the main defence: a hung run cannot bill for longer than one
task timeout per retry. One retry rather than three, because this pipeline's
likeliest failure is deterministic, and retrying a deterministic failure just
multiplies its cost.

### BigQuery

| control | where | default |
| --- | --- | --- |
| `maximum_bytes_billed` on every adapter query | `--max-bytes-billed`, `BQ_MAX_BYTES_BILLED`, `query_bytes_billed_limit` | 1 GiB |
| partition pruning | `layer` range partitioning | — |
| cluster pruning | `run_id` first | — |
| free loads | staging tables are filled by load jobs | — |

Load jobs are free; the MERGE and the verification count are billed by bytes
scanned, which partitioning and clustering keep small. Project and per-user
query quotas are **not** Terraform resources and are not set here. Set them
once, deliberately, in the console or with the quota API: a per-user and a
per-project daily query usage limit. Until they are set, `maximum_bytes_billed`
is the only ceiling, and it is per query rather than per day.

### Storage

Lifecycle rules are in the table above. The one cost that survives every job
finishing is storage: objects in the bucket and image versions in the
registry. Hence the `.f32` expiry and the registry cleanup policy that keeps
the most recent versions and deletes the rest.

### What keeps accruing cost after a job stops

- **Cloud Storage**: every object, until a lifecycle rule or a human removes
  it. This is the main one.
- **Artifact Registry**: every image version, bounded by the cleanup policy.
- **BigQuery storage**: the ledger tables. Small, but not zero.
- **BigQuery queries**: whatever anyone runs afterwards, which is precisely
  why `maximum_bytes_billed` is set in the adapter and why project quotas are
  worth setting by hand.
- **Logs**: retained by Cloud Logging's own retention, which this module does
  not configure.

Not accruing: the Cloud Run Job itself, which bills only while a task runs,
and the service account, the dataset definition and the APIs, which are free.

### The first workload

Keep the first smoke run deliberately tiny: one model, one or two layers, the
`synth` path or a single small shard, and a configuration whose extraction
fits inside the task timeout with room to spare. The purpose of the first run
is to prove the path end to end and to produce a real cost figure. Until that
figure exists, every number in this document about what a run costs would be
arithmetic rather than a measurement, so there are none.

## Validation

What ran in the environment this was written in:

| check | result |
| --- | --- |
| `python -m unittest discover -s cloud/tests` | pass |
| BigQuery schema against the adapter's rows, column for column | pass |
| `deploy.yml` parsed as YAML, triggers and permissions asserted | pass |
| scan for project ids, addresses, keys and local paths | pass |
| Terraform brace balance, declared-against-used variables, descriptions | pass |
| `git diff --check` | pass |

What could **not** run, because the tool is not installed here:

| check | command to run before applying |
| --- | --- |
| Terraform formatting | `terraform -chdir=infra fmt -check` |
| Terraform initialisation | `terraform -chdir=infra init -backend=false` |
| Terraform validation | `terraform -chdir=infra validate` |
| Dockerfile linting | `hadolint docker/job.Dockerfile docker/adapter.Dockerfile` |
| Image build | `docker build -f docker/job.Dockerfile .` |

The static checks in `cloud/tests/test_platform_static.py` are not a
substitute for `terraform validate`: they cannot tell whether a provider
attribute exists. In particular the provider is constrained to `~> 6.0`
because the job uses v2 attributes — memory-backed `empty_dir` volumes and
`deletion_protection` — that are not in every 5.x provider. `init` followed by
`validate` is the first command to run, and the first that can prove it.

## Commands that create billable resources

Everything above this line is inert. Each command below is not.

```sh
# 1. Enable the APIs and create the resources. Free in itself, except that
#    some of what it creates can hold data. Review the plan first.
terraform -chdir=infra init
terraform -chdir=infra plan  -var-file=terraform.tfvars
terraform -chdir=infra apply -var-file=terraform.tfvars

# 2. Workload Identity Federation for the workflow. Deliberately not in
#    Terraform: this grants an external system the right to deploy.
gcloud iam workload-identity-pools create github \
  --location=global --project=<project>
gcloud iam workload-identity-pools providers create-oidc github \
  --location=global --workload-identity-pool=github \
  --issuer-uri=https://token.actions.githubusercontent.com \
  --attribute-mapping=google.subject=assertion.sub,attribute.repository=assertion.repository \
  --attribute-condition='assertion.repository=="<owner>/<repo>"' \
  --project=<project>
# Bind the deploying service account to that pool, restricted to one
# repository. An unrestricted binding lets any repository deploy.

# 3. Build and push an image. Billable: registry storage.
docker build -f docker/job.Dockerfile -t <region>-docker.pkg.dev/<project>/<repo>/measure:<sha> .
docker push <region>-docker.pkg.dev/<project>/<repo>/measure:<sha>

# 4. Run the job. Billable: CPU, memory, egress, and whatever it writes.
gcloud run jobs execute <job-name> --region <region> --project <project> --wait

# 5. Publish a run from a workstation instead. Billable: storage and BigQuery.
PYTHONPATH=cloud python -m sg_cloud.publish <run-dir> \
  --bucket <bucket> --bq-project <project> --bq-dataset <dataset>
```

`terraform -chdir=infra output execute_command` prints command 4 with the real
names filled in, so it is never reconstructed from memory.

## Teardown

```sh
# Objects first: the bucket will not delete while it holds any, and
# force_destroy is deliberately false so that this is a conscious act.
gcloud storage rm -r gs://<bucket>/runs gs://<bucket>/failures

# Ledger rows, if the dataset should go. Deletion protection is on by default,
# so these two flags are required rather than available.
terraform -chdir=infra apply \
  -var-file=terraform.tfvars \
  -var dataset_deletion_protection=false \
  -var table_deletion_protection=false

terraform -chdir=infra destroy -var-file=terraform.tfvars

# Images, which Terraform removes with the repository, and APIs, which it
# deliberately does not disable.
gcloud services disable run.googleapis.com --project <project>  # optional
```

`destroy` does not disable the APIs: another component in the project may
depend on one, so disabling is a separate, deliberate act.

## Assumptions to reconcile

These are the places where this platform had to assume something about a
runtime that does not exist yet. Each is a one-file change, and each is
covered by a test that will fail if the assumption turns out to be wrong.

1. **`sg run` is the entry point and the subcommand exists.** The job image
   provides `sg` as a shim onto the existing module, because the console
   script only exists after an installation. When the runtime branch merges
   and the package is installed properly, the shim becomes redundant rather
   than wrong. The build-time validation deliberately does not invoke
   `sg run`, since asserting it today would assert a fiction.
2. **`status.json` carries `run_id`, `state`, `created_at`, `completed_at`,
   `error_code`.** `created_at` is required: it is the partitioning column of
   `runs`. The accepted spellings are in `STATUS_FIELDS`.
3. **`manifest.json` carries the provenance fields**, flat or nested one level
   (`model.id`, `inputs.scales_digest`). Accepted spellings are in
   `MANIFEST_FIELDS`; anything absent is published as null and logged.
4. **Result documents are classified by shape**, in `measurements/` and
   `report/`. If the runtime puts the subspace analysis elsewhere, add the
   directory to `RESULT_DIRECTORIES`.
5. **The run directory is the only state.** The wrapper locates it by looking
   for the single directory under the run root that holds a `status.json`, and
   refuses to guess between two. If `sg run` prints its run id, the wrapper
   should read that instead.
6. **`duration_seconds` comes from `status.json` or the manifest.** Whichever
   the runtime fills; if neither, the column is null rather than computed from
   timestamps that may mean different things.
7. **`significant` is decided by the adapter** until the runtime emits it. See
   the schema section above for the family definition and its two warnings.
