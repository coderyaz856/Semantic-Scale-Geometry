"""The ``sg`` command line.

Four subcommands:

``plan``     count the work before any of it runs or costs anything
``synth``    write a shard of synthetic vectors, no model and no GPU needed
``extract``  embed the scales with a real model and write one shard per layer
``measure``  run the Rust kernel over a shard

``synth`` exists so the whole interchange path, the binary layout, the sidecar,
the grouping and the statistics, can be exercised end to end with a planted
gradient and with pure noise, using nothing but the standard library. A format
bug then surfaces in a second rather than after a model download.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import subprocess
import sys
from array import array
from pathlib import Path

from .scales import enumerate_rows, load_scales, summarize
from .seeds import derive_seed

DEFAULT_SCALES = "data/scales/scales.yaml"
SYNTH_MODEL = "synthetic"


# ------------------------------------------------------------------- plan


def _conditions(scale) -> int:
    return 1 if scale.is_utterance else 2


def cmd_plan(args: argparse.Namespace) -> int:
    scales = load_scales(args.scales)
    summary = summarize(scales)
    removals = [r for r in args.removals.split(",") if r.strip()]
    rows_per_model = summary["rows"]
    texts_per_model = rows_per_model + args.background
    cells_per_shard = sum(_conditions(s) for s in scales) * len(removals)
    shards = args.models * args.layers
    print(f"scales                {summary['scales']}")
    print(f"items                 {summary['items']}")
    print(f"rows per model        {rows_per_model}")
    print(f"background rows       {args.background}")
    print(f"texts per model       {texts_per_model}")
    print(f"models x layers       {args.models} x {args.layers} = {shards} shards")
    print(f"cells per shard       {cells_per_shard}")
    print(f"cells total           {cells_per_shard * shards}")
    print(f"texts total           {texts_per_model * args.models}")
    if args.usd_per_gpu_hour is not None and args.texts_per_second is not None:
        hours = texts_per_model * args.models / args.texts_per_second / 3600.0
        print(f"extraction hours      {hours:.2f}   (scenario, not a budget)")
        print(f"extraction spend      {hours * args.usd_per_gpu_hour:.2f} USD")
        print()
        print("  SCENARIO ARITHMETIC. One throughput rate is applied to every")
        print("  model, and models of different sizes do not share a rate. A rate")
        print("  measured on one device combined with another device's hourly")
        print("  price bounds nothing in either direction. Measurement time and")
        print("  infrastructure cost are not included. For a real budget, measure")
        print("  a rate per model on the hardware that will run it.")
    else:
        print(
            "extraction spend      not estimated; pass --usd-per-gpu-hour and "
            "--texts-per-second, both measured rather than assumed"
        )
    return 0


# ------------------------------------------------------------------ synth


def _unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


def _shared_basis(dim: int, count: int, master: int) -> list[list[float]]:
    """An orthonormal basis the scale directions are drawn from.

    Used to plant a known shared dimensionality, so the subspace analysis can
    be checked against a ground truth instead of being trusted on sight.
    """
    r = random.Random(derive_seed(master, "shared_basis") % (2**32))
    basis: list[list[float]] = []
    for _ in range(count):
        v = [r.gauss(0.0, 1.0) for _ in range(dim)]
        for previous in basis:  # Gram-Schmidt, so the planted rank is exact
            projection = sum(a * b for a, b in zip(v, previous, strict=True))
            v = [a - projection * b for a, b in zip(v, previous, strict=True)]
        basis.append(_unit(v))
    return basis


def _synth_rows(
    rows,
    dim: int,
    signal: float,
    noise: float,
    master: int,
    align: float,
    shared_dims: int = 0,
):
    """One vector per row, with a planted gradient along the gold rank.

    Each scale gets its own random direction and offset, so a correct kernel
    recovers every scale independently. The noise is per row and derived from
    the master seed, so the whole shard is reproducible.

    ``align`` tilts the planted direction towards axis 0, which is the axis the
    synthetic background is made to dominate. At 0 the gradient is independent
    of the background, so removing components should leave the result alone; at
    1 the gradient lies along the dominant axis, so removing it should destroy
    the result. Without that second case, a passing anisotropy control only
    shows removal is harmless, never that it does anything.

    ``shared_dims`` confines every scale's direction to one subspace of that
    rank, which is the ground truth the shared-subspace analysis has to
    recover. At 0 each scale gets an independent direction, which is the
    matched negative case.
    """
    basis = _shared_basis(dim, shared_dims, master) if shared_dims > 0 else []
    per_scale: dict[str, tuple[list[float], list[float]]] = {}
    out: list[list[float]] = []
    for row in rows:
        if row.scale_id not in per_scale:
            r = random.Random(derive_seed(master, "direction", row.scale_id) % (2**32))
            if basis:
                direction = [0.0] * dim
                for vector in basis:
                    weight = r.gauss(0.0, 1.0)
                    direction = [
                        a + weight * b
                        for a, b in zip(direction, vector, strict=True)
                    ]
                direction = _unit(direction)
            else:
                direction = _unit([r.gauss(0.0, 1.0) for _ in range(dim)])
            if align > 0.0:
                direction[0] += align / max(1.0 - align, 1e-6)
                direction = _unit(direction)
            per_scale[row.scale_id] = (
                direction,
                [r.gauss(0.0, 1.0) for _ in range(dim)],
            )
        direction, base = per_scale[row.scale_id]
        r = random.Random(
            derive_seed(master, row.scale_id, row.condition, row.context, str(row.rank))
            % (2**32)
        )
        out.append(
            [
                base[j] + signal * row.rank * direction[j] + noise * r.gauss(0.0, 1.0)
                for j in range(dim)
            ]
        )
    return out


def _synth_background(count: int, dim: int, master: int, dominance: float):
    """Background vectors with one deliberately dominant axis.

    Real embedding spaces are anisotropic, so a synthetic background without a
    dominant direction would make component removal a no-op and the control
    untested.
    """
    r = random.Random(derive_seed(master, "background") % (2**32))
    rows = []
    for _ in range(count):
        v = [r.gauss(0.0, 1.0) for _ in range(dim)]
        v[0] += dominance * r.gauss(0.0, 1.0)
        rows.append(v)
    return rows


def cmd_synth(args: argparse.Namespace) -> int:
    scales = load_scales(args.scales)
    rows = enumerate_rows(scales)
    vectors = _synth_rows(
        rows,
        args.dim,
        args.signal,
        args.noise,
        args.seed,
        args.align,
        args.shared_dims,
    )
    background = _synth_background(args.background, args.dim, args.seed, args.dominance)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = array("f")
    for vector in vectors:
        payload.extend(vector)
    for vector in background:
        payload.extend(vector)
    if sys.byteorder != "little":
        payload.byteswap()
    (out_dir / "embeddings.f32").write_bytes(payload.tobytes())

    sidecar = {
        "format": "scalar-geometry/shard/1",
        "model": SYNTH_MODEL,
        "layer": 0,
        "n_layers": 1,
        "dim": args.dim,
        "pooling": "synthetic",
        "max_length": 0,
        "n_rows": len(rows),
        "n_background": len(background),
        "synthetic": {
            "signal": args.signal,
            "noise": args.noise,
            "dominance": args.dominance,
            "align": args.align,
            "shared_dims": args.shared_dims,
            "seed": args.seed,
        },
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
    (out_dir / "shard.json").write_text(
        json.dumps(sidecar, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"wrote {len(rows)} rows + {len(background)} background "
        f"at dim {args.dim} to {out_dir}"
    )
    return 0


# ---------------------------------------------------------------- extract


def cmd_extract(args: argparse.Namespace) -> int:
    # Imported here so that plan, synth and measure work without torch
    # installed, which keeps the fast path free of a two gigabyte dependency.
    from .extract import ExtractionConfig, load_background, write_shards

    scales = load_scales(args.scales)
    rows = enumerate_rows(scales)
    background = load_background(args.background) if args.background else []
    config = ExtractionConfig(
        model=args.model,
        max_length=args.max_length,
        batch_size=args.batch_size,
        device=args.device,
        pooling=args.pooling,
    )
    written = write_shards(args.out, rows, background, config)
    print(f"wrote {len(written)} layer shards to {args.out}")
    return 0


# ---------------------------------------------------------------- measure


def _resolve_binary(path: str) -> str:
    """Accept the Unix spelling of the kernel path on Windows too."""
    candidate = Path(path)
    if not candidate.exists() and candidate.with_suffix(".exe").exists():
        return str(candidate.with_suffix(".exe"))
    return path


def cmd_measure(args: argparse.Namespace) -> int:
    command = [
        _resolve_binary(args.binary),
        "--shard",
        str(args.shard),
        "--out",
        str(args.out),
        "--remove-top",
        args.removals,
        "--random-draws",
        str(args.random_draws),
        "--bootstrap-draws",
        str(args.bootstrap_draws),
        "--seed",
        str(args.seed),
    ]
    if args.directions:
        command += [
            "--directions",
            str(args.directions),
            "--null-replicates",
            str(args.null_replicates),
        ]
    try:
        completed = subprocess.run(command, check=False)
    except FileNotFoundError:
        print(
            f"sg: kernel binary not found at {args.binary}.\n"
            "    Build it with: cargo build --release "
            "--manifest-path kernel/Cargo.toml",
            file=sys.stderr,
        )
        return 2
    return completed.returncode


# -------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sg", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="count the work before running it")
    plan.add_argument("--scales", default=DEFAULT_SCALES)
    plan.add_argument("--models", type=int, default=1)
    plan.add_argument("--layers", type=int, default=13)
    plan.add_argument("--background", type=int, default=2000)
    plan.add_argument("--removals", default="0,1,3,10")
    plan.add_argument("--usd-per-gpu-hour", type=float, default=None)
    plan.add_argument("--texts-per-second", type=float, default=None)
    plan.set_defaults(func=cmd_plan)

    synth = sub.add_parser("synth", help="write a synthetic shard, no GPU needed")
    synth.add_argument("--scales", default=DEFAULT_SCALES)
    synth.add_argument("--out", required=True)
    synth.add_argument("--dim", type=int, default=64)
    synth.add_argument("--signal", type=float, default=1.0)
    synth.add_argument("--noise", type=float, default=0.3)
    synth.add_argument("--background", type=int, default=600)
    synth.add_argument("--dominance", type=float, default=5.0)
    synth.add_argument(
        "--align",
        type=float,
        default=0.0,
        help="tilt the planted gradient onto the dominant background axis, 0 to 1",
    )
    synth.add_argument(
        "--shared-dims",
        type=int,
        default=0,
        help="confine every scale direction to a subspace of this rank; "
        "0 gives each scale an independent direction",
    )
    synth.add_argument("--seed", type=int, default=1)
    synth.set_defaults(func=cmd_synth)

    extract = sub.add_parser("extract", help="embed the scales with a real model")
    extract.add_argument("--scales", default=DEFAULT_SCALES)
    extract.add_argument("--model", required=True)
    extract.add_argument("--out", required=True)
    extract.add_argument("--background", default=None)
    extract.add_argument("--max-length", type=int, default=48)
    extract.add_argument("--batch-size", type=int, default=16)
    extract.add_argument("--device", default="cpu")
    extract.add_argument("--pooling", default="mean", choices=["mean", "cls"])
    extract.set_defaults(func=cmd_extract)

    measure = sub.add_parser("measure", help="run the Rust kernel over a shard")
    measure.add_argument("--shard", required=True)
    measure.add_argument("--out", required=True)
    measure.add_argument(
        "--binary", default="kernel/target/release/sg-measure", help="kernel path"
    )
    measure.add_argument("--removals", default="0,1,3,10")
    measure.add_argument("--random-draws", type=int, default=10000)
    measure.add_argument("--bootstrap-draws", type=int, default=2000)
    measure.add_argument("--seed", type=int, default=1)
    measure.add_argument(
        "--directions",
        default=None,
        help="also export fitted, half-split and shuffled-label directions here",
    )
    measure.add_argument("--null-replicates", type=int, default=50)
    measure.set_defaults(func=cmd_measure)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
