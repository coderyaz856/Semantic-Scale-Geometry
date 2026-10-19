# Provider and Terraform versions are constrained rather than floating. The
# Cloud Run Job below uses v2 attributes (memory-backed volumes, deletion
# protection) that are not present in every 5.x provider, so the lower bound is
# deliberate and `terraform init` is the first command that proves it.

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region

  default_labels = local.labels
}
