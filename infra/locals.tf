locals {
  # The three required labels, with room for more. Label values must be
  # lowercase letters, digits, dashes and underscores, which every default
  # already is.
  labels = merge(
    {
      project     = var.application
      component   = var.component
      environment = var.environment
    },
    var.extra_labels,
  )

  name = "${var.name_prefix}-${var.environment}"

  # Bucket names are globally unique, so the project id is the disambiguator
  # whenever no explicit name is given.
  artifact_bucket = var.artifact_bucket_name != "" ? var.artifact_bucket_name : "${local.name}-artifacts-${var.project_id}"

  job_name = "${local.name}-${var.job_name}"

  measurement_clustering = ["run_id", "condition", "scale_id"]
}
