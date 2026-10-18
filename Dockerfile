# Two stages, because the two halves of this pipeline need nothing from each
# other at runtime. The kernel is a static-ish binary built once; the Python
# side only needs it on the path.
#
# Version tags are explicit rather than floating. A published image should be
# referenced by digest, passed through SG_CONTAINER_DIGEST, and recorded by the
# manifest integration that remains on the roadmap.

FROM rust:1.83-bookworm AS kernel
WORKDIR /build
COPY kernel/Cargo.toml ./kernel/Cargo.toml
COPY kernel/src ./kernel/src
RUN cargo build --release --manifest-path kernel/Cargo.toml \
 && cargo test --release --manifest-path kernel/Cargo.toml

FROM python:3.12-slim-bookworm
WORKDIR /app

# The measurement job needs neither torch nor a GPU, so the default image stays
# small. Extraction installs its own dependencies in a separate image.
RUN pip install --no-cache-dir pyyaml==6.0.2

COPY --from=kernel /build/kernel/target/release/sg-measure /usr/local/bin/sg-measure
COPY python ./python
COPY tools ./tools
COPY data ./data

ENV PYTHONPATH=/app/python \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Fail fast and loudly if the image is broken, before a batch of array jobs
# discovers it one cell at a time.
RUN python -m scalar_geometry.cli plan --models 1 --layers 1 > /dev/null \
 && sg-measure --help > /dev/null

ENTRYPOINT ["python", "-m", "scalar_geometry.cli"]
CMD ["plan"]
