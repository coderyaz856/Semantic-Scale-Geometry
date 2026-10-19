"""Turning result documents into ledger rows.

Two rules shape this module. The ledger is an index, so a row is metadata and a
handful of statistics, never a payload: embeddings, exported directions and
rendered reports stay in the bucket. And the translation is a rename, not a
reinterpretation, with one exception that is called out below.

The exception is ``significant``. The schema asks for a decision and the
measurement files contain only p-values, so the decision is made here, with the
same family definition the repository's own report uses: the scales measured at
one layer, under one preprocessing setting, in one condition *group*. Carrier
frames and utterances form the contextual group, every scale once; bare lexical
items form their own. The unit is the group, not the single condition: three
utterance scales corrected among themselves would face a threshold seven times
more lenient than the same scales corrected with the rest.

Two warnings travel with the decision. If the layer or the setting was chosen
after looking at results, the real family is larger than this one and the
survivors are optimistic. And a scale that does not survive has not been shown
to lack a gradient: with five or six items per scale the exact permutation test
has a floor of 1/k!, which leaves it little power to begin with.

Where a measurement document already carries an explicit ``significant`` field,
that field wins, so the day the runtime decides this itself the adapter stops
deciding.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .runs import RunDirectory, normalize_timestamp

DEFAULT_FDR_ALPHA = 0.05

#: Ledger column -> key in a measurement result object.
MEASUREMENT_MAP = {
    "in_sample_order": "spearman_in_sample",
    "leave_one_out_order": "spearman_loo",
    "supervised_variance_share": "variance_share",
    "unsupervised_pc1_share": "pc1_share",
    "permutation_p": "p_permutation",
    # Dispersion of the held-out statistic across carrier contexts. Lower is
    # more stable; the column name says stability, so the direction of the
    # scale is documented in the schema and repeated here.
    "context_stability": "per_context_loo_sd",
}

#: Ledger column -> key in a subspace group object, outside `observed`.
SUBSPACE_GROUP_MAP = {
    "matched_null_dimensions": "null_participation_mean",
    "half_split_cosine": "half_split_cosine_median",
    "permutation_p": "p_participation",
}

#: Ledger column -> key inside a subspace group's `observed` object.
SUBSPACE_OBSERVED_MAP = {
    "n_directions": "n_directions",
    "effective_dimensions": "participation_ratio",
    "dimensions_90": "components_90",
    "dimensions_99": "components_99",
    "mean_absolute_cosine": "mean_abs_cosine",
}

#: Counts, not measurements: these three are integers in the schema.
SUBSPACE_INTEGER_COLUMNS = frozenset({"n_directions", "dimensions_90", "dimensions_99"})

MEASUREMENT_KEY = (
    "run_id",
    "model_id",
    "layer",
    "scale_id",
    "condition",
    "remove_top",
)

SUBSPACE_KEY = ("run_id", "model_id", "layer", "condition", "remove_top")

#: The condition measured without context. Every other condition belongs to the
#: contextual group, which is the family tools/report.py corrects over.
BARE_CONDITION = "bare"


def condition_group(condition: str | None) -> str:
    """The multiple-testing group a condition belongs to."""
    return "bare" if condition == BARE_CONDITION else "contextual"


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def run_row(run: RunDirectory, artifact_uri: str | None) -> dict:
    """The single `runs` row for this run."""
    duration = _number(run.status_field("duration_seconds"))
    if duration is None:
        duration = _number(run.manifest_field("duration_seconds"))

    return {
        "run_id": run.run_id,
        "state": run.state,
        "created_at": normalize_timestamp(run.status_field("created_at")),
        "completed_at": normalize_timestamp(run.status_field("completed_at")),
        "git_commit": run.manifest_field("git_commit"),
        "config_digest": run.manifest_field("config_digest"),
        "model_id": run.manifest_field("model_id"),
        "model_revision": run.manifest_field("model_revision"),
        "scales_digest": run.manifest_field("scales_digest"),
        "background_digest": run.manifest_field("background_digest"),
        "container_digest": run.manifest_field("container_digest"),
        "device": run.manifest_field("device"),
        "duration_seconds": duration,
        "artifact_uri": artifact_uri,
        "error_code": run.status_field("error_code"),
    }


def benjamini_hochberg(p_values: Iterable[float], alpha: float) -> int:
    """How many of the sorted p-values survive at level `alpha`.

    Returns the Benjamini-Hochberg cutoff index: the largest rank whose
    p-value is at or below rank / m * alpha. Zero means none survive.
    """
    ordered = sorted(p for p in p_values if p is not None)
    m = len(ordered)
    if m == 0:
        return 0
    cutoff = 0
    for index, p in enumerate(ordered, start=1):
        if p <= index / m * alpha:
            cutoff = index
    return cutoff


def _apply_significance(rows: list[dict], alpha: float) -> None:
    """Fill `significant` in place, per family, leaving explicit values alone."""
    families: dict[tuple, list[dict]] = {}
    for row in rows:
        if row.get("significant") is not None:
            continue
        family = (
            row["run_id"],
            row["model_id"],
            row["layer"],
            condition_group(row["condition"]),
            row["remove_top"],
        )
        families.setdefault(family, []).append(row)

    for members in families.values():
        candidates = [row for row in members if row["permutation_p"] is not None]
        cutoff = benjamini_hochberg((row["permutation_p"] for row in candidates), alpha)
        if cutoff == 0:
            threshold = None
        else:
            threshold = sorted(row["permutation_p"] for row in candidates)[cutoff - 1]
        for row in members:
            p = row["permutation_p"]
            row["significant"] = (
                None if p is None else (threshold is not None and p <= threshold)
            )


def measurement_rows(
    run: RunDirectory,
    documents: Iterable[tuple[str, dict]],
    alpha: float = DEFAULT_FDR_ALPHA,
) -> list[dict]:
    """Rows for `scale_measurements`, with significance decided per family."""
    rows: list[dict] = []
    for _source, document in documents:
        model_id = document.get("model")
        layer = _integer(document.get("layer"))
        for result in document["results"]:
            row = {
                "run_id": run.run_id,
                "model_id": model_id,
                "layer": layer,
                "scale_id": result.get("scale_id"),
                "condition": result.get("condition"),
                "remove_top": _integer(result.get("remove_top")),
                "significant": result.get("significant"),
            }
            for column, key in MEASUREMENT_MAP.items():
                row[column] = _number(result.get(key))
            rows.append(row)

    _apply_significance(rows, alpha)
    return rows


def subspace_rows(
    run: RunDirectory, documents: Iterable[tuple[str, list]]
) -> list[dict]:
    """Rows for `subspace_summaries`.

    `dimension_gap` is computed rather than read, because it is the number the
    result is usually quoted as and a stored difference cannot disagree with
    its own operands. Positive means the fitted directions occupy fewer
    dimensions than the matched null.
    """
    rows: list[dict] = []
    for _source, document in documents:
        for group in document:
            observed = group.get("observed") or {}
            row = {
                "run_id": run.run_id,
                "model_id": group.get("model"),
                "layer": _integer(group.get("layer")),
                "condition": group.get("condition"),
                "remove_top": _integer(group.get("remove_top")),
            }
            for column, key in SUBSPACE_GROUP_MAP.items():
                row[column] = _number(group.get(key))
            for column, key in SUBSPACE_OBSERVED_MAP.items():
                value = observed.get(key)
                row[column] = (
                    _integer(value)
                    if column in SUBSPACE_INTEGER_COLUMNS
                    else _number(value)
                )

            effective = row["effective_dimensions"]
            matched = row["matched_null_dimensions"]
            row["dimension_gap"] = (
                None if effective is None or matched is None else matched - effective
            )
            rows.append(row)
    return rows


def key_of(row: dict, key: tuple[str, ...]) -> tuple:
    return tuple(row.get(name) for name in key)


def duplicate_keys(rows: list[dict], key: tuple[str, ...]) -> list[tuple]:
    """Keys appearing more than once.

    A merge keyed on these columns would silently drop rows that share a key,
    so the adapter refuses instead. Two documents covering the same layer is
    the realistic cause, and that is a run worth looking at rather than
    deduplicating.
    """
    seen: set[tuple] = set()
    repeated: list[tuple] = []
    for row in rows:
        identity = key_of(row, key)
        if identity in seen:
            repeated.append(identity)
        else:
            seen.add(identity)
    return repeated
