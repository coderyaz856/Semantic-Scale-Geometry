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
- [Runtime interface](#runtime-interface)
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
- [Integrated contract decisions](#integrated-contract-decisions)

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

## Runtime interface

The platform and runtime are validated together against this command:

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
rather than inferred. A cross-contract test builds a manifest with the runtime
module and verifies the BigQuery provenance row produced by the adapter.

The configuration is fetched to `/config`, so repository inputs and tools must
use their absolute paths inside the image. A cloud configuration therefore uses
paths such as:

```yaml
scales: /app/data/scales/scales.yaml
background: /app/data/background/wikitext2.txt
model:
  device: cpu
execution:
  kernel_binary: sg-measure
  subspace_binary: sg-subspace
  report_script: /app/tools/report.py
```

The model identifier, exact 40-character model revision, and remaining
analysis fields follow [`docs/runtime.md`](runtime.md). Relative paths remain
appropriate for local configurations stored beside their inputs.

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
a decision. The adapter applies Benjamini-Hochberg over one family: the scales
sharing a run, model, layer and `remove_top` within one condition **group**.
Carrier and utterance conditions form the contextual group, every scale once,
which is the family `tools/report.py` corrects over; bare items are corrected
as a group of their own. The unit is the group rather than the single
condition, and the difference is not cosmetic. Correcting the three utterance
scales among themselves would make their thresholds seven times more lenient,
and on the saved results that per-condition definition disagrees with the
report in 18 of 21 layer and setting cells.
`python/tests/test_cloud_contract.py` holds the two decisions equal on every
saved layer.

Two warnings travel with the decision. If the layer or the preprocessing
setting was chosen after looking at results, the real family is larger and the
survivors are optimistic. And a scale that does not survive has not been shown
to lack a gradient. The exact permutation p-value has a floor of 1/k!, which
is 0.0083 for the two five-item scales and 0.0014 for the six-item ones,
however perfectly a scale is ordered. The inference model is set out in
`docs/statistical-inference.md`. The authoritative figure is the report in the
run directory. If a measurement
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

The runtime is installed as a package, so `sg` is its real console script and
`sg run` performs extraction in the job. That makes torch a dependency of the
image. pip's default Linux torch wheel is the CUDA build, which brings several
gigabytes of GPU libraries to a job that runs on CPU, so the CPU build is
installed from the PyTorch CPU index first and `pip install .` finds it
satisfied. Image size matters more than usual here, because every deployed
image is kept (see [Storage](#storage)).

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
deployment workflow records the digest of what it actually pushed and passes
it to the job as the container argument `--container-digest=sha256:...`. The
wrapper checks its form and exports it as `SG_CONTAINER_DIGEST`, which the
runtime writes into the run manifest, so a run can name the exact image that
produced it.

Why an argument and not an environment variable: Terraform manages the job's
environment, and the provider models `env` as a set, so one variable cannot be
excluded from Terraform's management without excluding all of them. If all of
them were excluded, a changed bucket or dataset would never reach the job, and
`apply` would still report success. So Terraform ignores exactly two fields,
the image and the arguments, both owned by the deployment.

One consequence: `gcloud run jobs execute --args=...` replaces the arguments
for that execution, and the manifest of that run then records no digest. It
records null, not a wrong value.

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
| retries | `job_max_retries` | 0 |
| CPU limit | `job_cpu` | 1 |
| memory limit | `job_memory` | 2Gi |
| tasks, parallelism | `cloud_run.tf` | 1, 1 |
| minimum instances | not applicable | a job holds no idle capacity |

The timeout is the main defence: a hung run cannot bill for longer than one
task timeout. Retries default to zero because `/work` is memory-backed. A new
attempt cannot resume a completed extraction or measurement stage, so it would
repeat the full experiment and its cost. Retries should be enabled only after
the runtime can restore completed stages from durable storage.

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
registry. Hence the `.f32` expiry in the bucket.

The registry behaves differently, and it is worth being exact about. Under
immutable tags a tagged version cannot be deleted, by a person or by a cleanup
policy, and a tag cannot be removed. The cleanup policy therefore reaches
untagged versions only, such as layers left by an interrupted push. Every
deployed commit's image is kept. That is the price of a commit SHA naming one
image forever. It also means automatic cleanup can never delete the image the
job references.

To prune by hand, switch immutability off, delete, and switch it back on:

```sh
terraform -chdir=infra apply -var-file=terraform.tfvars -var immutable_image_tags=false
gcloud artifacts docker images list <region>-docker.pkg.dev/<project>/<repo>/measure --include-tags
gcloud artifacts docker images delete <region>-docker.pkg.dev/<project>/<repo>/measure@<old-digest> --delete-tags
terraform -chdir=infra apply -var-file=terraform.tfvars
```

Never delete the digest the job currently references:
`gcloud run jobs describe <job> --region <region> --format='value(spec.template.spec.template.spec.containers[0].image)'`
prints it.

### What keeps accruing cost after a job stops

- **Cloud Storage**: every object, until a lifecycle rule or a human removes
  it. This is the main one.
- **Artifact Registry**: every deployed image version, until pruned by hand
  as described under [Storage](#storage). Growth is one image per deployed
  commit.
- **BigQuery storage**: the ledger tables. Small, but not zero.
- **BigQuery queries**: whatever anyone runs afterwards, which is precisely
  why `maximum_bytes_billed` is set in the adapter and why project quotas are
  worth setting by hand.
- **Logs**: retained by Cloud Logging's own retention, which this module does
  not configure.

Not accruing: the Cloud Run Job itself, which bills only while a task runs,
and the service account, the dataset definition and the APIs, which are free.

### The first execution

The first execution proved the path end to end: configuration fetched from the
bucket, model downloaded at a pinned revision, every runtime stage, the run
directory uploaded and the ledger loaded. It ran on 19 October 2026 from commit
`baef7a9`, image digest
`sha256:5dbc9999f40b35144f6e9d784626a1943a5de67e1acb3805bcef662040d004e8`,
with `sentence-transformers/all-MiniLM-L6-v2` at revision
`1110a243fdf4706b3f48f1d95db1a4f5529b4d41`, on 1 vCPU and 2 GiB. The
configuration kept one preprocessing setting (`removals: [0]`) and reduced the
random, bootstrap and null draws to 1,000, 200 and 20.

| quantity | value |
| --- | --- |
| run id | `bdd317c9ca8322e9426d` |
| runtime stages, from the run status | 135 s |
| whole execution, including model download and publishing | 3 min 30 s |
| `scale_measurements` rows | 287: 41 per layer, 7 layers |
| `subspace_summaries` rows | 7 |
| significant after correction | 5, all at layer 6 |

The significance decisions reproduce those computed from the saved results in
`results/` at the same setting: the same five scales (beauty, brightness,
loudness, size and valence, carrier condition, layer 6) and none at layers 0 to
5. The exact permutation p-values do not depend on the reduced draws, which is
why the decisions can agree although the configurations differ.

Cost figures will be added from the billing report rather than estimated. Every
command behind this record, with its output, is in
[`operations/first-deployment.md`](operations/first-deployment.md).

## Validation

What ran in the environment this was written in:

| check | result |
| --- | --- |
| `python -m unittest discover -s cloud/tests` | pass |
| BigQuery schema against the adapter's rows, column for column | pass |
| `deploy.yml` parsed as YAML, triggers and permissions asserted | pass |
| scan for project ids, addresses, keys and local paths | pass |
| Terraform brace balance, declared-against-used variables, descriptions | pass |
| `python -m unittest discover -s python/tests`, including the runtime-to-cloud contract test | pass |
| `ruff check python tools cloud` and `ruff format --check cloud` (ruff 0.17.1, as pinned in CI) | pass |
| `terraform -chdir=infra fmt -check -recursive` (Terraform 1.16.6) | pass |
| `terraform -chdir=infra init -backend=false` | pass, `hashicorp/google` 6.50.0 |
| `terraform -chdir=infra validate` | pass, no errors, no warnings |
| `hadolint docker/job.Dockerfile docker/adapter.Dockerfile` (2.15.1) | pass |
| `git diff --check` | pass |

`init` writes `infra/.terraform.lock.hcl`, which is committed with hashes for
Linux, macOS and Windows, so `init` in Cloud Shell or on a workstation selects
the same provider build without rewriting the file.

`validate` checks every resource attribute against the provider's schema,
which the static checks in `cloud/tests/test_platform_static.py` cannot do. It
caught one defect they could not: an `ignore_changes` entry that indexed into
`env`, which the provider models as a set. That is why the digest now travels
as an argument; see [Containers](#containers).

The image build, `docker build -f docker/job.Dockerfile .`, runs in the cloud,
not on a workstation. Until a build has produced a real digest, nothing in
this document should be read as claiming the image is reproducible by digest.
`terraform plan` needs credentials and reads the live project, so it belongs
with the commands below.

## Commands that create billable resources

Everything above this line is inert. Each command below is not.

```sh
# 0. Terraform enables the other APIs through these two, so they must already
#    be on. A new project has neither. Enabling them is free.
gcloud services enable cloudresourcemanager.googleapis.com \
  serviceusage.googleapis.com --project=<project>

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
gcloud services enable iamcredentials.googleapis.com sts.googleapis.com \
  --project=<project>
# The deploying identity, with the three grants the workflow needs and no more.
gcloud iam service-accounts create sg-dev-deployer --project=<project>
gcloud artifacts repositories add-iam-policy-binding images \
  --location=<region> --role=roles/artifactregistry.writer \
  --member=serviceAccount:sg-dev-deployer@<project>.iam.gserviceaccount.com
gcloud run jobs add-iam-policy-binding sg-dev-run --region=<region> \
  --role=roles/run.developer \
  --member=serviceAccount:sg-dev-deployer@<project>.iam.gserviceaccount.com
gcloud iam service-accounts add-iam-policy-binding \
  sg-dev-runtime@<project>.iam.gserviceaccount.com \
  --role=roles/iam.serviceAccountUser \
  --member=serviceAccount:sg-dev-deployer@<project>.iam.gserviceaccount.com
# Bind the deploying service account to that pool, restricted to one
# repository. An unrestricted binding lets any repository deploy.
gcloud iam service-accounts add-iam-policy-binding \
  sg-dev-deployer@<project>.iam.gserviceaccount.com \
  --role=roles/iam.workloadIdentityUser \
  --member=principalSet://iam.googleapis.com/projects/<number>/locations/global/workloadIdentityPools/github/attribute.repository/<owner>/<repo>

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

## Integrated contract decisions

1. **`sg run` is installed from the project package.** The job image contains
   the full extraction dependencies and validates the production subcommand at
   build time.
2. **Manifest provenance follows the runtime schema.** Model metadata lives at
   `config.model`, while input identities use `inputs.scales_sha256` and
   `inputs.background_sha256`.
3. **Result documents are classified by shape** in `measurements/` and
   `report/`, so harmless filename changes do not break publication.
4. **The run root must contain one run.** Each Cloud Run task starts with an
   empty memory-backed volume, and the publisher refuses to guess if that
   invariant is violated.
5. **`duration_seconds` comes from `status.json`.** It is the sum of completed
   runtime stage durations.
6. **`significant` is decided by the adapter** until the runtime emits it. See
   the schema section above for the family definition and its two warnings.
