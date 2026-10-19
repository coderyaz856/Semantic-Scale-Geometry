"""Builds a run directory on disk that matches the frozen contract.

The fixture is generated rather than committed, for two reasons. A committed
fixture containing a raw `.f32` payload is a binary blob in a repository whose
results are meant to be regenerable. And generating it means the shape of the
contract is stated once, in code, where a change to the contract breaks a test
instead of quietly disagreeing with a checked-in file.
"""

from __future__ import annotations

import json
from pathlib import Path

RUN_ID = "r-0123456789ab"

STATUS = {
    "run_id": RUN_ID,
    "state": "SUCCEEDED",
    "created_at": "2026-10-18T09:00:00Z",
    "completed_at": "2026-10-18T09:04:12Z",
    "duration_seconds": 252.0,
    "error_code": None,
}

MANIFEST = {
    "git_commit": "0" * 40,
    "config_digest": "a" * 64,
    "config": {
        "model": {
            "id": "sentence-transformers/all-MiniLM-L6-v2",
            "revision": "c" * 40,
            "device": "cpu",
        }
    },
    "inputs": {
        "scales_sha256": "d" * 64,
        "background_sha256": "e" * 64,
    },
    "container_digest": "sha256:" + "f" * 64,
    "duration_seconds": 252.0,
}

MODEL_ID = MANIFEST["config"]["model"]["id"]
MODEL_REVISION = MANIFEST["config"]["model"]["revision"]


def measurement(layer: int, p_values: dict[str, float]) -> dict:
    """One measurement document: the format sg-measure already writes."""
    results = []
    for scale_id, p in p_values.items():
        results.append(
            {
                "scale_id": scale_id,
                "condition": "carrier",
                "remove_top": 0,
                "n_items": 6,
                "dim": 384,
                "spearman_in_sample": 1.0,
                "spearman_loo": 0.886,
                "variance_share": 0.34,
                "pc1_share": 0.35,
                "p_permutation": p,
                "permutations": 720,
                "permutation_exact": True,
                "n_contexts": 12,
                "per_context_loo_mean": 0.81,
                "per_context_loo_sd": 0.09,
            }
        )
    return {
        "model": MODEL_ID,
        "layer": layer,
        "pooling": "mean",
        "dim": 384,
        "seed": 1,
        "results": results,
    }


def subspace(layer: int) -> list[dict]:
    """One subspace document: the format sg-subspace already writes."""
    return [
        {
            "model": MODEL_ID,
            "layer": layer,
            "pooling": "mean",
            "condition": "carrier",
            "remove_top": 0,
            "dim": 384,
            "observed": {
                "n_directions": 19,
                "mean_abs_cosine": 0.078,
                "participation_ratio": 16.04,
                "components_90": 16,
                "components_99": 19,
                "eigenvalues": [1.94, 1.82, 1.59],
            },
            "null_replicates": 50,
            "null_participation_mean": 17.16,
            "null_participation_sd": 0.226,
            "null_mean_abs_cosine_mean": 0.06,
            "p_participation": 0.0196,
            "n_half_pairs": 19,
            "half_split_cosine_median": 0.9999,
        }
    ]


def build(
    root: Path,
    state: str = "SUCCEEDED",
    run_id: str = RUN_ID,
    manifest: dict | None = None,
    layers: tuple[int, ...] = (0, 3),
) -> Path:
    """Write a complete run directory and return its path."""
    run = root / run_id
    (run / "shards").mkdir(parents=True, exist_ok=True)
    (run / "directions").mkdir(parents=True, exist_ok=True)
    (run / "measurements").mkdir(parents=True, exist_ok=True)
    (run / "report").mkdir(parents=True, exist_ok=True)

    status = dict(STATUS, state=state, run_id=run_id)
    (run / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    (run / "manifest.json").write_text(
        json.dumps(MANIFEST if manifest is None else manifest, indent=2),
        encoding="utf-8",
    )
    (run / "config.yaml").write_text(
        "model: all-MiniLM-L6-v2\nlayers: [0, 3]\n", encoding="utf-8"
    )
    (run / "logs.jsonl").write_text(
        '{"severity":"INFO","message":"started"}\n', encoding="utf-8"
    )

    # A spread of p-values: two clearly ordered scales, one marginal, one not.
    p_values = {"loudness": 0.0002, "size": 0.0014, "age": 0.039, "fear": 0.47}
    for layer in layers:
        (run / "measurements" / f"layer{layer:02d}.json").write_text(
            json.dumps(measurement(layer, p_values), indent=2), encoding="utf-8"
        )
        # Raw payloads exist in a real run and must be uploaded, so the fixture
        # includes one small enough to be harmless.
        (run / "shards" / f"layer{layer:02d}.f32").write_bytes(b"\x00\x00\x80?" * 8)
        (run / "directions" / f"layer{layer:02d}.f32").write_bytes(b"\x00\x00\x00?" * 8)

    (run / "report" / "subspace.json").write_text(
        json.dumps(subspace(layers[0]), indent=2), encoding="utf-8"
    )
    (run / "report" / "report.md").write_text(
        "# Report\n\nOne table.\n", encoding="utf-8"
    )
    return run
