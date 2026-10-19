# The adapter alone: publish a run directory that was produced somewhere else.
#
# This exists because the two jobs have genuinely different dependency sets and
# different trust requirements. Extraction needs a model, a GPU and a large
# dependency tree; publishing needs two client libraries and write access to
# one bucket and one dataset. Keeping them apart means a workstation or a CI
# runner can publish a run without installing the runtime, and the identity
# that publishes never needs the identity that measures.
#
# Build context is the repository root:
#   docker build -f docker/adapter.Dockerfile -t <tag> .

FROM python:3.12-slim-bookworm

COPY cloud/requirements.txt /tmp/cloud-requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip \
 && python -m pip install --no-cache-dir -r /tmp/cloud-requirements.txt \
 && rm /tmp/cloud-requirements.txt

WORKDIR /app
COPY cloud ./cloud

ENV PYTHONPATH=/app/cloud \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Validation at build time: the entry points parse, the clients import, and the
# test suite passes against its fakes. The suite needs no credentials, which is
# the property that makes it runnable here.
RUN python -m sg_cloud.publish --help > /dev/null \
 && python -m sg_cloud.fetch --help > /dev/null \
 && python -m sg_cloud.salvage --help > /dev/null \
 && python -c "import google.cloud.storage, google.cloud.bigquery" \
 && python -m unittest discover -s cloud/tests -q > /dev/null

RUN useradd --system --no-create-home --uid 10001 publish \
 && mkdir -p /work \
 && chown publish:publish /work \
 && chmod -R a-w /app
USER 10001:10001

WORKDIR /work
ENTRYPOINT ["python", "-m", "sg_cloud.publish"]
