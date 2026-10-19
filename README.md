# Semantic Scale Geometry

**Does an embedding model know that *scalding* is hotter than *warm*, and is "hotter" a straight line in its space?**

Status: research prototype. The numbers below come from one small model and
should be read as a first pass, not a general finding. Every reported number
was produced by the pipeline in this repository. The committed seeds and
artifacts support independent inspection. Production runs require an exact
model revision and emit versioned manifests, status, logs and output hashes.

## The question

Language is full of ordered scales. Temperature runs *freezing, cold, cool,
warm, hot, scalding*. Quantity runs *none, few, several, many, most, all*.
Certainty, frequency and politeness behave the same way. The ordering is known
in advance, which makes these scales a rare thing in representation research: a
ground truth that does not depend on a model.

An embedding is a point in a few thousand dimensions. So the question becomes
geometric and falsifiable. Is there a direction along which projecting these
items recovers the human ordering, how much of their variation does that single
direction explain, and is the same direction reused across different scales or
does each one get its own?

## What is measured

Three claims that are routinely conflated, measured separately:

1. **Order.** Rank correlation between the projection onto the fitted direction
   and the gold ordering. A model can get this right while the geometry is a
   mess.
2. **Linearity.** The share of variance a single component explains. This is
   what licenses the word "direction", and it is a different question from
   order.
3. **Sharing.** Whether every scale gets its own direction or they share a
   small number. Fit a direction per scale, then ask how many dimensions the
   whole collection actually spans, and whether a direction fitted on one
   scale transfers to another. If twenty-two scales live in a three
   dimensional subspace, that is a claim about how the model encodes degree
   in general, and no single scale could support it.

## Controls

The result is only as good as what it is compared against, so each measurement
runs against:

- **A random-direction rate.** The share of arbitrary directions that order the
  items as well as the fitted one. This is reported as a collinearity
  diagnostic and explicitly not as a p-value, because it runs the opposite way
  to intuition: when a scale genuinely lies along one axis the item vectors are
  nearly collinear, so almost any direction orders them correctly and the rate
  is high, while pure noise makes the fitted direction overfit to a value
  random directions cannot match and the rate collapses. Treating it as
  significance would invert the conclusion exactly where the signal is
  strongest. The permutation test is the only p-value here.
- **Shuffled gold labels.** The same fitting procedure on a scrambled ordering.
  If it still succeeds, the procedure is fitting noise.
- **Anisotropy removal (implemented).** Embedding spaces concentrate in a
  narrow cone, so part of any apparent direction can come from a few dominant
  components. Every measurement is reported raw and after removing the top
  components, estimated from the background corpus. **Whitening is not
  implemented**: components are projected out, their variance is not rescaled.
  An earlier version of this file claimed otherwise.
- **Frequency (planned, not implemented).** Rare words sit differently in the
  space than common ones, and scale endpoints tend to be rarer than their
  middles. Nothing in the pipeline measures or adjusts for this yet, so no
  result here is frequency-controlled.

A finding that the effect is real but smaller than it looks before controls is
a result, and currently the one I would bet on.

## First results

One model, `all-MiniLM-L6-v2`, all seven layers, 22 scales, 2,000 WikiText
sentences as background. Contextual condition, no components removed, p from
exact permutation over every one of the k! orderings.

**Nine of 22 scales clear unadjusted p < 0.05. Five survive Benjamini-Hochberg
at 5%: loudness, valence, beauty, brightness and size.** Both numbers are
printed by `tools/report.py` so neither is computed by hand.

Two things go with that. A scale that does not survive has not been shown to
lack a gradient; the test has very little power here. And the family being
corrected over is the 22 scales at one layer in one preprocessing setting, so if
a layer or a setting is chosen after looking at results, the real family is
larger and even the five are optimistic.

### The design is at the edge of its own resolution

This is the most useful thing the first run produced, and it is a limit of the
method rather than a result about any model.

A permutation test over k items cannot return a p-value below 1/k!. BH's
first-rank threshold at m = 22 and alpha = 0.05 is 0.0023.

| items per scale | orderings | smallest possible p | can clear BH rank 1 |
|---|---|---|---|
| 5 | 120 | 0.0083 | never |
| 6 | 720 | 0.0014 | only with a perfect ordering |
| 7 | 5,040 | 0.0002 | yes |
| 8 | 40,320 | 0.000025 | comfortably |

So a five-item scale cannot survive correction however well the model orders
it, and `formality` and `completeness` are five-item scales. Layer 3 shows the
consequence: eleven scales clear unadjusted 0.05, none survive BH, because its
smallest p-value is 0.0139 and the first threshold is 0.0023.

**The fix is more items per scale, not more contexts.** Going to eight items
buys two orders of magnitude of headroom. Contexts tighten the interval and
measure context sensitivity, but they cannot move the floor, because the
permutation test permutes the k ranks.

### Context

| condition | scales | leave-one-out | variance share | raw p<.05 |
|---|---|---|---|---|
| bare word | 19 | +0.600 | 0.291 | 4/19 |
| in a carrier sentence | 19 | +0.771 | 0.374 | 7/19 |
| whole utterance | 3 | +0.886 | 0.418 | 2/3 |

Bare and carrier cover the same 19 scales, so that contrast is paired and the
improvement means something. The utterance row is three different scales, and
2 of 3 establishes nothing about utterances in general. It is listed because
those scales exist, not as a comparison.

The layer-0 baseline, before any attention runs, already reaches +0.714 with 5
of 22 unadjusted, so part of what looks like a gradient sits in the static
embedding table rather than being built by the model.

### In-sample against held out

Three scales score far worse out of sample than in it: `fear` +1.000 in-sample
and -0.714 held out, `completeness` +1.000 and -0.700, `speed` +0.943 and
-0.543.

Because the direction is fitted using the intended ranks, this says the fitted
ordering **does not generalize when an item is withheld**. It is not evidence
that the model stores a reversed axis, and an earlier draft of this file said
so wrongly.

Negation at the low pole is a plausible explanation and nothing more: `fearless`
is the scale's own stem plus a negating suffix, and `weightless`, `untouched`
and `stationary` are absences rather than small amounts. That is a hypothesis
flagged in `data/scales/scales.yaml`.

**These scales will not be edited in place.** Rebuilding items after seeing
which ones failed would tune the benchmark to the model. The current definitions
and their results stay, and any replacement arrives as a new version of the
scale with an ordering justified independently of these results.

### Variance along the fitted direction, against the items' own axis

The kernel now reports both: variance along the **rank-supervised** direction,
and variance along the items' own leading principal component, fitted without
the ranks. The supervised number is bounded above by the unsupervised one by
construction, and that holds in all 861 measured cells with no exceptions.

The quantifier scales are the sharpest case. `quantity` has the highest
unsupervised PC1 share in the whole set, 0.828 against a median of 0.413, so
its items genuinely are close to one-dimensional, and that is now established
without reference to the gold ranks. The supervised direction recovers most of
that axis, 0.729. Held-out ordering is nevertheless poor: +0.657, surviving
nothing, and +0.657 in-sample too, so it is not an overfitting artifact.
`frequency` behaves the same way at 0.747 unsupervised, 0.714 supervised and
+0.429 held out.

Said precisely: **these items lie along a dominant axis, the ranks pick out
close to that same axis, and projecting onto it still does not order a withheld
item.** That is a stronger and narrower statement than an earlier draft's
"clean amount axis", which asserted more than the measurements support.

### Do different scales reuse a smaller set of directions?

This was the project's central unfinished question. It is now implemented and
checked against ground truth before being pointed at real directions: on
synthetic items confined to a planted rank-3 subspace the analysis recovers
3.07, and on independent directions it recovers 16.50 of a possible 19 and
reports no sharing.

`sg-measure --directions` exports each scale's fitted direction together with
its layer, condition and preprocessing setting, two directions fitted on
disjoint halves of the contexts, and 50 shuffled-label directions as the
matched null. `sg-subspace` then compares spectra within one model, one layer
and one setting.

Nineteen lexical scales, carrier condition, no components removed:

| layer | effective dims | matched null | gap | dims for 90% | dims for 99% | mean abs cosine | null |
|---|---|---|---|---|---|---|---|
| 0 | 16.04 | 17.16 | 1.12 | 16 | 19 | 0.078 | 0.060 |
| 1 | 14.95 | 16.18 | 1.23 | 15 | 19 | 0.094 | 0.077 |
| 2 | 13.94 | 15.71 | 1.77 | 15 | 19 | 0.108 | 0.084 |
| 3 | 12.23 | 15.00 | 2.77 | 14 | 19 | 0.131 | 0.096 |
| 4 | 10.64 | 14.06 | 3.42 | 14 | 19 | 0.155 | 0.109 |
| 5 | 10.45 | 14.06 | 3.61 | 14 | 19 | 0.160 | 0.108 |
| 6 | 10.84 | 14.74 | 3.90 | 14 | 19 | 0.146 | 0.094 |

Effective dimensions is the participation ratio of the stacked directions'
spectrum, which needs no threshold: it is 19 for 19 orthogonal directions and 1
if they all coincided.

**There is real sharing, it grows with depth, and it is modest.** The directions
are more aligned than the matched null at every layer, including layer 0 before
any attention runs, and the gap widens monotonically from 1.12 to 3.90 as depth
increases. But about 11 effective dimensions out of at most 19 is a reduction,
not a collapse, and it still takes all 19 components to carry 99% of the energy,
so no scale's direction is redundant.

Four limits belong with those numbers:

- **At most 19.** Nineteen directions can span nineteen dimensions, so this
  estimates the dimensionality of *these selected scales*, not of semantics.
- **The p-value is floored.** Zero of 50 shuffled-label replicates fell at or
  below the observed ratio at any layer, giving p = 1/51 = 0.0196. That is the
  smallest value 50 replicates can express, not a measurement of how small the
  true value is.
- **The null is doing real work.** A random-vector null would sit near 19,
  because a fitted direction necessarily lies in the row space of its own
  centred item matrix, which has rank at most k-1. The shuffled-label null is
  matched on that, and it is why the claimed gap is 3.90 rather than 8.
- **The directions are stable, which is what makes the rest meaningful.**
  Median absolute cosine between directions fitted on disjoint halves of the
  contexts is 0.986 or better at every layer.

### The prediction registered in this file was not supported

I expected the effect to shrink once anisotropy was controlled. Removing up to
three dominant background components left the aggregate median leave-one-out at
+0.800 and moved the variance share from 0.398 to 0.391.

Stated within its scope: **removing the tested components barely changed the
aggregate.** That does not exclude anisotropy more generally, more components
were not tried, and the unchanged median hides individual movement. Per scale
the median absolute change is 0.000 but the maximum is 0.114, and `age` crosses
the threshold. `tools/report.py` prints the per-scale movement for that reason.

## Coverage

English, twenty-two scales across six families: physical intensity, quantity,
modality, evaluation, emotion and pragmatics. Breadth of scales is what makes
the sharing question answerable, so it is the axis the study spends its effort
on.

Three properties of a scale are recorded and then used as factors in the
analysis rather than ignored:

- **Polarity.** Unipolar scales run from a floor to a ceiling, like *silent*
  to *deafening*. Bipolar scales have a natural midpoint, like *terrible* to
  *excellent*. There is no reason to assume the geometry treats them alike.
- **Unit.** Most scales are lexical, a single word in a fixed carrier
  sentence. Politeness, hedging and formality exist only as whole utterances,
  which tests whether a gradient can live in sentence structure rather than in
  a word.
- **Gold confidence.** Some orderings are perceptual and beyond dispute,
  others are conventional and weaker. Those are tagged rather than dropped,
  and results are reported split by the tag instead of pooled as though every
  ordering were equally solid.

## Layout

```text
data/scales/scales.yaml       22 ordered scales, frames and subjects
data/background/              WikiText sentences plus their provenance
python/scalar_geometry/       scale expansion, extraction, seeds, manifest utilities, CLI
python/tests/                 29 tests, standard library only
kernel/src/lib.rs             the measurement numerics, 15 tests
kernel/src/main.rs            sg-measure: one shard in, results out
kernel/src/bin/sg-subspace.rs shared-subspace analysis over exported directions
tools/fetch_background.py     fetch and clean the background corpus
tools/check_synthetic.py      end-to-end checks on synthetic shards
tools/report.py               the tables this README is built from
docs/interchange.md           the shard and directions formats
docs/theoretical-framework.md hypotheses, controls and interpretation boundaries
```

Python owns extraction, Rust owns the statistics, and they meet at a file
rather than at a language binding. The cost is one format to document; the
benefit is that either side can be rewritten or run on a different machine, and
that the Rust side needs neither a YAML parser nor a linear algebra dependency.
The authorship boundary is documented in [`CONTRIBUTIONS.md`](CONTRIBUTIONS.md)
and preserved by the two publication branches.

## Local verification

Python 3.10 or newer and a Rust toolchain are required. The fast verification
path does not download a transformer model or require a GPU.

For a versioned, resumable production run with immutable output identities,
automatic manifests, and structured status, see
[`docs/runtime.md`](docs/runtime.md). The Google Cloud batch platform, immutable
artifact publication and BigQuery ledger are documented in
[`docs/cloud-deployment.md`](docs/cloud-deployment.md).

```sh
python -m venv .venv
python -m pip install -e ".[dev]"
python -m unittest discover -s python/tests -v
python -m unittest discover -s cloud/tests -v
ruff check python tools cloud
cargo fmt --manifest-path kernel/Cargo.toml --check
cargo clippy --manifest-path kernel/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path kernel/Cargo.toml --release
cargo build --manifest-path kernel/Cargo.toml --release
python tools/check_synthetic.py
```

Real extraction additionally downloads the selected model and writes large,
regenerable shards. Those shards and exported direction matrices are excluded
from version control; compact measurement reports remain under `results/`.

## Reproducibility

Two properties are enforced by the current tests:

- **Seed derivation is deterministic.** Per-cell seeds are derived from one
  master seed without ambiguous concatenation.
- **The interchange is exercised end to end.** Synthetic vectors pass from the
  Python shard writer through the Rust measurement and subspace binaries, with
  planted-signal and negative controls checked in CI.

Extraction uses fixed-length padding, float32 inference and deterministic
algorithm requests to reduce avoidable variation. This is not a claim of
bit-for-bit identity across hardware backends. `sg run` requires an exact model
commit, derives an immutable run identity from the configuration and inputs,
and records the Git state, container digest, dependencies, hardware, timings,
and output hashes in its manifest.

## Cost

Extraction is shared across analysis cells, so GPU time scales with the number
of models rather than with the size of the grid.

Measured, on CPU, for `all-MiniLM-L6-v2`:

- extraction, 5,588 texts at all 7 layers: **6m23s**, about 15 texts per second
- measurement, one shard, 123 cells at 2,000 random draws and 1,000 bootstrap
  resamples: **54s**

`sg plan` will not print anything unless you give it a measured rate, because
an invented rate is worse than no rate. What it prints is **scenario
arithmetic, not a budget**: it applies one throughput figure to every model,
and twelve models of different sizes do not share a throughput. Combining a CPU
measurement with a GPU hourly price is incoherent as a bound in either
direction, and measurement and infrastructure costs sit outside the number
entirely. A real budget needs a measured rate per model on the hardware that
will run it.

```sh
python -m scalar_geometry.cli plan --models 12 --layers 13     --texts-per-second 15 --usd-per-gpu-hour 0.35
```
