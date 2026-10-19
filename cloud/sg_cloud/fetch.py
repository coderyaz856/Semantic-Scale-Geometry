"""`sg-fetch-config`: put the experiment configuration where the runtime expects it.

The job receives a URI, not a configuration, so that the image stays generic
and an experiment is a file in a bucket rather than a rebuild. A local path is
accepted too, which is what makes the wrapper runnable outside a container.

This refuses to download something implausibly large. A configuration is a few
kilobytes; anything much bigger is a mistake about which URI was passed, and
reading it into the job's memory-backed filesystem would be the expensive way
to discover that.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from urllib.parse import urlparse

from .errors import AdapterError, Exit
from .logs import Log

MAX_CONFIG_BYTES = 1024 * 1024


def fetch(uri: str, destination: Path, log: Log) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)

    parsed = urlparse(uri)
    if parsed.scheme in ("", "file"):
        source = Path(parsed.path if parsed.scheme == "file" else uri)
        if not source.is_file():
            raise AdapterError(Exit.USAGE, f"no such configuration file: {source}")
        size = source.stat().st_size
        _check_size(size, uri)
        shutil.copyfile(source, destination)
        log.info("configuration copied", source=str(source), bytes=size)
        return destination

    if parsed.scheme != "gs":
        raise AdapterError(
            Exit.USAGE, f"unsupported configuration scheme: {parsed.scheme}://"
        )

    bucket_name = parsed.netloc
    key = parsed.path.lstrip("/")
    if not bucket_name or not key:
        raise AdapterError(Exit.USAGE, f"malformed gs:// URI: {uri}")

    from google.cloud import storage  # imported here, not at module scope

    client = storage.Client()
    blob = client.bucket(bucket_name).get_blob(key)
    if blob is None:
        raise AdapterError(Exit.USAGE, f"configuration not found: {uri}")
    _check_size(int(blob.size or 0), uri)
    blob.download_to_filename(str(destination))
    log.info("configuration downloaded", uri=uri, bytes=blob.size)
    return destination


def _check_size(size: int, uri: str) -> None:
    if size > MAX_CONFIG_BYTES:
        raise AdapterError(
            Exit.USAGE,
            f"configuration at {uri} is {size} bytes, over the"
            f" {MAX_CONFIG_BYTES} byte ceiling",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sg-fetch-config",
        description="Copy or download an experiment configuration to a local path.",
    )
    parser.add_argument("uri", help="gs:// URI, file:// URI or local path")
    parser.add_argument("destination", help="local path to write")
    args = parser.parse_args(argv)

    log = Log(component="sg-fetch-config")
    try:
        fetch(args.uri, Path(args.destination), log)
        return int(Exit.OK)
    except AdapterError as error:
        log.error(str(error), exit_code=int(error.code), **error.context)
        return int(error.code)


if __name__ == "__main__":
    raise SystemExit(main())
