"""Versioned experiment configuration for production runs."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

FORMAT = "semantic-scale-geometry/experiment/1"
REVISION = re.compile(r"^[0-9a-fA-F]{40}$")


class ConfigError(ValueError):
    """Raised when an experiment configuration violates its contract."""


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be a mapping")
    if not all(isinstance(key, str) for key in value):
        raise ConfigError(f"{label} keys must be strings")
    return value


def _only(values: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ConfigError(f"{label} has unknown field(s): {', '.join(unknown)}")


def _positive_int(value: object, label: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{label} must be an integer >= {minimum}")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True)
class ModelConfig:
    model_id: str
    revision: str
    device: str
    pooling: str
    max_length: int
    batch_size: int


@dataclass(frozen=True)
class AnalysisConfig:
    removals: tuple[int, ...]
    random_draws: int
    bootstrap_draws: int
    seed: int
    null_replicates: int
    subspace_condition: str


@dataclass(frozen=True)
class ExecutionConfig:
    kernel_binary: str
    subspace_binary: str
    report_script: str


@dataclass(frozen=True)
class ExperimentConfig:
    source_path: Path
    scales_path: Path
    background_path: Path
    model: ModelConfig
    analysis: AnalysisConfig
    execution: ExecutionConfig
    normalized: dict[str, Any]

    def canonical_json(self) -> str:
        return json.dumps(self.normalized, sort_keys=True, separators=(",", ":"))


def _resolve_input(base: Path, value: object, label: str) -> Path:
    path = Path(_string(value, label)).expanduser()
    if not path.is_absolute():
        path = base / path
    path = path.resolve()
    if not path.is_file():
        raise ConfigError(f"{label} does not exist or is not a file: {path}")
    return path


def load(path: str | Path) -> ExperimentConfig:
    """Load and strictly validate one versioned experiment configuration."""
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise ConfigError(f"configuration does not exist: {source}")
    try:
        decoded = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ConfigError(f"could not read configuration: {error}") from error

    root = _mapping(decoded, "configuration")
    _only(
        root,
        {"format", "scales", "background", "model", "analysis", "execution"},
        "configuration",
    )
    if root.get("format") != FORMAT:
        raise ConfigError(f"format must be {FORMAT!r}")

    base = source.parent
    scales_path = _resolve_input(base, root.get("scales"), "scales")
    background_path = _resolve_input(base, root.get("background"), "background")

    model_values = _mapping(root.get("model"), "model")
    _only(
        model_values,
        {"id", "revision", "device", "pooling", "max_length", "batch_size"},
        "model",
    )
    model_id = _string(model_values.get("id"), "model.id")
    revision = _string(model_values.get("revision"), "model.revision")
    if not REVISION.fullmatch(revision):
        raise ConfigError("model.revision must be a 40-character commit SHA")
    device = _string(model_values.get("device", "cpu"), "model.device")
    pooling = _string(model_values.get("pooling", "mean"), "model.pooling")
    if pooling not in {"mean", "cls"}:
        raise ConfigError("model.pooling must be 'mean' or 'cls'")
    model = ModelConfig(
        model_id=model_id,
        revision=revision.lower(),
        device=device,
        pooling=pooling,
        max_length=_positive_int(
            model_values.get("max_length", 48), "model.max_length"
        ),
        batch_size=_positive_int(
            model_values.get("batch_size", 16), "model.batch_size"
        ),
    )

    analysis_values = _mapping(root.get("analysis", {}), "analysis")
    _only(
        analysis_values,
        {
            "removals",
            "random_draws",
            "bootstrap_draws",
            "seed",
            "null_replicates",
            "subspace_condition",
        },
        "analysis",
    )
    removal_values = analysis_values.get("removals", [0, 1, 3])
    if not isinstance(removal_values, list) or not removal_values:
        raise ConfigError("analysis.removals must be a non-empty list")
    removals = tuple(
        _positive_int(item, "analysis.removals[]", minimum=0) for item in removal_values
    )
    if len(set(removals)) != len(removals):
        raise ConfigError("analysis.removals must not contain duplicates")
    seed = analysis_values.get("seed", 1)
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ConfigError("analysis.seed must be a non-negative integer")
    analysis = AnalysisConfig(
        removals=removals,
        random_draws=_positive_int(
            analysis_values.get("random_draws", 10_000), "analysis.random_draws"
        ),
        bootstrap_draws=_positive_int(
            analysis_values.get("bootstrap_draws", 2_000),
            "analysis.bootstrap_draws",
        ),
        seed=seed,
        null_replicates=_positive_int(
            analysis_values.get("null_replicates", 50),
            "analysis.null_replicates",
        ),
        subspace_condition=_string(
            analysis_values.get("subspace_condition", "carrier"),
            "analysis.subspace_condition",
        ),
    )

    execution_values = _mapping(root.get("execution", {}), "execution")
    _only(
        execution_values,
        {"kernel_binary", "subspace_binary", "report_script"},
        "execution",
    )
    execution = ExecutionConfig(
        kernel_binary=_string(
            execution_values.get("kernel_binary", "sg-measure"),
            "execution.kernel_binary",
        ),
        subspace_binary=_string(
            execution_values.get("subspace_binary", "sg-subspace"),
            "execution.subspace_binary",
        ),
        report_script=_string(
            execution_values.get("report_script", "tools/report.py"),
            "execution.report_script",
        ),
    )

    normalized = {
        "format": FORMAT,
        "scales": str(root["scales"]),
        "background": str(root["background"]),
        "model": {
            "id": model.model_id,
            "revision": model.revision,
            "device": model.device,
            "pooling": model.pooling,
            "max_length": model.max_length,
            "batch_size": model.batch_size,
        },
        "analysis": {
            **asdict(analysis),
            "removals": list(analysis.removals),
        },
        "execution": asdict(execution),
    }
    return ExperimentConfig(
        source_path=source,
        scales_path=scales_path,
        background_path=background_path,
        model=model,
        analysis=analysis,
        execution=execution,
        normalized=normalized,
    )
