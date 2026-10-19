# Operations log: first deployment

The commands run on 19 October 2026 to take the platform from an empty project
to two published runs and a keyless deployment from GitHub, with what each one
returned. They were run in Cloud Shell, in the order below, failures included:
each failure is followed by its cause and the command that replaced it.

Identifiers that belong to one account are replaced: `<project>` is the project
id, `<project-number>` its number, and the bucket is written
`sg-dev-artifacts-<project>`. Digests, run ids, commits and timings are as
returned. The reference procedure, without the detours, is in
[`../cloud-deployment.md`](../cloud-deployment.md).

## 1. Source and Terraform

```bash
git clone --branch Yazid-The-Tech-Orchestrator https://github.com/coderyaz856/Semantic-Scale-Geometry.git
cd Semantic-Scale-Geometry
git log --oneline -1
cp infra/terraform.tfvars.example infra/terraform.tfvars
sed -i 's/replace-with-your-project-id/<project>/' infra/terraform.tfvars
```

The clone resolved to `6e960a0`. `infra/terraform.tfvars` is ignored by git and
stays in Cloud Shell. Terraform is not installed in Cloud Shell, so the release
was downloaded, checked against HashiCorp's published checksums and unpacked
into `~/bin`:

```bash
TF=1.16.6
curl -fsSLO https://releases.hashicorp.com/terraform/$TF/terraform_${TF}_linux_amd64.zip
curl -fsSLO https://releases.hashicorp.com/terraform/$TF/terraform_${TF}_SHA256SUMS
grep linux_amd64 terraform_${TF}_SHA256SUMS | sha256sum -c   # OK
mkdir -p ~/bin && unzip -o terraform_${TF}_linux_amd64.zip terraform -d ~/bin
export PATH="$HOME/bin:$PATH"
terraform version                                             # v1.16.6
```

## 2. Infrastructure

```bash
terraform -chdir=infra init        # hashicorp/google 6.50.0, from the lock file
terraform -chdir=infra plan -var-file=terraform.tfvars -out=tfplan
```

Plan: 20 to add, 0 to change, 0 to destroy.

```bash
terraform -chdir=infra apply tfplan
```

Failed after enabling three of the six APIs: the provider manages APIs through
the Cloud Resource Manager API, which a new project does not have enabled.

```bash
gcloud services enable cloudresourcemanager.googleapis.com serviceusage.googleapis.com
terraform -chdir=infra apply -var-file=terraform.tfvars
```

Plan: 17 to add, 0 to change, 3 to destroy. The three destroys were the API
resources left tainted by the failed apply; with `disable_on_destroy = false`
replacing them removes and re-adds a state entry and disables nothing.

```text
Apply complete! Resources: 17 added, 0 changed, 3 destroyed.

artifact_bucket              = "sg-dev-artifacts-<project>"
artifact_registry_repository = "europe-west1-docker.pkg.dev/<project>/images"
bigquery_dataset             = "scale_geometry"
bigquery_tables              = ["runs", "scale_measurements", "subspace_summaries"]
cloud_run_job                = "sg-dev-run"
runtime_service_account      = "sg-dev-runtime@<project>.iam.gserviceaccount.com"
```

The prerequisite is now step 0 of the billable commands in
`docs/cloud-deployment.md`.

## 3. The job image

```bash
SHA=$(git rev-parse HEAD)
IMG=europe-west1-docker.pkg.dev/<project>/images/measure
gcloud auth configure-docker europe-west1-docker.pkg.dev --quiet
docker build --file docker/job.Dockerfile --tag $IMG:$SHA .
```

Failed in the kernel stage. The Dockerfile copied `Cargo.toml` without
`Cargo.lock`, so Cargo resolved the newest `clap`, 4.6.7, which needs Rust 1.85,
and the stage used `rust:1.83-bookworm`:

```text
feature `edition2024` is required ... not stabilized in this version of Cargo (1.83.0)
```

Fixed on `fix/cloud-first-deploy` (`fb100e6`): the kernel stage copies the lock
file, builds with `--locked` and uses `rust:1.88-bookworm`. Rebuilt from that
branch:

```bash
git fetch origin && git checkout fix/cloud-first-deploy    # baef7a9
SHA=$(git rev-parse HEAD)
docker build --file docker/job.Dockerfile --tag $IMG:$SHA .
```

Finished in 207.7 s with all 30 steps, including the kernel's tests and the
image's own `--help` checks of `sg run`, both kernel binaries and the adapter.

## 4. Publishing the image

```bash
docker push $IMG:$SHA
```

Failed twice with `dial tcp 74.125.x.82:443: connect: connection refused`. The
shell itself reached the registry:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://europe-west1-docker.pkg.dev/v2/   # 401
getent ahosts europe-west1-docker.pkg.dev                                          # 64.233.184.82
```

A 401 is the registry asking for credentials, so only the Docker daemon's
connections were refused, to addresses other than the one the shell resolved.
The image was exported and pushed with `crane` instead, which runs in the shell:

```bash
V=v0.20.3
U=https://github.com/google/go-containerregistry/releases/download/$V
curl -fsSLO $U/go-containerregistry_Linux_x86_64.tar.gz
curl -fsSLO $U/checksums.txt
grep ' go-containerregistry_Linux_x86_64.tar.gz' checksums.txt | sha256sum -c   # OK
tar -xzf go-containerregistry_Linux_x86_64.tar.gz -C ~/bin crane
docker save --platform linux/amd64 -o ~/measure.tar $IMG:$SHA                  # 373 MB
crane push ~/measure.tar $IMG:$SHA
rm ~/measure.tar
```

```text
measure:baef7a92e55f56ddaa86fc28ea67e5d5e5a16bb3:
  digest: sha256:5dbc9999f40b35144f6e9d784626a1943a5de67e1acb3805bcef662040d004e8
```

The job was pointed at the image by digest, with the digest passed as the
argument the entrypoint records:

```bash
DIGEST=$(gcloud artifacts docker images describe $IMG:$SHA --format='value(image_summary.digest)')
gcloud run jobs update sg-dev-run --region=europe-west1 \
  --image=$IMG@$DIGEST --args=--container-digest=$DIGEST
```

## 5. Configuration

The model revision was read from the model host and written into a small
configuration, then uploaded to the bucket:

```bash
H=https://huggingface.co/api/models/sentence-transformers/all-MiniLM-L6-v2
REV=$(curl -s $H | grep -o '"sha":"[0-9a-f]\{40\}"' | head -1 | cut -d'"' -f4)
gcloud storage cp ~/smoke.yaml gs://sg-dev-artifacts-<project>/configs/smoke.yaml
```

```yaml
format: semantic-scale-geometry/experiment/1
scales: /app/data/scales/scales.yaml
background: /app/data/background/wikitext2.txt
model:
  id: sentence-transformers/all-MiniLM-L6-v2
  revision: 1110a243fdf4706b3f48f1d95db1a4f5529b4d41
  device: cpu
analysis:
  removals: [0]
  random_draws: 1000
  bootstrap_draws: 200
  null_replicates: 20
execution:
  report_script: /app/tools/report.py
```

## 6. First execution

```bash
C=gs://sg-dev-artifacts-<project>/configs/smoke.yaml
gcloud run jobs execute sg-dev-run --region=europe-west1 --update-env-vars=CONFIG_URI=$C
gcloud config set run/region europe-west1
gcloud run jobs executions list --job=sg-dev-run --limit=1
```

Execution `sg-dev-run-fc9pc`: complete 1/1, from 19:21:50 to 19:25:20 UTC.

```bash
gcloud storage ls gs://sg-dev-artifacts-<project>/runs/
BQ='bq query --use_legacy_sql=false --maximum_bytes_billed=20000000'
$BQ "SELECT run_id, state, model_revision, container_digest, duration_seconds FROM scale_geometry.runs"
$BQ "SELECT layer, COUNT(*) AS n, COUNTIF(significant) AS sig FROM scale_geometry.scale_measurements GROUP BY layer ORDER BY layer"
$BQ "SELECT COUNT(*) AS n FROM scale_geometry.subspace_summaries"
$BQ "SELECT condition, scale_id FROM scale_geometry.scale_measurements WHERE significant ORDER BY scale_id"
```

| result | value |
| --- | --- |
| run directory | `runs/bdd317c9ca8322e9426d/` |
| `runs` row | SUCCEEDED, revision `1110a243…`, digest `sha256:5dbc9999…`, 135.0 s |
| `scale_measurements` | 41 rows at each of layers 0 to 6, significant only at layer 6 (5) |
| `subspace_summaries` | 7 rows |
| significant | beauty, brightness, loudness, size, valence; carrier condition |

A first attempt at these queries used a 10,000,000-byte cap and was refused:
BigQuery bills at least 10 MiB per table referenced, so the cap must be at
least 10,485,760 bytes.

## 7. Deployment from GitHub

Workload Identity Federation, so that the `deploy` workflow can act in the
project without a stored key, and only from this repository:

```bash
gcloud services enable iamcredentials.googleapis.com sts.googleapis.com
gcloud iam workload-identity-pools create github --location=global
gcloud iam workload-identity-pools providers create-oidc github \
  --location=global --workload-identity-pool=github \
  --issuer-uri=https://token.actions.githubusercontent.com \
  --attribute-mapping=google.subject=assertion.sub,attribute.repository=assertion.repository \
  --attribute-condition="assertion.repository=='coderyaz856/Semantic-Scale-Geometry'"
gcloud iam service-accounts create sg-dev-deployer
D=sg-dev-deployer@<project>.iam.gserviceaccount.com
gcloud artifacts repositories add-iam-policy-binding images --location=europe-west1 \
  --member=serviceAccount:$D --role=roles/artifactregistry.writer
gcloud run jobs add-iam-policy-binding sg-dev-run \
  --member=serviceAccount:$D --role=roles/run.developer
gcloud iam service-accounts add-iam-policy-binding sg-dev-runtime@<project>.iam.gserviceaccount.com \
  --member=serviceAccount:$D --role=roles/iam.serviceAccountUser
gcloud iam service-accounts add-iam-policy-binding $D --role=roles/iam.workloadIdentityUser \
  --member=principalSet://iam.googleapis.com/projects/<project-number>/locations/global/workloadIdentityPools/github/attribute.repository/coderyaz856/Semantic-Scale-Geometry
```

The provider was read back: condition
`assertion.repository=='coderyaz856/Semantic-Scale-Geometry'`, issuer
`https://token.actions.githubusercontent.com`, state `ACTIVE`.

Six repository variables were set in GitHub (`GCP_PROJECT_ID`, `GCP_REGION`,
`GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_DEPLOY_SERVICE_ACCOUNT`,
`GCP_ARTIFACT_REPOSITORY`, `GCP_JOB_NAME`), and no secret. After pull request
#3 merged as `96dfc94`, the `deploy` workflow was run by hand: it succeeded in
about three minutes, recorded a deployment to the `google-cloud` environment,
and updated the job:

```bash
gcloud run jobs describe sg-dev-run --format='value(spec.template.spec.template.spec.containers[0].image)'
```

```text
europe-west1-docker.pkg.dev/<project>/images/measure@sha256:f8fbc88c61aca8b10d3b4e1681071f6bf8532a01cdca04b3a1cfa71ca3ea416e
```

## 8. Second execution, on the image built by GitHub

Re-running the first configuration (`sg-dev-run-jd87g`) reuses run id
`bdd317c9ca8322e9426d`, because the id is derived from the configuration and
inputs, while its manifest and status differ in time and image. The adapter
treats a run prefix as write-once and refuses different bytes under an existing
key. That execution had already ended at 0/1 when a cancel was attempted, and
its logs were not examined. A configuration with one changed field gives a new
identity:

```bash
sed 's/  null_replicates: 20/  null_replicates: 20\n  seed: 2/' ~/smoke.yaml > ~/smoke2.yaml
gcloud storage cp ~/smoke2.yaml gs://sg-dev-artifacts-<project>/configs/smoke-seed2.yaml
gcloud run jobs execute sg-dev-run --update-env-vars=CONFIG_URI=gs://sg-dev-artifacts-<project>/configs/smoke-seed2.yaml
```

Execution `sg-dev-run-6rhkm`: complete 1/1.

| run id | seed | image | built by | state |
| --- | --- | --- | --- | --- |
| `bdd317c9ca8322e9426d` | 1 | `sha256:5dbc9999…` | Cloud Shell, `baef7a9` | SUCCEEDED |
| `ba310ab49a275d1cbed2` | 2 | `sha256:f8fbc88c…` | GitHub Actions, `96dfc94` | SUCCEEDED |

Both runs mark the same five scales significant, all at layer 6: beauty,
brightness, loudness, size and valence.

## Notes for the next operator

- Paste commands one short line at a time. The console wrapped long lines, and
  the wrapped part ran as a separate command (`--wait: command not found`).
- Set `gcloud config set run/region europe-west1` once; without it, listing
  executions prompts for a region.
- Field paths differ by tool: `gcloud run jobs describe` reports the v1 shape,
  `spec.template.spec.template.spec.containers[0].image`, not Terraform's.
- If `docker push` from Cloud Shell is refused while `curl` to the registry
  returns 401, push with `crane` as in step 4.
