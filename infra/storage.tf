# One private bucket holds every run directory under an immutable per-run
# prefix. Immutability is not a convention here: the runtime service account is
# granted object creation and object reading and is deliberately not granted
# deletion, so overwriting a published artifact fails at the IAM layer rather
# than being discouraged in documentation. See iam.tf.

resource "google_storage_bucket" "artifacts" {
  project  = var.project_id
  name     = local.artifact_bucket
  location = var.region
  labels   = local.labels

  # Private by default and not negotiable per object.
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  # Nothing overwrites an object, so there is no prior version worth keeping,
  # and a kept version is billed storage.
  versioning {
    enabled = false
  }

  soft_delete_policy {
    retention_duration_seconds = var.soft_delete_retention_seconds
  }

  # Raw payloads are regenerable; their JSON sidecars, manifests and reports
  # are not. Matching on the suffix rather than a directory prefix is what
  # makes this expressible at all, because the run id sits above `shards/` in
  # the object path.
  lifecycle_rule {
    condition {
      age            = var.shard_retention_days
      matches_suffix = [".f32"]
    }

    action {
      type = "Delete"
    }
  }

  lifecycle_rule {
    condition {
      age = var.nearline_after_days
    }

    action {
      type          = "SetStorageClass"
      storage_class = "NEARLINE"
    }
  }

  # Disabled by default (see the variable). Present so that enabling full
  # expiry is a one-line, reviewable change rather than a console action.
  dynamic "lifecycle_rule" {
    for_each = var.run_delete_after_days > 0 ? [var.run_delete_after_days] : []

    content {
      condition {
        age            = lifecycle_rule.value
        matches_prefix = ["runs/"]
      }

      action {
        type = "Delete"
      }
    }
  }

  # An interrupted resumable upload leaves billed parts that do not appear in
  # an object listing, so nothing would ever notice them.
  lifecycle_rule {
    condition {
      age = 1
    }

    action {
      type = "AbortIncompleteMultipartUpload"
    }
  }

  force_destroy = false

  depends_on = [google_project_service.required]
}
