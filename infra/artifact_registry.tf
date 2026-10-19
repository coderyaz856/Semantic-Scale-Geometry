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

  # Under immutable tags a tagged version cannot be deleted, by a person or by a
  # policy, and a tag cannot be removed. Cleanup therefore reaches untagged
  # versions only, such as layers left behind by an interrupted push. Every
  # deployed commit's image stays until immutability is switched off; the
  # pruning procedure is in docs/cloud-deployment.md. That is the price of a
  # commit SHA naming one image forever, and it is why the job image is kept
  # small.
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
      # Stated explicitly, although immutable tags already imply it: a tagged
      # version is out of reach, so the deployed digest cannot be cleaned up
      # from under the job that references it.
      tag_state  = "UNTAGGED"
      older_than = "2592000s" # 30 days
    }
  }

  depends_on = [google_project_service.required]
}
