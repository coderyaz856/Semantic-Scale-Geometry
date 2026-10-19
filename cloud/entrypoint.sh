#!/usr/bin/env bash
# Cloud execution wrapper.
#
#   1. fetch the experiment configuration named by CONFIG_URI
#   2. run the frozen runtime contract:
#        sg run --config /config/experiment.yaml --run-root /work/runs
#   3. on success, publish the run directory and the ledger rows
#   4. on failure, salvage the logs and exit with the runtime's own code
#
# Nothing here contains a credential, a project, a bucket or a path outside the
# container. Every value arrives as an environment variable, set by the Cloud
# Run Job in infra/cloud_run.tf, so the same image runs in any project.
#
# The runtime's exit code survives: a failure to upload logs must not mask the
# failure being logged, and a successful run whose publish failed must not look
# like a successful publish.

set -euo pipefail

CONFIG_PATH="${CONFIG_PATH:-/config/experiment.yaml}"
RUN_ROOT="${RUN_ROOT:-/work/runs}"
PUBLISH_TO_BIGQUERY="${PUBLISH_TO_BIGQUERY:-1}"
SALVAGE_ON_FAILURE="${SALVAGE_ON_FAILURE:-1}"

log() {
  # One JSON object per line, matching the adapter's own log format so that a
  # single Cloud Logging filter covers the whole task.
  printf '{"severity":"%s","component":"entrypoint","message":"%s"}\n' "$1" "$2" >&2
}

die() {
  log ERROR "$1"
  exit "${2:-1}"
}

if [[ -z "${CONFIG_URI:-}" ]]; then
  die "CONFIG_URI is not set: there is no experiment to run" 2
fi

# Fail fast on an unwritable scratch path. Both /work and /config are mounted
# volumes; if either is read-only the run would fail much later, with a
# traceback that does not say why.
for directory in "$(dirname "${CONFIG_PATH}")" "${RUN_ROOT}"; do
  mkdir -p "${directory}" 2>/dev/null || die "cannot create ${directory}" 2
  probe="${directory}/.writable"
  : >"${probe}" 2>/dev/null || die "${directory} is not writable by this user" 2
  rm -f "${probe}"
done

log INFO "fetching configuration"
python -m sg_cloud.fetch "${CONFIG_URI}" "${CONFIG_PATH}"

log INFO "starting run"
set +e
sg run --config "${CONFIG_PATH}" --run-root "${RUN_ROOT}"
runtime_status=$?
set -e

if [[ ${runtime_status} -ne 0 ]]; then
  log ERROR "runtime exited with status ${runtime_status}"
  if [[ "${SALVAGE_ON_FAILURE}" == "1" && -n "${ARTIFACT_BUCKET:-}" ]]; then
    # The run directory is memory-backed and disappears with the task, so this
    # is the only chance to keep the logs. Its own failure is reported and then
    # ignored: the exit code below belongs to the run.
    salvage_id="${CLOUD_RUN_EXECUTION:-local}-${CLOUD_RUN_TASK_INDEX:-0}"
    python -m sg_cloud.salvage "${RUN_ROOT}" \
      --bucket "${ARTIFACT_BUCKET}" \
      --run-id "${salvage_id}" || log WARNING "salvage failed"
  fi
  exit "${runtime_status}"
fi

if [[ -z "${ARTIFACT_BUCKET:-}" ]]; then
  log WARNING "run succeeded but ARTIFACT_BUCKET is unset: nothing was published"
  exit 0
fi

publish_args=("${RUN_ROOT}" --bucket "${ARTIFACT_BUCKET}")
if [[ "${PUBLISH_TO_BIGQUERY}" == "1" ]]; then
  [[ -n "${BQ_DATASET:-}" ]] || die "PUBLISH_TO_BIGQUERY is set but BQ_DATASET is not" 2
  [[ -n "${BQ_PROJECT:-}" ]] || die "PUBLISH_TO_BIGQUERY is set but BQ_PROJECT is not" 2
  publish_args+=(--bq-project "${BQ_PROJECT}" --bq-dataset "${BQ_DATASET}")
  # An `&&` tail here would end the script under `set -e` whenever the
  # variable happens to be unset.
  if [[ -n "${BQ_LOCATION:-}" ]]; then
    publish_args+=(--bq-location "${BQ_LOCATION}")
  fi
fi

log INFO "publishing run"
python -m sg_cloud.publish "${publish_args[@]}"

log INFO "task complete"
