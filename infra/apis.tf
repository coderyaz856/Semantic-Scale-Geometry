# Enabling an API is free, but it is a project-level change, so the set is
# explicit and minimal rather than a convenience list.
#
# `disable_on_destroy = false` is deliberate: tearing down this module must not
# disable an API that something else in the project depends on. Disabling is a
# separate, deliberate act.

locals {
  required_apis = [
    "artifactregistry.googleapis.com",
    "bigquery.googleapis.com",
    "iam.googleapis.com",
    "logging.googleapis.com",
    "run.googleapis.com",
    "storage.googleapis.com",
  ]
}

resource "google_project_service" "required" {
  for_each = toset(local.required_apis)

  project = var.project_id
  service = each.value

  disable_on_destroy         = false
  disable_dependent_services = false
}
