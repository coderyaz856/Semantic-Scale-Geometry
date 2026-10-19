"""Embedding extraction.

This is the only part of the pipeline that needs a GPU, and it runs once per
model. Everything downstream reuses the vectors it writes.

Repeatability is the point of the choices below. Three common sources of
variation are controlled here:

1. Padding length. With dynamic padding, a text's vector depends on the
   longest text in its batch, so results change when batching changes. Every
   text is padded to a fixed ``max_length`` instead.
2. Reduced precision. Autocast and fp16 reorder accumulations. Extraction runs
   in float32.
3. Nondeterministic kernels. Some GPU operations use atomics, so
   deterministic algorithms are requested explicitly.

These settings reduce avoidable variation within a pinned environment. They do
not establish bit-for-bit identity across CPU and GPU backends. The current
end-to-end contract test exercises the Python-to-Rust interchange on synthetic
vectors; a cross-backend extraction benchmark remains future work.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch
from transformers import AutoModel, AutoTokenizer

from .scales import Row

DEFAULT_MAX_LENGTH = 48
DEFAULT_BATCH_SIZE = 16


@dataclass(frozen=True)
class ExtractionConfig:
    model: str
    revision: str
    max_length: int = DEFAULT_MAX_LENGTH
    batch_size: int = DEFAULT_BATCH_SIZE
    device: str = "cpu"
    pooling: str = "mean"

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-fA-F]{40}", self.revision):
            raise ValueError("model revision must be a 40-character commit SHA")


def pin_determinism() -> None:
    torch.use_deterministic_algorithms(True, warn_only=True)
    torch.backends.cudnn.benchmark = False
    if hasattr(torch.backends, "cuda"):
        torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False


def load_background(path: str | Path) -> list[str]:
    """Background texts used to estimate the anisotropy of the space.

    These must be unrelated to the scales. Estimating dominant directions from
    the scale items themselves would remove the signal along with the
    artifact, which is the mistake this file exists to avoid.
    """
    lines = [
        line.strip()
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(lines) < 500:
        raise ValueError(
            f"background corpus has {len(lines)} lines; dominant directions "
            "estimated from fewer than 500 are not trustworthy"
        )
    return lines


def _pool(hidden: torch.Tensor, mask: torch.Tensor, pooling: str) -> torch.Tensor:
    if pooling == "cls":
        return hidden[:, 0, :]
    if pooling != "mean":
        raise ValueError(f"unknown pooling: {pooling}")
    m = mask.unsqueeze(-1).to(hidden.dtype)
    return (hidden * m).sum(dim=1) / m.sum(dim=1).clamp(min=1e-9)


def embed(texts: list[str], config: ExtractionConfig) -> np.ndarray:
    """Embed every text at every layer.

    Returns an array of shape ``(n_layers, n_texts, dim)`` in float32, where
    layer 0 is the embedding layer before any transformer block.
    """
    pin_determinism()
    tokenizer = AutoTokenizer.from_pretrained(
        config.model,
        revision=config.revision,
    )
    model = AutoModel.from_pretrained(
        config.model,
        revision=config.revision,
        dtype=torch.float32,
    )
    model.eval()
    model.to(config.device)

    per_layer: list[list[np.ndarray]] = []
    with torch.inference_mode():
        for start in range(0, len(texts), config.batch_size):
            batch = texts[start : start + config.batch_size]
            encoded = tokenizer(
                batch,
                padding="max_length",
                truncation=True,
                max_length=config.max_length,
                return_tensors="pt",
            ).to(config.device)
            out = model(**encoded, output_hidden_states=True)
            mask = encoded["attention_mask"]
            if per_layer == []:
                per_layer = [[] for _ in out.hidden_states]
            for index, hidden in enumerate(out.hidden_states):
                pooled = _pool(hidden, mask, config.pooling)
                per_layer[index].append(pooled.to(torch.float32).cpu().numpy())

    if not per_layer:
        raise ValueError("no texts to embed")
    return np.stack([np.concatenate(chunks, axis=0) for chunks in per_layer])


def write_shards(
    out_dir: str | Path,
    rows: list[Row],
    background: list[str],
    config: ExtractionConfig,
) -> list[Path]:
    """Embed the scale rows and the background, then write one shard per layer.

    A shard is a raw float32 payload plus a JSON sidecar. The format is
    documented in ``docs/interchange.md``; it is deliberately plain so that the
    Rust kernel needs no parquet or YAML dependency and so that a shard can be
    read with twenty lines of code in any language.
    """
    out_dir = Path(out_dir)
    texts = [row.text for row in rows] + list(background)
    vectors = embed(texts, config)
    n_layers, n_texts, dim = vectors.shape
    assert n_texts == len(texts)

    written: list[Path] = []
    for layer in range(n_layers):
        layer_dir = out_dir / f"layer_{layer:02d}"
        layer_dir.mkdir(parents=True, exist_ok=True)
        matrix = np.ascontiguousarray(vectors[layer], dtype="<f4")
        (layer_dir / "embeddings.f32").write_bytes(matrix.tobytes())
        sidecar = {
            "format": "scalar-geometry/shard/1",
            "model": config.model,
            "model_revision": config.revision,
            "layer": layer,
            "n_layers": n_layers,
            "dim": dim,
            "pooling": config.pooling,
            "max_length": config.max_length,
            "n_rows": len(rows),
            "n_background": len(background),
            "rows": [
                {
                    "scale_id": row.scale_id,
                    "rank": row.rank,
                    "condition": row.condition,
                    "context": row.context,
                }
                for row in rows
            ],
        }
        (layer_dir / "shard.json").write_text(
            json.dumps(sidecar, indent=2) + "\n", encoding="utf-8"
        )
        written.append(layer_dir)
    return written
