output "artifact_bucket" {
  description = "Bucket holding run directories, one immutable prefix per run."
  value       = google_storage_bucket.artifacts.name
}

output "artifact_registry_repository" {
  description = "Fully qualified Docker repository for job images."
  value       = "${google_artifact_registry_repository.images.location}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "bigquery_dataset" {
  description = "Dataset id of the run ledger."
  value       = google_bigquery_dataset.ledger.dataset_id
}

output "bigquery_tables" {
  description = "Table ids in the ledger."
  value = [
    google_bigquery_table.runs.table_id,
    google_bigquery_table.scale_measurements.table_id,
    google_bigquery_table.subspace_summaries.table_id,
  ]
}

output "runtime_service_account" {
  description = "Least-privilege identity the job runs as. No key exists for it, by design."
  value       = google_service_account.runtime.email
}

output "cloud_run_job" {
  description = "Cloud Run Job name."
  value       = google_cloud_run_v2_job.run.name
}

output "execute_command" {
  description = "The command that starts a billable execution. Printed here so it is never reconstructed from memory."
  value       = "gcloud run jobs execute ${google_cloud_run_v2_job.run.name} --region ${var.region} --project ${var.project_id} --wait"
}
