# Contribution map

This repository separates the theoretical benchmark design from the applied
mathematics and systems implementation so that the provenance of each part is
reviewable in Git history.

## SaadQacif

- Theoretical framing of order, linearity and shared semantic structure
- Ordered lexical-scale benchmark design
- Control logic and interpretation boundaries
- Experimental taxonomy recorded in `data/scales/scales.yaml`

## coderyaz856

- Applied mathematical measurement pipeline
- Python extraction, deterministic seed derivation and interchange layer
- Rust statistics and shared-subspace kernel
- Synthetic ground-truth validation, reporting, containerization and CI

The `Saad-The-Theoretical-Conceiver` branch preserves the theoretical
foundation. The `Yazid-The-Tech-Orchestrator` branch adds the complete tested
implementation and reported artifacts.
