# Execution model

The statistical note fixes how a measurement becomes evidence. This note fixes
what it means to execute a measurement more than once: on another machine, in
another image, with another seed. It characterises what the cloud platform
guarantees, what two executions are entitled to disagree about, and what the
infrastructure makes possible that a workstation did not. It states method and
consequences, not results.

## Experiments and executions

Write an experiment as the tuple

    E = (c, s, b, M@r)

of a normalised configuration c, the scale definitions s, the background corpus
b and a model M at an exact revision r. The run identifier is a hash of E, so
two configurations that differ in any field, including the seed, are two
experiments with two identities.

An execution is an experiment together with everything that is not part of it:

    X = (E, i, h, t)

with i the image digest, h the hardware and t the time. The platform records
all four, but only E enters the identifier. This is deliberate. A measurement is
claimed to be a property of E: if it changed with i or h, it would be a
property of the software stack, and the experiment would be underspecified.

The consequence is that a second execution of the same E claims the same
identity with different records (its manifest names another image and another
time). The artifact store is write-once per identity, so it refuses the second
execution rather than overwrite the first. That refusal is correct as a
guarantee and inconvenient as a workflow; the remedy is in the last section.

## Which outputs depend on what

Every output of a run is a function of E, and some are also functions of the
seed inside c. They fall into three classes.

1. **Deterministic in the embeddings.** The leave-one-out ordering statistic,
   the in-sample statistic, the supervised and unsupervised variance shares, and
   the exact permutation p-value. The permutation test enumerates all k!
   labellings when k! is at most 50,000, so no random number is drawn. For a
   fixed set of embeddings these outputs, and every significance decision built
   on them, are the same for every seed.
2. **Monte Carlo in the seed.** The random-direction baseline, the bootstrap
   dispersion reported as context stability, the half-split cosines and the
   matched null for effective dimensionality. Their values move with the seed;
   their distributions do not.
3. **Bookkeeping.** Timestamps, durations, the image digest. They describe the
   execution, not the experiment.

So a change of seed is a controlled experiment on the second class: it must
leave the first class unchanged and may move the second only within its Monte
Carlo error.

## Why decisions survive a change of image

Embeddings are not guaranteed to be bit-identical across images or hardware:
library versions and instruction sets change the order of floating-point
operations. The decisions are nonetheless stable, for a structural reason.

The exact p-value of a k-item scale lies on the grid j/k!. It changes only if a
perturbation of the embeddings reorders the ordering statistic between the gold
labelling and some other labelling, that is, only near a tie. If a perturbation moves the statistic of
every labelling by at most delta, the p-value moves by at most the number of
labellings whose statistic lies within 2 * delta of the gold value, divided by
k!. Away from ties the p-value does not move at all.

A Benjamini-Hochberg decision then changes only if a p-value crosses its
threshold r * alpha / m. Both conditions together give a margin for each
decision: the number of grid steps between the observed p-value and the
nearest threshold that would flip it. A decision with a margin of several
steps is a property of E. A decision at a margin of zero or one step is a
property of E and the arithmetic, and should be reported as fragile.

This is the sense in which two executions with different images and different
seeds reaching the same significance decisions is evidence, and the sense in
which it is not: it confirms the class-one outputs are stable at this design,
and says nothing about whether the design has the resolution to find what it
is looking for.

## Resolution as a budget

The statistical note derived that a rejection at rank one requires the floor of
the test to sit below alpha / m. The cloud makes larger families cheap to
compute, which makes this bound the binding constraint rather than compute.

For the order test, with floor 1/k!:

| family | m | needs k! of at least | smallest k |
| --- | --- | --- | --- |
| one layer, contextual group | 22 | 440 | 6 |
| all seven layers, contextual group | 154 | 3,080 | 7 |
| seven layers, two models | 308 | 6,160 | 8 |

For the sharing test, with floor 1/(R + 1) over R null replicates, the same
condition reads R + 1 >= m / alpha. One subspace group at one layer is a family
of one, and R = 50 suffices. Across seven layers at alpha = 0.05 it needs
R >= 139. A configuration with R = 20 has a floor of 1/21, about 0.048, which
can pass an uncorrected test at 0.05 and cannot pass rank one of any corrected
family larger than one; its sharing p-values check the pipeline, not the
hypothesis.

Two remedies follow, and they are not interchangeable. Widening scales to seven
or eight items lowers the order floor for every future family. Declaring the
layer before results exist keeps the family small without changing the data.

## What the infrastructure makes possible

**Separate the two identities.** Keep the run identifier as the identity of E,
and add an execution key, the identifier together with the image digest and
an attempt number. Artifacts then live under the execution key, the ledger
holds one row per execution, and agreement between executions becomes a query
instead of a conflict. The class-one outputs of two executions of one E should
be equal up to their margins, and a ledger that can say so is a reproduction
record rather than an index.

**Measure Monte Carlo error rather than assume it.** Executing one E under
several seeds gives, for every class-two output, an empirical spread. A null
participation ratio whose spread across seeds is comparable to the observed gap
cannot support a sharing claim, whatever its p-value.

**Fix the confirmatory family before execution.** The bucket stores each
configuration before the job reads it, and object storage records when. A
configuration that names its family, its layer and its alpha, uploaded before
the first execution of that experiment, is a pre-registration with a
timestamp. Families chosen afterwards are exploratory and are labelled as such.

**Compare models through invariants.** Directions fitted in two models live in
different spaces and are not comparable as vectors. The ordering statistic, the
effective dimensionality, its gap to the matched null and the resolution margin
are defined in every model, so a multi-model sweep (one execution per model and
revision) compares those. A shared encoding of degree across models is a claim
about invariants, never about aligned directions.

**Price runs from measurements.** A run's cost is the product of its measured
duration and the resources it holds. The duration of one configuration does not
predict another: extraction scales with texts and layers, measurement with
cells and draws, the matched null with replicates. Each new configuration earns
its own measured figure before it is scaled.
