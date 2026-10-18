# Shard format

Python owns extraction, Rust owns measurement, and they meet at a file rather
than at a language binding. There is no PyO3 layer, no shared process and no
serialisation library in common. The cost is one format to document, which is
this page. The benefit is that either side can be rewritten, run on a different
machine, or replaced by a reimplementation, and that the Rust kernel needs
neither a YAML parser nor a parquet dependency.

A shard is one directory holding two files.

## `embeddings.f32`

Raw little-endian IEEE 754 single precision, row major, no header.

```text
(n_rows + n_background) * dim  values
```

The first `n_rows` rows are scale items in exactly the order listed in the
sidecar. The remaining `n_background` rows are the background corpus, in the
order it was read. Nothing else appears in the file, so its length is a
complete integrity check and the reader verifies it before parsing anything.

## `shard.json`

```json
{
  "format": "scalar-geometry/shard/1",
  "model": "sentence-transformers/all-MiniLM-L6-v2",
  "layer": 6,
  "n_layers": 7,
  "dim": 384,
  "pooling": "mean",
  "max_length": 48,
  "n_rows": 252,
  "n_background": 2000,
  "rows": [
    { "scale_id": "temperature", "rank": 1, "condition": "bare" }
  ]
}
```

`format` is checked on read and an unknown value is a hard error, so a future
format change cannot be silently misread as this one.

The row order is produced by `enumerate_rows` in `scalar_geometry.scales` and is
deliberately independent of dict iteration and of the filesystem: scales in file
order, then condition, then ascending rank. The binary payload means nothing
without that order, which is why the order is tested rather than assumed.

## Why the background rows travel with the items

Dominant directions must be estimated from text unrelated to the scales. Taken
from the scale items themselves they would absorb the gradient along with the
anisotropy, and the measurement would quietly return nothing. Shipping the
background in the same shard means the estimate always comes from the same
model, the same layer and the same pooling as the items it corrects, which
removes a whole class of mismatch bug.

## Layers

One shard per layer, so `layer_00` through `layer_NN` sit side by side under a
model's directory. Layer 0 is the embedding layer before any transformer block,
which is a useful baseline: whatever a gradient looks like there, it was not
built by attention.

# Directions format

`sg-measure --directions <dir>` writes a second pair of files in the same
style, and `sg-subspace` reads them.

## `directions.f32`

Raw little-endian float32, row major, no header: one unit direction per row, in
the order the index lists them.

## `directions.json`

```json
{
  "format": "scalar-geometry/directions/1",
  "model": "sentence-transformers/all-MiniLM-L6-v2",
  "layer": 6,
  "dim": 384,
  "pooling": "mean",
  "seed": 1,
  "null_replicates": 50,
  "rows": [
    { "scale_id": "temperature", "condition": "carrier", "remove_top": 0,
      "kind": "fitted", "replicate": 0 }
  ]
}
```

`kind` is one of:

- `fitted` — the direction implied by the gold ranks, one per scale.
- `half_a`, `half_b` — the same fit on two disjoint halves of the contexts,
  split by a seeded shuffle. Their cosine measures whether a direction depends
  on which contexts it happened to see.
- `null` — the identical fit on a shuffled ordering, `replicate` times. This is
  the matched null: same items, same contexts, same preprocessing, same
  procedure, only the ordering destroyed.

## Why the null is exported rather than generated later

A fitted direction always lies inside the row space of its own centred item
matrix, which has rank at most k-1 for a k-item scale. Random unit vectors
ignore that constraint and would make any set of fitted directions look far
more aligned than it is. The only null matched on it is one produced by the
same fit on the same data, so the kernel produces it where the items are, and
the analysis tool never has to reconstruct the geometry.

## Why the model, layer and pooling travel with the directions

Directions are only comparable within one model, one layer and one
preprocessing setting. The index carries all of them and `sg-subspace` groups
on them, so a mismatched comparison cannot be made by passing the wrong files.
