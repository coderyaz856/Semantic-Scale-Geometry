"""End-to-end check on synthetic shards.

This exercises the whole path, scale expansion, the binary shard layout, the
sidecar, the grouping, the statistics and the controls, without a model, a GPU
or a network. It is the test that would have caught both of the bugs found
while building this: a context identifier confused with a subject, and a
statistic that pointed the wrong way.

Three shards, three things that must hold:

1. A planted gradient is found in every scale.
2. Pure noise is found in almost none, at about the false positive rate the
   threshold implies.
3. A gradient deliberately aligned with the dominant background axis is
   degraded when that axis is removed. Without this case a passing anisotropy
   control would only show that removal is harmless.

    python tools/check_synthetic.py
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FALSE_POSITIVE_TOLERANCE = 0.20  # at a 0.05 threshold, well above chance


def kernel_binary(name: str, explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit)
    base = REPO / "kernel" / "target" / "release" / name
    exe = base.with_suffix(".exe")
    return exe if exe.exists() else base


def run(command: list[str], env: dict[str, str]) -> None:
    result = subprocess.run(
        command, cwd=REPO, env=env, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(f"command failed: {' '.join(command)}")


def synth_and_measure(
    name: str,
    workdir: Path,
    binary: Path,
    env: dict[str, str],
    *,
    signal: float,
    noise: float,
    align: float,
    seed: int,
    removals: str,
) -> list[dict]:
    shard = workdir / name / "layer_00"
    out = workdir / f"{name}.json"
    run(
        [
            sys.executable, "-m", "scalar_geometry.cli", "synth",
            "--out", str(shard),
            "--dim", "64",
            "--signal", str(signal),
            "--noise", str(noise),
            "--align", str(align),
            "--seed", str(seed),
        ],
        env,
    )
    run(
        [
            str(binary),
            "--shard", str(shard),
            "--out", str(out),
            "--remove-top", removals,
            "--random-draws", "1000",
            "--bootstrap-draws", "300",
            "--seed", "1",
        ],
        env,
    )
    return json.loads(out.read_text(encoding="utf-8"))["results"]


def subspace_case(
    name: str,
    workdir: Path,
    measure: Path,
    subspace: Path,
    env: dict[str, str],
    *,
    shared_dims: int,
    seed: int,
) -> dict:
    """Plant a known shared dimensionality and see whether it is recovered."""
    shard = workdir / name / "layer_00"
    dirs = workdir / f"dirs_{name}"
    out = workdir / f"{name}_subspace.json"
    run(
        [
            sys.executable, "-m", "scalar_geometry.cli", "synth",
            "--out", str(shard),
            "--dim", "128",
            "--signal", "1.0",
            "--noise", "0.4",
            "--shared-dims", str(shared_dims),
            "--seed", str(seed),
        ],
        env,
    )
    run(
        [
            str(measure),
            "--shard", str(shard),
            "--out", str(workdir / f"{name}_results.json"),
            "--remove-top", "0",
            "--random-draws", "100",
            "--bootstrap-draws", "50",
            "--directions", str(dirs),
            "--null-replicates", "50",
        ],
        env,
    )
    run([str(subspace), str(dirs), "--condition", "carrier", "--out", str(out)], env)
    groups = json.loads(out.read_text(encoding="utf-8"))
    assert len(groups) == 1, f"expected one group, got {len(groups)}"
    return groups[0]


def at(results: list[dict], remove_top: int) -> list[dict]:
    return [r for r in results if r["remove_top"] == remove_top]


def share_significant(results: list[dict]) -> float:
    return sum(1 for r in results if r["p_permutation"] < 0.05) / len(results)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", default=None)
    args = parser.parse_args(argv)

    binary = kernel_binary("sg-measure", args.binary)
    subspace_binary = kernel_binary("sg-subspace")
    if not binary.exists():
        print(
            f"kernel binary not found at {binary}\n"
            "build it with: cargo build --release --manifest-path kernel/Cargo.toml",
            file=sys.stderr,
        )
        return 2

    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "python")
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)

        signal = synth_and_measure(
            "signal", workdir, binary, env,
            signal=1.0, noise=0.4, align=0.0, seed=1, removals="0",
        )
        cells = at(signal, 0)
        loo = statistics.median(r["spearman_loo"] for r in cells)
        found = share_significant(cells)
        print(f"planted gradient : loo median {loo:+.3f}, significant {found:.0%}")
        if loo < 0.9:
            failures.append(f"planted gradient not recovered: loo median {loo:+.3f}")
        if found < 0.95:
            failures.append(f"planted gradient missed in cells: only {found:.0%} found")

        noise = synth_and_measure(
            "noise", workdir, binary, env,
            signal=0.0, noise=1.0, align=0.0, seed=2, removals="0",
        )
        cells = at(noise, 0)
        p_median = statistics.median(r["p_permutation"] for r in cells)
        false_rate = share_significant(cells)
        print(
            f"pure noise       : p median {p_median:.3f}, "
            f"significant {false_rate:.0%}"
        )
        if p_median < 0.2:
            failures.append(f"noise looks significant: p median {p_median:.3f}")
        if false_rate > FALSE_POSITIVE_TOLERANCE:
            failures.append(f"too many false positives in noise: {false_rate:.0%}")

        aligned = synth_and_measure(
            "aligned", workdir, binary, env,
            signal=1.0, noise=0.4, align=0.9, seed=3, removals="0,1",
        )
        before, after = at(aligned, 0), at(aligned, 1)
        share_before = statistics.median(r["variance_share"] for r in before)
        share_after = statistics.median(r["variance_share"] for r in after)
        found_before = share_significant(before)
        found_after = share_significant(after)
        print(
            f"aligned gradient : variance share {share_before:.3f} -> "
            f"{share_after:.3f}, "
            f"significant {found_before:.0%} -> {found_after:.0%}"
        )
        if share_after >= share_before * 0.75:
            failures.append(
                "removing the dominant component barely changed the variance share "
                f"({share_before:.3f} -> {share_after:.3f}); the anisotropy control "
                "is not doing anything"
            )
        if found_after >= found_before:
            failures.append(
                "removing the dominant component did not reduce the significant "
                f"share ({found_before:.0%} -> {found_after:.0%})"
            )

        # The subspace analysis is checked against ground truth before it is
        # trusted on real directions: a planted rank must be recovered, and
        # independent directions must not look shared.
        if subspace_binary.exists():
            planted = 3
            shared = subspace_case(
                "shared", workdir, binary, subspace_binary, env,
                shared_dims=planted, seed=9,
            )
            independent = subspace_case(
                "independent", workdir, binary, subspace_binary, env,
                shared_dims=0, seed=9,
            )
            pr_shared = shared["observed"]["participation_ratio"]
            pr_indep = independent["observed"]["participation_ratio"]
            n = shared["observed"]["n_directions"]
            print(
                f"planted rank {planted}   : pr {pr_shared:.2f} "
                f"(null {shared['null_participation_mean']:.2f}, "
                f"p {shared['p_participation']:.3f})"
            )
            print(
                f"independent dirs : pr {pr_indep:.2f} "
                f"(null {independent['null_participation_mean']:.2f}, "
                f"p {independent['p_participation']:.3f})"
            )
            if not planted - 1.0 <= pr_shared <= planted + 2.0:
                failures.append(
                    f"planted rank {planted} not recovered: pr {pr_shared:.2f}"
                )
            if shared["p_participation"] > 0.1:
                failures.append(
                    "planted sharing not distinguished from the matched null: "
                    f"p {shared['p_participation']:.3f}"
                )
            if pr_indep < n * 0.6:
                failures.append(
                    f"independent directions looked shared: pr {pr_indep:.2f} "
                    f"with {n} directions"
                )
            if independent["p_participation"] < 0.1:
                failures.append(
                    "independent directions were called shared: "
                    f"p {independent['p_participation']:.3f}"
                )
            for case, label in ((shared, "shared"), (independent, "independent")):
                cosine = case["half_split_cosine_median"]
                if cosine is None or cosine < 0.5:
                    failures.append(
                        f"{label}: directions unstable across context halves, "
                        f"median |cos| {cosine}"
                    )
        else:
            print("sg-subspace not built; skipping the subspace checks")

        # Structural guarantees, independent of the statistics.
        every = signal + noise + aligned
        if not all(r["permutation_exact"] for r in every):
            failures.append("a permutation test fell back to sampling")
        if any(
            r["n_contexts"] == 1 and r["spearman_loo_ci_low"] is not None
            for r in every
        ):
            failures.append("an interval was invented for a single context")
        if any(
            r["n_contexts"] > 1 and r["spearman_loo_ci_low"] is None for r in every
        ):
            failures.append("a multi-context cell is missing its interval")

    if failures:
        print()
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print("\nall synthetic checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
