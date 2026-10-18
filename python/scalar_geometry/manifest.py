"""Run manifests.

Every run records what produced it. Without this the results are anecdotes.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
from hashlib import sha256
from pathlib import Path

# Set by the container image so a result can be traced to the exact image that
# produced it. Absent outside a container, and recorded as null rather than
# guessed.
CONTAINER_DIGEST_ENV = "SG_CONTAINER_DIGEST"


def git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def git_dirty() -> bool | None:
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return bool(out.stdout.strip())


def config_digest(config: dict) -> str:
    payload = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def build(config: dict, extra: dict | None = None) -> dict:
    """Assemble a manifest. Unknown values are recorded as null, never guessed."""
    manifest = {
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "git_commit": git_commit(),
        "git_dirty": git_dirty(),
        "container_digest": os.environ.get(CONTAINER_DIGEST_ENV),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "config": config,
        "config_digest": config_digest(config),
    }
    if extra:
        manifest.update(extra)
    return manifest


def write(manifest: dict, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return path
