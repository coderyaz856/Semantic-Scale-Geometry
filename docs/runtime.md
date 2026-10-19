# Production runtime

The production entry point executes extraction, per-layer measurement,
shared-subspace analysis, and report generation as one reproducible run:

```console
sg run --config /config/experiment.yaml --run-root /work/runs
```

The command validates the complete configuration before loading a model. The
model revision must be an exact 40-character commit SHA, so a mutable branch or
tag cannot silently change the embeddings. Relative input paths are resolved
from the configuration file.

## Experiment configuration

```yaml
format: semantic-scale-geometry/experiment/1
scales: data/scales/scales.yaml
background: data/background/wikitext2.txt
model:
  id: sentence-transformers/all-MiniLM-L6-v2
  # Replace this example with the exact commit published by the model host.
  revision: 0123456789abcdef0123456789abcdef01234567
  device: cpu
  pooling: mean
  max_length: 48
  batch_size: 16
analysis:
  removals: [0, 1, 3]
  random_draws: 10000
  bootstrap_draws: 2000
  seed: 1
  null_replicates: 50
  subspace_condition: carrier
execution:
  kernel_binary: sg-measure
  subspace_binary: sg-subspace
  report_script: tools/report.py
```

The authoritative machine-readable contract is
[`schemas/experiment.schema.json`](../schemas/experiment.schema.json). Runtime
validation also rejects unknown fields, missing input files, duplicate removal
values, invalid minimums, and unsupported pooling modes.

## Run identity and outputs

The run identifier is the first 20 hexadecimal characters of SHA-256 over the
normalized configuration and the SHA-256 identities of both input files. A
changed model revision, seed, analysis setting, scale definition, or background
corpus therefore produces a different directory.

```text
runs/<run-id>/
  config.yaml
  manifest.json
  status.json
  logs.jsonl
  shards/
  directions/
  measurements/
  report/
    subspace.json
    summary.txt
```

`manifest.json` records the normalized configuration, input hashes, Git state,
container digest when `SG_CONTAINER_DIGEST` is set, dependency versions,
hardware metadata, stage durations, and content-derived output summaries.
`status.json` is a compact state record for a scheduler. `logs.jsonl` contains
structured lifecycle and command events.

The schemas for downstream systems are
[`schemas/run-manifest.schema.json`](../schemas/run-manifest.schema.json) and
[`schemas/run-status.schema.json`](../schemas/run-status.schema.json).

## Atomic publication and recovery

Each stage writes to a run-local temporary path. Outputs become visible at
their final paths only after the stage succeeds and validates its result. A
completion record stores the output hashes in `.stages/`.

Re-running the same command verifies those hashes and skips completed stages.
A fully successful run is reused without executing work again. If a completed
artifact has changed, the runtime stops instead of presenting altered data as
the original immutable run. A failed stage leaves the status as `FAILED`. Once
the underlying problem is fixed, the same command resumes from the latest
valid completion record.

Exit status `0` means the run succeeded or an identical completed run was
reused. Configuration errors, unsafe resume conditions, and stage failures use
a nonzero status and retain the structured failure in both the status and the
manifest.

## Runtime prerequisites

Install the Python package, build both Rust binaries, and make `sg-measure` and
`sg-subspace` available on `PATH`. The report script path is interpreted from
the process working directory unless an absolute path is provided. Extraction
requires access to the pinned model revision on its first use. Once the model
is cached, measurement, analysis, reporting, recovery, and inspection are
local operations.
