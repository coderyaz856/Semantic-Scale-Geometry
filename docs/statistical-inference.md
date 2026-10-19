# Statistical inference

The theoretical framework separates three hypotheses: order, linearity and
sharing. This note states how a measurement becomes evidence about them: what
one observation is, which tests are corrected together, what the tests can and
cannot resolve, and how the two quantities with counter-intuitive directions
are read. It applies wherever results are recorded, on a workstation or in a
cloud ledger. A ledger stores these decisions; it does not change them.

## The unit of evidence

A measurement is a property of one function applied to one set of inputs. The
function is the model at an exact revision, not the model's name: two
revisions of one model are two functions, and a geometric property of one is
not a property of the other. The inputs are the versioned scale definitions,
the background corpus used to estimate anisotropy, and the configuration that
selects layers, pooling and preprocessing.

A result is therefore identified by the model identifier, the full revision
hash, the content digests of the scale definitions and the background corpus,
the digest of the normalised configuration, and the digest of the executable
environment. A result that cannot name all of these cannot be compared with
another result, because a difference between them could be a difference in any
unnamed input. Missing identity is recorded as missing, never inferred.

## The order test and its floor

Order is tested by exact permutation. For a scale of $k$ items, every one of the
$k!$ labellings is enumerated, the leave-one-out ordering statistic $T$ is
recomputed under each with the direction refitted, and the p-value is the
share of labellings that score at least as well as the gold labelling $\pi_0$:

$$
p = \frac{\left\lvert \lbrace \pi \in S_k : T(\pi) \ge T(\pi_0) \rbrace \right\rvert}{k!}.
$$

The test is one-sided: the hypothesis is that the gold order is recovered, not
that some order is.

Two consequences follow from the construction rather than from any data.

1. The gold labelling is one of the $k!$ labellings, so $p \ge 1/k!$. Every
   attainable value is a multiple of $1/k!$.
2. The floor depends on $k$ alone. More contexts sharpen the statistic and its
   interval, but they cannot lower the floor, because the test permutes the $k$
   ranks and nothing else.

| items $k$ | labellings $k!$ | floor $1/k!$ |
| --- | --- | --- |
| 5 | 120 | 0.0083 |
| 6 | 720 | 0.0014 |
| 7 | 5,040 | 0.00020 |
| 8 | 40,320 | 0.000025 |

Enumeration is exhaustive up to 50,000 labellings, which covers $k \le 8$.

## The family and the correction

Significance is decided by Benjamini-Hochberg at level $\alpha$ over one family of
tests. The family is the scales measured at one layer, under one preprocessing
setting, in one condition group. The contextual group contains every scale
once, lexical scales in carrier frames and utterance scales in their
constructions; the bare lexical condition is a group of its own.

The group, not the single condition, is the unit. Correcting a few utterance
scales among themselves would give them thresholds several times more lenient
than the same scales receive alongside the rest, and would turn a choice of
bookkeeping into a claim about the model.

With $m$ tests and ordered p-values $p_{(1)} \le \dots \le p_{(m)}$, the step-up
procedure finds

$$
r^{\ast} = \max\left\lbrace r : p_{(r)} \le \frac{r \alpha}{m} \right\rbrace
$$

and rejects every test of rank at most $r^{\ast}$. Because it is step-up, a
p-value above its own rank-1 threshold can still be rejected when enough
smaller p-values in the family carry it to a higher rank.

## Resolution: what the design can detect

The floor and the family size together bound what can be found.

A scale at its floor clears the first threshold only when

$$
\frac{1}{k!} \le \frac{\alpha}{m}
\quad\Longleftrightarrow\quad
m \le \alpha \cdot k!.
$$

At $\alpha = 0.05$:

| items $k$ | largest $m$ with a rank-1 rejection possible |
| --- | --- |
| 5 | 6 |
| 6 | 36 |
| 7 | 252 |
| 8 | 2,016 |

For a contextual family of $m = 22$ at $\alpha = 0.05$, the first threshold is
$\alpha/m = 0.05/22 \approx 0.0023$.

- A **six-item** scale can clear it, but only at its floor: $1/720 \approx 0.0014$
  passes, and the next attainable value, $2/720 \approx 0.0028$, does not.
- A **five-item** scale cannot clear it alone, since its floor is
  $1/120 \approx 0.0083$. Under step-up it can still be rejected at rank
  $r \ge 4$, which needs at least four p-values in the family at or below
  $4 \times 0.05 / 22 \approx 0.0091$. Its
  significance then depends on other scales' results as much as its own.

So the design sits at the edge of its own resolution. The remedy is more items
per scale, not more contexts: at eight items the floor is two orders of
magnitude below the first threshold.

## Reading a non-rejection

A scale that is not rejected has not been shown to lack an ordered direction.
Given the floor, a scale can be perfectly ordered in every context and still
fail to survive correction. Non-rejection is evidence about the design's power
as much as about the model, and is reported as non-rejection, never as absence.

## Choosing a layer or a setting

The family is defined at one layer and one preprocessing setting. If the layer
or the setting is chosen after looking at results, the effective family
includes every layer and setting that was looked at, and survivors computed in
the smaller family are optimistic. The layer and setting are therefore fixed
before results are read, or the correction is taken over every combination
examined.

## Sharing: effective dimensionality against a matched null

Sharing is measured on the directions fitted to a group of scales. Their
Gram spectrum $\lambda_1, \dots, \lambda_n$ is summarised by the participation
ratio,

$$
\mathrm{PR} = \frac{\left(\sum_{i=1}^{n} \lambda_i\right)^{2}}{\sum_{i=1}^{n} \lambda_i^{2}},
\qquad 1 \le \mathrm{PR} \le n,
$$

a continuous count of the dimensions the $n$ directions effectively occupy.

The reference is not random vectors. Each fitted direction lies in the row
space of its own centred item matrix, of rank at most $k - 1$, and random
vectors are not so constrained. The matched null refits every direction after
shuffling the gold labels, which keeps items, contexts, preprocessing, the
fitting procedure and that row-space constraint, and destroys only the order.

The quantity reported is the dimension gap, the mean null participation ratio
over $R$ replicates minus the observed one:

$$
\Delta = \frac{1}{R}\sum_{j=1}^{R} \mathrm{PR}^{\mathrm{null}}_{j} - \mathrm{PR}^{\mathrm{obs}}.
$$

A positive gap means the fitted directions occupy fewer dimensions than fitting
on unordered labels would produce, which is what sharing predicts. Its p-value
counts the null replicates at or below the observed ratio, with one added to
numerator and denominator:

$$
p = \frac{1 + \left\lvert \lbrace j : \mathrm{PR}^{\mathrm{null}}_{j} \le \mathrm{PR}^{\mathrm{obs}} \rbrace \right\rvert}{R + 1}
\quad \ge \quad \frac{1}{R + 1},
$$

about $0.0196$ for $R = 50$. The same resolution argument as above applies,
with $R + 1$ in place of $k!$.

## Stability: a dispersion, read downward

Context stability is the standard deviation, across contexts, of the held-out
ordering statistic. It is a dispersion, not a score: lower means the scale's
ordering depends less on the context it appears in. It is undefined for a
condition with a single context. A separate check fits directions on two
disjoint halves of the contexts and reports their cosine, because a direction
that changes with the contexts it was fitted on is not a property of the scale.
