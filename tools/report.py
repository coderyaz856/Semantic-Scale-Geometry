"""Summarise measurement results.

Reads the per-shard JSON files the kernel writes, joins them back to the scale
metadata, and prints the tables the README is built from. Every figure here
comes from a file on disk; nothing is recomputed or smoothed.

Significance is reported twice, unadjusted and after Benjamini-Hochberg across
the scales in one layer and one preprocessing setting. The unadjusted count is
the weaker number and is kept only so the two can be compared.

    python tools/report.py results/minilm_layer*.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "python"))

from scalar_geometry.scales import load_scales  # noqa: E402

ALPHA = 0.05


def median(values: list[float]) -> float:
    return statistics.median(values) if values else float("nan")


def bh_survivors(rows: list[dict], alpha: float = ALPHA) -> set[str]:
    """Benjamini-Hochberg over one family of tests.

    The family is the scales measured at one layer, in one preprocessing
    setting, in one condition group. Two warnings go with every number this
    produces. If a layer or a setting was chosen after looking at results, the
    real family is larger than this and the survivors are optimistic. And a
    scale that does not survive has not been shown to lack a gradient; with six
    points per scale the test has little power to begin with.
    """
    ordered = sorted(rows, key=lambda r: r["p_permutation"])
    m = len(ordered)
    cutoff = 0
    for index, row in enumerate(ordered, start=1):
        if row["p_permutation"] <= index / m * alpha:
            cutoff = index
    return {r["scale_id"] for r in ordered[:cutoff]}


def load(paths: list[str]) -> list[dict]:
    out = []
    for path in paths:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        for result in doc["results"]:
            result["model"] = doc["model"]
            result["layer"] = doc["layer"]
            out.append(result)
    return out


def table(title: str, header: str, lines: list[str]) -> None:
    print(f"\n{title}")
    print(header)
    print("-" * len(header))
    for line in lines:
        print(line)


def select(rows, layer, removal, contextual_only=True, condition=None):
    out = []
    for r in rows:
        if r["layer"] != layer or r["remove_top"] != removal:
            continue
        if condition is not None and r["condition"] != condition:
            continue
        if condition is None and contextual_only and r["condition"] == "bare":
            continue
        out.append(r)
    return out


def summarise(rows: list[dict], survivors: set[str]) -> tuple[float, ...]:
    if not rows:
        return (float("nan"), float("nan"), 0.0, 0, 0)
    raw = sum(1 for r in rows if r["p_permutation"] < ALPHA)
    kept = sum(1 for r in rows if r["scale_id"] in survivors)
    return (
        median([r["spearman_loo"] for r in rows]),
        median([r["variance_share"] for r in rows]),
        float(len(rows)),
        raw,
        kept,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", nargs="+")
    parser.add_argument("--scales", default=str(REPO / "data/scales/scales.yaml"))
    parser.add_argument("--remove-top", type=int, default=0)
    args = parser.parse_args(argv)

    rows = load(args.results)
    meta = {s.id: s for s in load_scales(args.scales)}
    layers = sorted({r["layer"] for r in rows})
    removals = sorted({r["remove_top"] for r in rows})
    keep = args.remove_top
    print(f"model(s): {', '.join(sorted({r['model'] for r in rows}))}")
    print(f"layers:   {layers}")
    print(f"cells:    {len(rows)}")
    print(f"alpha:    {ALPHA} (BH applied across scales within a layer/setting)")

    # Survivor sets are computed once per family and reused, because applying
    # BH inside a three-scale subgroup would not be a family at all.
    survivors = {
        (layer, removal): bh_survivors(select(rows, layer, removal))
        for layer in layers
        for removal in removals
    }

    # ---- by layer ---------------------------------------------------------
    lines = []
    for layer in layers:
        sel = select(rows, layer, keep)
        loo, share, n, raw, kept = summarise(sel, survivors[(layer, keep)])
        spread = median([r["per_context_loo_sd"] or 0.0 for r in sel])
        lines.append(
            f"{layer:5d}  {loo:+8.3f}  {share:10.3f}  {raw:3.0f}/{n:<3.0f}"
            f"  {kept:3.0f}/{n:<3.0f}  {spread:8.3f}"
        )
    table(
        f"By layer (contextual, remove_top={keep})",
        "layer       loo   var share   raw p<.05   after BH    ctx sd",
        lines,
    )

    last = layers[-1]
    family = survivors[(last, keep)]

    # ---- by condition -----------------------------------------------------
    lines = []
    for condition in ("bare", "carrier", "utterance"):
        sel = select(rows, last, keep, condition=condition)
        if not sel:
            continue
        loo, share, n, raw, kept = summarise(sel, family)
        ids = sorted({r["scale_id"] for r in sel})
        note = (
            "same 19 scales"
            if len(ids) == 19
            else f"{len(ids)} scales: {', '.join(ids)}"
        )
        lines.append(
            f"{condition:<10}  {n:5.0f}  {loo:+8.3f}  {share:10.3f}"
            f"  {raw:3.0f}/{n:<3.0f}   {note}"
        )
    table(
        f"By condition (layer {last}, remove_top={keep})",
        "condition   cells       loo   var share   raw p<.05   coverage",
        lines,
    )
    print(
        "  Bare and carrier cover the same scales, so that contrast is paired.\n"
        "  Utterance covers three different scales and is not comparable to them."
    )

    # ---- scale properties as factors --------------------------------------
    for field, label in (
        ("polarity", "Polarity"),
        ("unit", "Unit"),
        ("gold_confidence", "Gold confidence"),
        ("family", "Family"),
    ):
        groups: dict[str, list[dict]] = {}
        for r in select(rows, last, keep):
            groups.setdefault(getattr(meta[r["scale_id"]], field), []).append(r)
        lines = []
        for key, sel in sorted(groups.items()):
            loo, share, n, raw, kept = summarise(sel, family)
            lines.append(
                f"{key:<20}  {n:5.0f}  {loo:+8.3f}  {share:10.3f}"
                f"  {raw:3.0f}/{n:<3.0f}  {kept:3.0f}/{n:<3.0f}"
            )
        table(
            f"{label} (layer {last}, contextual, remove_top={keep})",
            "group                 cells       loo   var share   raw p<.05   after BH",
            lines,
        )

    # ---- anisotropy, in aggregate and per scale ---------------------------
    lines = []
    for removal in removals:
        sel = select(rows, last, removal)
        loo, share, n, raw, kept = summarise(sel, survivors[(last, removal)])
        lines.append(
            f"{removal:10d}  {loo:+8.3f}  {share:10.3f}  {raw:3.0f}/{n:<3.0f}"
            f"  {kept:3.0f}/{n:<3.0f}"
        )
    table(
        f"Anisotropy control, aggregate (layer {last}, contextual)",
        "remove_top       loo   var share   raw p<.05   after BH",
        lines,
    )

    # An unchanged median does not mean nothing moved, so the per-scale
    # movement is printed rather than summarised away.
    if len(removals) > 1:
        first, highest = removals[0], removals[-1]
        base = {r["scale_id"]: r for r in select(rows, last, first)}
        after = {r["scale_id"]: r for r in select(rows, last, highest)}
        lines = []
        for sid in sorted(base):
            a, b = base[sid], after[sid]
            delta = b["spearman_loo"] - a["spearman_loo"]
            crossed = (a["p_permutation"] < ALPHA) != (b["p_permutation"] < ALPHA)
            if abs(delta) > 0.05 or crossed:
                flag = "  crosses alpha" if crossed else ""
                lines.append(
                    f"{sid:<14}  {a['spearman_loo']:+7.3f} -> {b['spearman_loo']:+7.3f}"
                    f"  ({delta:+.3f}){flag}"
                )
        deltas = [
            abs(after[s]["spearman_loo"] - base[s]["spearman_loo"]) for s in base
        ]
        table(
            f"Anisotropy control, per scale (remove_top {first} -> {highest})",
            "scale                loo change",
            lines or ["(no scale moved by more than 0.05)"],
        )
        print(
            f"  median |change| {median(deltas):.3f}, max {max(deltas):.3f}; "
            "an unchanged median does not mean individual scales held still."
        )

    # ---- every scale ------------------------------------------------------
    lines = []
    for r in sorted(select(rows, last, keep), key=lambda r: -r["spearman_loo"]):
        low, high = r["spearman_loo_ci_low"], r["spearman_loo_ci_high"]
        interval = f"[{low:+.2f}, {high:+.2f}]" if low is not None else "     none     "
        mark = "BH" if r["scale_id"] in family else "  "
        lines.append(
            f"{r['scale_id']:<14} {meta[r['scale_id']].family:<19} "
            f"{r['spearman_in_sample']:+7.3f} {r['spearman_loo']:+7.3f}  {interval}"
            f"  {r['variance_share']:6.3f} {r.get('pc1_share', float('nan')):6.3f}"
            f"  {r['p_permutation']:8.4f} {mark}"
        )
    table(
        f"Every scale (layer {last}, contextual, remove_top={keep})",
        "scale          family              in-samp     loo  95% interval"
        "    sup'd    pc1    p_perm  BH",
        lines,
    )
    print(
        "  sup'd is variance along the rank-supervised direction; pc1 is\n"
        "  variance along the items own leading component, fitted without the\n"
        "  ranks. sup'd is bounded above by pc1. A large pc1 with a much\n"
        "  smaller sup'd means the items do lie along an axis, but not the\n"
        "  one the ranks pick out.\n"
        "  in-samp is the fitted direction scored on the same items it was fitted\n"
        "  on. A large gap between it and loo means the fitted ordering does not\n"
        "  generalize to a withheld item, which is a different claim from the\n"
        "  model storing a reversed axis."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
