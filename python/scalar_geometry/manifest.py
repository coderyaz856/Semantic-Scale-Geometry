"""Run identity, manifests, and atomic metadata writes."""

from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import time
from hashlib import sha256
from pathlib import Path
from typing import Any

CONTAINER_DIGEST_ENV = "SG_CONTAINER_DIGEST"
MANIFEST_FORMAT = "semantic-scale-geometry/run-manifest/1"


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _git(command: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *command],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def git_commit() -> str | None:
    return _git(["rev-parse", "HEAD"])


def git_dirty() -> bool | None:
    value = _git(["status", "--porcelain"])
    return None if value is None else bool(value)


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def config_digest(config: dict[str, Any]) -> str:
    return sha256(canonical_json(config).encode("utf-8")).hexdigest()


def file_digest(path: str | Path) -> str:
    digest = sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_summary(path: str | Path) -> dict[str, Any]:
    """Return a content-derived digest, byte count, and file count."""
    root = Path(path)
    digest = sha256()
    count = 0
    size = 0
    files = sorted(candidate for candidate in root.rglob("*") if candidate.is_file())
    for item in files:
        relative = item.relative_to(root).as_posix()
        item_digest = file_digest(item)
        item_size = item.stat().st_size
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item_digest.encode("ascii"))
        digest.update(b"\0")
        digest.update(str(item_size).encode("ascii"))
        digest.update(b"\n")
        count += 1
        size += item_size
    return {"sha256": digest.hexdigest(), "files": count, "bytes": size}


def run_id(config: dict[str, Any], inputs: dict[str, str]) -> str:
    identity = {"config": config, "inputs": inputs}
    return sha256(canonical_json(identity).encode("utf-8")).hexdigest()[:20]


def dependency_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for distribution in ("numpy", "pyyaml", "torch", "transformers"):
        try:
            versions[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[distribution] = None
    return versions


def hardware() -> dict[str, Any]:
    details: dict[str, Any] = {
        "machine": platform.machine(),
        "processor": platform.processor() or None,
    }
    try:
        import torch
    except ImportError:
        details.update({"cuda_available": None, "cuda_version": None, "gpu": None})
        return details
    available = torch.cuda.is_available()
    details.update(
        {
            "cuda_available": available,
            "cuda_version": torch.version.cuda,
            "gpu": torch.cuda.get_device_name(0) if available else None,
        }
    )
    return details


def build(
    config: dict[str, Any],
    extra: dict[str, Any] | None = None,
    *,
    identity: str | None = None,
    inputs: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Assemble a manifest. Unknown values are recorded as null."""
    manifest: dict[str, Any] = {
        "format": MANIFEST_FORMAT,
        "run_id": identity,
        "state": "CREATED",
        "created_utc": utc_now(),
        "completed_utc": None,
        "git_commit": git_commit(),
        "git_dirty": git_dirty(),
        "container_digest": os.environ.get(CONTAINER_DIGEST_ENV),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "dependencies": dependency_versions(),
        "hardware": hardware(),
        "config": config,
        "config_digest": config_digest(config),
        "inputs": inputs or {},
        "stages": {},
        "outputs": {},
        "error": None,
    }
    if extra:
        manifest.update(extra)
    return manifest


def write_json(value: object, path: str | Path) -> Path:
    """Atomically replace a UTF-8 JSON file."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, destination)
    return destination


def write(manifest: dict[str, Any], path: str | Path) -> Path:
    return write_json(manifest, path)
