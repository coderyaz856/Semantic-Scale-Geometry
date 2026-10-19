# The ledger holds metadata and compact measurements only. Embeddings,
# exported directions and reports stay in the bucket: BigQuery is the index
# over runs, not a copy of them.
#
# Partitioning and clustering choices are explained in
# docs/cloud-deployment.md and summarised here. The short version: `runs` is
# partitioned by time because a run happens at a time, and the two measurement
# tables are partitioned by `layer` because `layer` is both the analysis axis
# and part of the merge key, so an idempotent reload prunes to the partitions
# it rewrites instead of scanning the table.

resource "google_bigquery_dataset" "ledger" {
  project    = var.project_id
  dataset_id = var.dataset_id
  location   = var.bigquery_location
  labels     = local.labels

  friendly_name = "Semantic scale geometry run ledger"
  description   = "Run metadata, scale measurements and shared-subspace summaries."

  delete_contents_on_destroy = !var.dataset_deletion_protection

  depends_on = [google_project_service.required]
}

resource "google_bigquery_table" "runs" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.ledger.dataset_id
  table_id   = "runs"
  labels     = local.labels

  description         = "One row per run. Merge key: run_id."
  deletion_protection = var.table_deletion_protection
  schema              = file("${path.module}/bigquery/runs.json")

  time_partitioning {
    type  = "DAY"
    field = "created_at"
  }

  # A ledger is read by state far more often than by model, and both are low
  # cardinality, so clustering order follows the common filter.
  clustering = ["state", "model_id"]
}

resource "google_bigquery_table" "scale_measurements" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.ledger.dataset_id
  table_id   = "scale_measurements"
  labels     = local.labels

  description         = "One row per scale, condition and preprocessing cell. Merge key: run_id, model_id, layer, scale_id, condition, remove_top."
  deletion_protection = var.table_deletion_protection
  schema              = file("${path.module}/bigquery/scale_measurements.json")

  range_partitioning {
    field = "layer"

    range {
      start    = 0
      end      = var.layer_partition_ceiling
      interval = 1
    }
  }

  require_partition_filter = var.require_partition_filter
  clustering               = local.measurement_clustering
}

resource "google_bigquery_table" "subspace_summaries" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.ledger.dataset_id
  table_id   = "subspace_summaries"
  labels     = local.labels

  description         = "One row per group of scale directions. Merge key: run_id, model_id, layer, condition, remove_top."
  deletion_protection = var.table_deletion_protection
  schema              = file("${path.module}/bigquery/subspace_summaries.json")

  range_partitioning {
    field = "layer"

    range {
      start    = 0
      end      = var.layer_partition_ceiling
      interval = 1
    }
  }

  require_partition_filter = var.require_partition_filter

  # No scale_id here: a row summarises a whole group of directions.
  clustering = ["run_id", "condition", "remove_top"]
}
