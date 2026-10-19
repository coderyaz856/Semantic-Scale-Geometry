# One Cloud Run Job, one task, no parallelism. Every cost control that can be
# expressed as a resource attribute is expressed here rather than left to
# operator discipline: a wall-clock timeout, a retry ceiling, explicit CPU and
# memory limits, and a bounded memory-backed scratch volume.
#
# Jobs have no minimum instance count to set to zero: a job holds no idle
# capacity between executions and bills only while a task runs. That is the
# reason a job is the right shape for this workload and a service is not.

resource "google_cloud_run_v2_job" "run" {
  project  = var.project_id
  name     = local.job_name
  location = var.region
  labels   = local.labels

  deletion_protection = false

  template {
    task_count  = 1
    parallelism = 1

    template {
      service_account = google_service_account.runtime.email
      max_retries     = var.job_max_retries
      timeout         = var.job_task_timeout

      containers {
        image = var.job_image

        resources {
          limits = {
            cpu    = var.job_cpu
            memory = var.job_memory
          }
        }

        env {
          name  = "CONFIG_URI"
          value = var.config_uri
        }

        env {
          name  = "RUN_ROOT"
          value = "/work/runs"
        }

        env {
          name  = "ARTIFACT_BUCKET"
          value = google_storage_bucket.artifacts.name
        }

        env {
          name  = "BQ_PROJECT"
          value = var.project_id
        }

        env {
          name  = "BQ_DATASET"
          value = google_bigquery_dataset.ledger.dataset_id
        }

        env {
          name  = "BQ_LOCATION"
          value = var.bigquery_location
        }

        env {
          name  = "BQ_MAX_BYTES_BILLED"
          value = tostring(var.query_bytes_billed_limit)
        }

        env {
          name  = "PUBLISH_TO_BIGQUERY"
          value = var.publish_to_bigquery ? "1" : "0"
        }

        volume_mounts {
          name       = "work"
          mount_path = "/work"
        }

        volume_mounts {
          name       = "config"
          mount_path = "/config"
        }
      }

      # The application files are read-only in the image; /work is the only
      # writable path. It is memory-backed, so its size limit must fit inside
      # the memory limit above together with the process itself.
      volumes {
        name = "work"

        empty_dir {
          medium     = "MEMORY"
          size_limit = var.job_work_volume_size
        }
      }

      # The configuration the wrapper downloads lands here. A separate, tiny
      # volume rather than a corner of /work, so that an experiment
      # configuration cannot be mistaken for a run artifact and uploaded as
      # one.
      volumes {
        name = "config"

        empty_dir {
          medium     = "MEMORY"
          size_limit = "16Mi"
        }
      }
    }
  }

  # The deployment workflow owns two things after the first apply: the image,
  # which it sets to the digest it published, and SG_CONTAINER_DIGEST, which
  # records that digest for the run manifest. Without these exclusions every
  # apply would revert the job to the default image and every deployment would
  # show a spurious diff.
  #
  # The cost is stated rather than hidden: because the whole environment block
  # is excluded, changing one of the values above does not reach a deployed job
  # through `apply` alone. Redeploy through the workflow, or use
  # `terraform apply -replace=google_cloud_run_v2_job.run`.
  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
      template[0].template[0].containers[0].env,
    ]
  }

  depends_on = [
    google_project_service.required,
    google_artifact_registry_repository_iam_member.runtime_image_reader,
  ]
}
