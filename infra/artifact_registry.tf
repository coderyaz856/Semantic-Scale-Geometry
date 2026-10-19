resource "google_artifact_registry_repository" "images" {
  project       = var.project_id
  location      = var.region
  repository_id = var.artifact_repository_id
  description   = "Measurement job images, tagged by commit SHA."
  format        = "DOCKER"
  labels        = local.labels

  # A commit-SHA tag should name one image forever. The cost is that pushing
  # the same SHA twice fails, so the deployment workflow checks for an existing
  # tag and reuses it instead of rebuilding.
  docker_config {
    immutable_tags = var.immutable_image_tags
  }

  # Keep the most recent versions and delete the rest. Without this, a
  # repository that is deployed on every merge grows forever, and image storage
  # is the one cost here that survives every job finishing.
  cleanup_policy_dry_run = false

  cleanup_policies {
    id     = "keep-recent-versions"
    action = "KEEP"

    most_recent_versions {
      keep_count = var.image_cleanup_keep_versions
    }
  }

  cleanup_policies {
    id     = "delete-the-rest"
    action = "DELETE"

    condition {
      older_than = "2592000s" # 30 days
    }
  }

  depends_on = [google_project_service.required]
}
