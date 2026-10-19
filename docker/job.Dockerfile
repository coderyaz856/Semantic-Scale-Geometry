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

FROM rust:1.83-bookworm AS kernel
WORKDIR /build
COPY kernel/Cargo.toml ./kernel/Cargo.toml
COPY kernel/src ./kernel/src
RUN cargo build --release --manifest-path kernel/Cargo.toml \
 && cargo test --release --manifest-path kernel/Cargo.toml

# A separate stage for the adapter's dependencies. Its wheels never touch the
# runtime stage's resolver, and the final image copies only the result.
FROM python:3.12-slim-bookworm AS adapter-deps
COPY cloud/requirements.txt /tmp/cloud-requirements.txt
RUN python -m venv /opt/adapter \
 && /opt/adapter/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/adapter/bin/pip install --no-cache-dir -r /tmp/cloud-requirements.txt

FROM python:3.12-slim-bookworm AS runtime

# The runtime half of the job needs neither torch nor a GPU: extraction is a
# separate concern on separate hardware, and the job measures what it is given.
RUN python -m pip install --no-cache-dir pyyaml==6.0.2

COPY --from=adapter-deps /opt/adapter /opt/adapter
COPY --from=kernel /build/kernel/target/release/sg-measure /usr/local/bin/sg-measure
COPY --from=kernel /build/kernel/target/release/sg-subspace /usr/local/bin/sg-subspace

WORKDIR /app
COPY python ./python
COPY cloud ./cloud
COPY tools ./tools
COPY data ./data

RUN chmod +x /app/cloud/entrypoint.sh

# `sg` is the frozen command name in the run contract. The project declares it
# as a console script, which only exists after an installation; this image runs
# from the source tree, so the name is provided by a shim that forwards to the
# same module. When the runtime branch merges and the package is installed
# properly, this shim becomes redundant rather than wrong.
RUN printf '%s\n' '#!/bin/sh' 'exec python -m scalar_geometry.cli "$@"' > /usr/local/bin/sg \
 && chmod +x /usr/local/bin/sg

# Both package roots are on the path: the runtime package and the adapter.
# The adapter's own dependencies live in its virtual environment and are added
# to the path explicitly rather than by activating it, so that one interpreter
# can import both halves.
ENV PYTHONPATH=/app/python:/app/cloud:/opt/adapter/lib/python3.12/site-packages \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RUN_ROOT=/work/runs \
    CONFIG_PATH=/config/experiment.yaml

# Startup validation at build time. Every entry point the wrapper calls is
# imported or invoked once here, so a missing module or a broken argument
# parser is a build failure.
#
# `sg run` itself is not exercised here: the subcommand arrives with the
# runtime branch, and a build that asserted it today would assert a fiction.
RUN sg plan --models 1 --layers 1 > /dev/null \
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

WORKDIR /work
ENTRYPOINT ["/app/cloud/entrypoint.sh"]
