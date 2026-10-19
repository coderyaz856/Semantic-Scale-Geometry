# Nothing in this module carries an account identifier, a billing identifier,
# an address or a local path. Every name is a variable with a defensible
# default, and the only value without a default is the project to deploy into.

variable "project_id" {
  description = "Google Cloud project that will own every resource in this module."
  type        = string
}

variable "region" {
  description = "Region for Artifact Registry, the artifact bucket and the Cloud Run Job."
  type        = string
  default     = "europe-west1"
}

variable "bigquery_location" {
  description = <<-EOT
    BigQuery dataset location. Multi-region "EU" by default, which matters more
    for a ledger than for the regenerable artifacts in the bucket. A dataset
    location cannot be changed after creation; moving it means creating a
    second dataset and copying into it.
  EOT
  type        = string
  default     = "EU"
}

variable "application" {
  description = "Value of the `project` resource label: the application, not the cloud project."
  type        = string
  default     = "scalar-geometry"
}

variable "component" {
  description = "Value of the `component` resource label."
  type        = string
  default     = "measurement-platform"
}

variable "environment" {
  description = "Value of the `environment` resource label, and part of every resource name."
  type        = string
  default     = "dev"
}

variable "name_prefix" {
  description = "Short prefix for generated resource names."
  type        = string
  default     = "sg"
}

variable "extra_labels" {
  description = "Additional labels, merged into the three required ones."
  type        = map(string)
  default     = {}
}

# ------------------------------------------------------------------ registry

variable "artifact_repository_id" {
  description = "Artifact Registry Docker repository id."
  type        = string
  default     = "images"
}

variable "immutable_image_tags" {
  description = <<-EOT
    Refuse to move an existing tag. A commit-SHA tag then names one image
    forever, at the price that re-running a deployment for the same commit
    cannot push again; the workflow detects that case and reuses the tag.
  EOT
  type        = bool
  default     = true
}

variable "image_cleanup_keep_versions" {
  description = <<-EOT
    How many image versions to keep. Older ones are deleted by an Artifact
    Registry cleanup policy, because one image per commit is the single storage
    line in this platform that grows without bound.
  EOT
  type        = number
  default     = 10
}

# -------------------------------------------------------------------- bucket

variable "artifact_bucket_name" {
  description = "Override for the artifact bucket name. Empty derives one from the prefix, environment and project."
  type        = string
  default     = ""
}

variable "shard_retention_days" {
  description = <<-EOT
    Age at which raw `.f32` payloads are deleted. Shards and exported
    directions are regenerable from a pinned model, a pinned corpus and a
    seed, so they are the cheapest objects here to discard and the most
    expensive to keep. The JSON sidecars that describe them are not touched,
    so a deleted payload leaves a readable record of what it held.
  EOT
  type        = number
  default     = 30
}

variable "nearline_after_days" {
  description = "Age at which surviving objects move to Nearline storage."
  type        = number
  default     = 30
}

variable "run_delete_after_days" {
  description = <<-EOT
    Age at which every object under the run prefix is deleted, manifests and
    reports included. Zero disables the rule, which is how it ships: a
    lifecycle rule that silently deletes the evidence behind a published number
    is worse than a storage bill.
  EOT
  type        = number
  default     = 0
}

variable "soft_delete_retention_seconds" {
  description = <<-EOT
    Bucket soft-delete window. Soft-deleted objects are billed as storage, and
    nothing in this platform is permitted to overwrite or delete an object in
    this bucket, so the window buys nothing and costs money. Zero disables it.
  EOT
  type        = number
  default     = 0
}

# ------------------------------------------------------------------ bigquery

variable "dataset_id" {
  description = "BigQuery dataset id for the run ledger."
  type        = string
  default     = "scale_geometry"
}

variable "layer_partition_ceiling" {
  description = <<-EOT
    Exclusive upper bound of the integer-range partitioning on `layer`. A model
    with more layers than this still loads, but its deepest layers fall outside
    the declared partitions and stop being pruned, so raise this before
    measuring a deeper model.
  EOT
  type        = number
  default     = 64
}

variable "require_partition_filter" {
  description = <<-EOT
    Force every query against the measurement tables to filter on `layer`. A
    real cost control and a real inconvenience: `SELECT * FROM
    scale_measurements` stops working, and the adapter's MERGE carries no
    constant predicate on `layer`, so turning this on means changing
    cloud/sg_cloud/ledger.py as well. Off by default because these tables are
    small enough that clustering does the pruning that matters.
  EOT
  type        = bool
  default     = false
}

variable "dataset_deletion_protection" {
  description = "Refuse `terraform destroy` while the dataset still holds tables."
  type        = bool
  default     = true
}

variable "table_deletion_protection" {
  description = "Refuse `terraform destroy` on a table that holds rows."
  type        = bool
  default     = true
}

# ----------------------------------------------------------------- cloud run

variable "job_name" {
  description = "Cloud Run Job name, after the generated prefix."
  type        = string
  default     = "run"
}

variable "job_image" {
  description = <<-EOT
    Image for the Cloud Run Job. The default is Google's public sample job
    image so that the first `apply` can precede the first image build; the
    deployment workflow then replaces it with a commit-SHA tag. The resource
    ignores later changes to this attribute, so Terraform and the workflow do
    not revert each other on every run.
  EOT
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/job:latest"
}

variable "job_cpu" {
  description = "CPU limit per task."
  type        = string
  default     = "1"
}

variable "job_memory" {
  description = <<-EOT
    Memory limit per task. The writable run directory is a memory-backed
    volume, so this one ceiling covers the process and the run directory
    together.
  EOT
  type        = string
  default     = "2Gi"
}

variable "job_work_volume_size" {
  description = "Size limit of the memory-backed /work volume. Counts against job_memory."
  type        = string
  default     = "1Gi"
}

variable "job_task_timeout" {
  description = "Wall-clock ceiling per task, and the main defence against a hung run billing all month."
  type        = string
  default     = "1800s"
}

variable "job_max_retries" {
  description = <<-EOT
    Retries per task. One covers an infrastructure fault; more would just
    multiply the cost of a deterministic failure, which is the failure this
    pipeline is most likely to have.
  EOT
  type        = number
  default     = 1
}

variable "config_uri" {
  description = "Default CONFIG_URI: the gs:// URI of an experiment configuration. Empty means it must be supplied per execution."
  type        = string
  default     = ""
}

variable "publish_to_bigquery" {
  description = "Whether the job's entry point loads the ledger after a successful run."
  type        = bool
  default     = true
}

variable "query_bytes_billed_limit" {
  description = <<-EOT
    Byte ceiling applied to every BigQuery job the adapter issues. A query that
    would scan more is rejected rather than billed. 1 GiB is far above anything
    this ledger can legitimately need, and far below an accident.
  EOT
  type        = number
  default     = 1073741824
}
