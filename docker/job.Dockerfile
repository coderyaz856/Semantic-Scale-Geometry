# The image the Cloud Run Job executes: measurement runtime, kernel binary and
# cloud adapter in one artifact, with their dependencies kept apart.
#
# The root Dockerfile is the local measurement image and is left alone. This
# one differs from it in the ways a job needs:
#
#   * the cloud adapter and its two client libraries are installed, into their
#     own virtual environment, so a resolver conflict between a client library
#     and the runtime's dependencies cannot arise at all;
#   * the process runs as a non-root user that owns nothing it executes;
#   * the application tree is read-only and /work is the only writable path;
#   * a startup validation runs at build time, so a broken image fails here
#     rather than one task at a time in the cloud.
#
# Base images are pinned by version tag. They are not pinned by digest yet,
# because a digest that has not been observed is a fiction; the deployment
# workflow records the digest of what it actually built and pushed.
#
# Build context is the repository root:
#   docker build -f docker/job.Dockerfile -t <tag> .

# 1.85 is the oldest toolchain the locked clap release accepts. The lock file
# is copied and enforced, so the kernel is built from the reviewed dependency
# versions rather than whatever crates.io resolves on the day of the build.
FROM rust:1.88-bookworm AS kernel
WORKDIR /build
COPY kernel/Cargo.toml kernel/Cargo.lock ./kernel/
COPY kernel/src ./kernel/src
RUN cargo build --release --locked --manifest-path kernel/Cargo.toml \
 && cargo test --release --locked --manifest-path kernel/Cargo.toml

# A separate stage for the adapter's dependencies. Its wheels never touch the
# runtime stage's resolver, and the final image copies only the result.
FROM python:3.12-slim-bookworm AS adapter-deps
COPY cloud/requirements.txt /tmp/cloud-requirements.txt
# Each venv's bundled pip is used as the base image ships it. Upgrading it
# would pull an unpinned package from the network on every build, which is a
# reproducibility hole for no gain.
RUN python -m venv /opt/adapter \
 && /opt/adapter/bin/pip install --no-cache-dir -r /tmp/cloud-requirements.txt

# torch is a dependency of the runtime package, and pip's default Linux wheel is
# the CUDA build, which brings several gigabytes of GPU libraries. This job runs
# on CPU, and under immutable tags every deployed image is stored until pruned
# by hand, so the CPU build is installed first; `pip install .` then finds
# torch already satisfied and does not fetch the CUDA one.
FROM python:3.12-slim-bookworm AS runtime-deps
WORKDIR /source
COPY pyproject.toml ./pyproject.toml
COPY python ./python
RUN python -m venv /opt/runtime \
 && /opt/runtime/bin/pip install --no-cache-dir \
      --index-url https://download.pytorch.org/whl/cpu "torch>=2.2" \
 && /opt/runtime/bin/pip install --no-cache-dir .

FROM python:3.12-slim-bookworm AS runtime

COPY --from=adapter-deps /opt/adapter /opt/adapter
COPY --from=runtime-deps /opt/runtime /opt/runtime
COPY --from=kernel /build/kernel/target/release/sg-measure /usr/local/bin/sg-measure
COPY --from=kernel /build/kernel/target/release/sg-subspace /usr/local/bin/sg-subspace

WORKDIR /app
COPY cloud ./cloud
COPY tools ./tools
COPY data ./data
COPY schemas ./schemas

RUN chmod +x /app/cloud/entrypoint.sh

# Both package roots are on the path: the runtime package and the adapter.
# The adapter's own dependencies live in its virtual environment and are added
# to the path explicitly rather than by activating it, so that one interpreter
# can import both halves.
ENV PATH=/opt/runtime/bin:$PATH \
    PYTHONPATH=/app/cloud:/opt/adapter/lib/python3.12/site-packages \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/work/cache \
    RUN_ROOT=/work/runs \
    CONFIG_PATH=/config/experiment.yaml

# Startup validation at build time. Every entry point the wrapper calls is
# imported or invoked once here, so a missing module or a broken argument
# parser is a build failure.
#
# Exercise the merged runtime contract without loading a model. This verifies
# that the installed console script exposes the production subcommand.
RUN sg run --help > /dev/null \
 && sg-measure --help > /dev/null \
 && sg-subspace --help > /dev/null \
 && python -m sg_cloud.publish --help > /dev/null \
 && python -m sg_cloud.fetch --help > /dev/null \
 && python -m sg_cloud.salvage --help > /dev/null \
 && python -c "import google.cloud.storage, google.cloud.bigquery"

# A non-root user that does not own /app. The application tree is therefore
# read-only to the process that runs it, without needing a read-only root
# filesystem the platform may not offer.
RUN useradd --system --no-create-home --uid 10001 measure \
 && mkdir -p /work /config \
 && chown measure:measure /work /config \
 && chmod -R a-w /app
USER 10001:10001

WORKDIR /app
ENTRYPOINT ["/app/cloud/entrypoint.sh"]
