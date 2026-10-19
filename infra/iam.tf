# The runtime identity. No keys are created here, and none should ever be:
# the job receives this identity from Cloud Run, and a human or a workflow
# receives one through impersonation or Workload Identity Federation.
#
# The grants below are deliberately narrower than the convenient ones:
#
#   storage.objectCreator + storage.objectViewer, not objectAdmin
#       Create and read, but no delete and no overwrite. The per-run prefix is
#       then immutable because the identity cannot do otherwise, which is a
#       stronger guarantee than the adapter's own pre-upload check.
#
#   bigquery.dataEditor on the dataset, not on the project
#       The adapter loads staging tables and merges into three tables. Scoping
#       to the dataset keeps that authority off every other dataset.
#
#   bigquery.jobUser on the project
#       Load and query jobs are project-level operations; there is no
#       dataset-scoped equivalent. This role can start jobs and cannot read
#       data by itself.

resource "google_service_account" "runtime" {
  project      = var.project_id
  account_id   = "${local.name}-runtime"
  display_name = "Semantic scale geometry measurement job"
  description  = "Runs the measurement job and publishes its run directory and ledger rows."

  depends_on = [google_project_service.required]
}

resource "google_storage_bucket_iam_member" "runtime_object_create" {
  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectCreator"
  member = google_service_account.runtime.member
}

resource "google_storage_bucket_iam_member" "runtime_object_read" {
  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectViewer"
  member = google_service_account.runtime.member
}

resource "google_bigquery_dataset_iam_member" "runtime_data_editor" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.ledger.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = google_service_account.runtime.member
}

resource "google_project_iam_member" "runtime_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = google_service_account.runtime.member
}

resource "google_project_iam_member" "runtime_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = google_service_account.runtime.member
}

# Pulling the job image. Scoped to the one repository rather than the project.
resource "google_artifact_registry_repository_iam_member" "runtime_image_reader" {
  project    = var.project_id
  location   = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.repository_id
  role       = "roles/artifactregistry.reader"
  member     = google_service_account.runtime.member
}
