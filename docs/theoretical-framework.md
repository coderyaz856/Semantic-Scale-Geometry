# Theoretical framework

Semantic Scale Geometry asks whether an embedding model represents ordered
linguistic scales as measurable geometric structure. A scale supplies an
ordering independent of the model, such as `freezing < cold < cool < warm <
hot < scalding`. That ordering makes the geometric claim falsifiable.

## Three distinct hypotheses

The study keeps three questions separate:

1. **Order:** does projection onto a fitted direction recover the independently
   specified ranking?
2. **Linearity:** how much of the items' variation is carried by one axis?
3. **Sharing:** do directions fitted to different scales occupy a smaller
   common subspace?

Success on one hypothesis does not establish the others. Ordered items can
follow a curved path, nearly collinear items can appear in the wrong order, and
individually stable directions can remain unrelated across scales.

## Experimental units

The benchmark contains lexical and utterance-level scales across physical
intensity, quantity, modality, evaluation, emotion and pragmatics. Each scale
records polarity, linguistic unit and confidence in its gold ordering. Lexical
items are measured both alone and inside shared carrier frames. Utterance
scales vary their object while preserving their construction.

The versioned definitions in `data/scales/scales.yaml` are experimental data.
Definitions are not edited after observing a failure. A revised scale must be
introduced as a new version so that sensitivity to the revision remains
visible.

## Controls and interpretation

The fitted ordering is compared with exact shuffled-label permutations. A
random-direction rate is retained only as a collinearity diagnostic because it
can move in the opposite direction from a conventional significance measure.
Dominant background components can be projected out to test sensitivity to
embedding anisotropy. This is component removal, not whitening.

Leave-one-item-out scoring distinguishes interpolation from held-out ordering.
Half-context fits test whether a direction is stable across linguistic
contexts. Shared-subspace estimates are compared with directions fitted after
shuffling the gold labels, preserving the low-rank row-space constraint caused
by fitting a direction from a small number of items.

## Boundaries

The current study is English-only and evaluates one model family in its first
reported run. Frequency control, grouped cross-validation over whole template
families and broader model coverage are not yet implemented. The results
therefore characterize this benchmark and configuration, not semantic
representation in general.
