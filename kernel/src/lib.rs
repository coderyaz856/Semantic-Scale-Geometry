//! Measurement kernel.
//!
//! Everything here is deterministic and dependency-light on purpose. The
//! random number generator is implemented in this file rather than taken from
//! a crate, so a result cannot change because a dependency changed its
//! generator. Top components come from power iteration, so there is no linear
//! algebra dependency either.
//!
//! Three statistics are computed per scale, and they answer different
//! questions that are routinely conflated:
//!
//! * `spearman_in_sample` uses the direction fitted on all the items. It is
//!   optimistic by construction and is reported only for transparency.
//! * `spearman_loo` holds each item out, fits on the rest, and projects the
//!   held-out item. This is the honest statistic and the one the permutation
//!   test uses.
//! * `variance_share` is the fraction of the items' total variance that lies
//!   along the fitted direction. It is what licenses the word "direction", and
//!   a scale can order perfectly while scoring badly here.

use serde::{Deserialize, Serialize};

// ---------------------------------------------------------------- matrices

#[derive(Clone, Debug)]
pub struct Matrix {
    pub rows: usize,
    pub cols: usize,
    pub data: Vec<f64>,
}

impl Matrix {
    pub fn zeros(rows: usize, cols: usize) -> Self {
        Self {
            rows,
            cols,
            data: vec![0.0; rows * cols],
        }
    }

    pub fn from_rows(rows: &[&[f64]]) -> Self {
        let cols = rows.first().map_or(0, |r| r.len());
        assert!(rows.iter().all(|r| r.len() == cols), "ragged rows");
        let mut data = Vec::with_capacity(rows.len() * cols);
        for row in rows {
            data.extend_from_slice(row);
        }
        Self {
            rows: rows.len(),
            cols,
            data,
        }
    }

    #[inline]
    pub fn row(&self, i: usize) -> &[f64] {
        &self.data[i * self.cols..(i + 1) * self.cols]
    }

    #[inline]
    pub fn row_mut(&mut self, i: usize) -> &mut [f64] {
        let cols = self.cols;
        &mut self.data[i * cols..(i + 1) * cols]
    }

    /// Subtract the column means in place and return them.
    pub fn center(&mut self) -> Vec<f64> {
        let mut mean = vec![0.0; self.cols];
        for i in 0..self.rows {
            for (m, v) in mean.iter_mut().zip(self.row(i)) {
                *m += *v;
            }
        }
        let n = self.rows as f64;
        for m in mean.iter_mut() {
            *m /= n;
        }
        for i in 0..self.rows {
            let cols = self.cols;
            let start = i * cols;
            for (value, m) in self.data[start..start + cols].iter_mut().zip(&mean) {
                *value -= *m;
            }
        }
        mean
    }

    pub fn frobenius_sq(&self) -> f64 {
        self.data.iter().map(|v| v * v).sum()
    }
}

#[inline]
pub fn dot(a: &[f64], b: &[f64]) -> f64 {
    debug_assert_eq!(a.len(), b.len());
    a.iter().zip(b).map(|(x, y)| x * y).sum()
}

pub fn normalize(v: &mut [f64]) -> f64 {
    let norm = dot(v, v).sqrt();
    if norm > 0.0 {
        for x in v.iter_mut() {
            *x /= norm;
        }
    }
    norm
}

// ------------------------------------------------------------------- prng

/// SplitMix64. Small, fast and fully specified, so a seed pins the stream
/// independently of any crate version.
#[derive(Clone, Debug)]
pub struct Prng(u64);

impl Prng {
    pub fn new(seed: u64) -> Self {
        Self(seed)
    }

    pub fn next_u64(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }

    /// Uniform in the open interval (0, 1).
    pub fn next_f64(&mut self) -> f64 {
        ((self.next_u64() >> 11) as f64 + 0.5) / (1u64 << 53) as f64
    }

    /// Standard normal via Box-Muller.
    pub fn next_normal(&mut self) -> f64 {
        let u1 = self.next_f64();
        let u2 = self.next_f64();
        (-2.0 * u1.ln()).sqrt() * (std::f64::consts::TAU * u2).cos()
    }

    pub fn unit_vector(&mut self, dim: usize) -> Vec<f64> {
        let mut v: Vec<f64> = (0..dim).map(|_| self.next_normal()).collect();
        normalize(&mut v);
        v
    }
}

// --------------------------------------------------------------- statistics

/// Average ranks, so ties are handled rather than ignored.
pub fn rank_of(values: &[f64]) -> Vec<f64> {
    let n = values.len();
    let mut order: Vec<usize> = (0..n).collect();
    order.sort_by(|&a, &b| values[a].partial_cmp(&values[b]).expect("NaN in ranks"));
    let mut ranks = vec![0.0; n];
    let mut i = 0;
    while i < n {
        let mut j = i + 1;
        while j < n && values[order[j]] == values[order[i]] {
            j += 1;
        }
        let shared = (i + j - 1) as f64 / 2.0 + 1.0;
        for k in i..j {
            ranks[order[k]] = shared;
        }
        i = j;
    }
    ranks
}

pub fn pearson(a: &[f64], b: &[f64]) -> f64 {
    let n = a.len() as f64;
    let ma = a.iter().sum::<f64>() / n;
    let mb = b.iter().sum::<f64>() / n;
    let mut num = 0.0;
    let mut da = 0.0;
    let mut db = 0.0;
    for (x, y) in a.iter().zip(b) {
        let dx = x - ma;
        let dy = y - mb;
        num += dx * dy;
        da += dx * dx;
        db += dy * dy;
    }
    if da == 0.0 || db == 0.0 {
        return 0.0;
    }
    num / (da.sqrt() * db.sqrt())
}

pub fn spearman(a: &[f64], b: &[f64]) -> f64 {
    pearson(&rank_of(a), &rank_of(b))
}

// ----------------------------------------------------------- direction fit

/// The first PLS component: the covariance direction between the items and
/// their gold ranks. Closed form, so there is no optimizer to tune and no
/// convergence to argue about.
pub fn fit_direction(x: &Matrix, y: &[f64]) -> Vec<f64> {
    debug_assert_eq!(x.rows, y.len());
    let mean_y = y.iter().sum::<f64>() / y.len() as f64;
    let mut w = vec![0.0; x.cols];
    for (i, yi) in y.iter().enumerate() {
        let c = yi - mean_y;
        for (wj, xj) in w.iter_mut().zip(x.row(i)) {
            *wj += c * xj;
        }
    }
    normalize(&mut w);
    w
}

pub fn project(x: &Matrix, w: &[f64]) -> Vec<f64> {
    (0..x.rows).map(|i| dot(x.row(i), w)).collect()
}

/// Leave-one-out scores. For each item, the direction is fitted on the others
/// and the held-out item is projected onto it. With only a handful of points in
/// a few hundred dimensions an in-sample fit is close to free, so this is the
/// statistic that carries the claim.
pub fn loo_scores(x: &Matrix, y: &[f64]) -> Vec<f64> {
    let k = x.rows;
    let mut out = vec![0.0; k];
    for (held, slot) in out.iter_mut().enumerate() {
        let kept: Vec<&[f64]> = (0..k).filter(|&i| i != held).map(|i| x.row(i)).collect();
        let kept_y: Vec<f64> = (0..k).filter(|&i| i != held).map(|i| y[i]).collect();
        let mut sub = Matrix::from_rows(&kept);
        let mean = sub.center();
        let w = fit_direction(&sub, &kept_y);
        let centered: Vec<f64> = x.row(held).iter().zip(&mean).map(|(v, m)| v - m).collect();
        *slot = dot(&centered, &w);
    }
    out
}

/// Share of the items' total variance lying along `w`. Expects `x` centered.
pub fn variance_share(x: &Matrix, w: &[f64]) -> f64 {
    let total = x.frobenius_sq();
    if total == 0.0 {
        return 0.0;
    }
    let along: f64 = (0..x.rows).map(|i| dot(x.row(i), w).powi(2)).sum();
    along / total
}

// --------------------------------------------------------------- anisotropy

/// Top `m` principal directions of `background`, by power iteration with
/// deflation. `background` must already be centered.
///
/// These are estimated from background text, never from the scale items.
/// Estimating them from the items would strip the signal along with the
/// artifact, which is the single easiest way to get a wrong answer here.
pub fn top_components(background: &Matrix, m: usize, prng: &mut Prng) -> Vec<Vec<f64>> {
    let mut work = background.clone();
    let mut comps = Vec::with_capacity(m);
    for _ in 0..m {
        let mut v = prng.unit_vector(work.cols);
        for _ in 0..64 {
            let scores: Vec<f64> = (0..work.rows).map(|i| dot(work.row(i), &v)).collect();
            let mut next = vec![0.0; work.cols];
            for (i, s) in scores.iter().enumerate() {
                for (nj, xj) in next.iter_mut().zip(work.row(i)) {
                    *nj += s * xj;
                }
            }
            if normalize(&mut next) == 0.0 {
                break;
            }
            let delta: f64 = next.iter().zip(&v).map(|(a, b)| (a - b).abs()).sum();
            v = next;
            if delta < 1e-12 {
                break;
            }
        }
        for i in 0..work.rows {
            let s = dot(work.row(i), &v);
            for (xj, vj) in work.row_mut(i).iter_mut().zip(&v) {
                *xj -= s * vj;
            }
        }
        comps.push(v);
    }
    comps
}

/// Centre a background matrix and take its top `m` directions.
///
/// These depend on the model, the layer and `m` alone, never on the scale, so
/// they are computed once per shard and reused across every scale in it. The
/// power iteration is the most expensive step in a cell, and recomputing it
/// per scale would multiply the whole sweep by the number of scales.
pub fn background_components(background: &Matrix, m: usize, prng: &mut Prng) -> Vec<Vec<f64>> {
    if m == 0 {
        return Vec::new();
    }
    let mut centered = background.clone();
    centered.center();
    top_components(&centered, m, prng)
}

pub fn remove_components(x: &mut Matrix, comps: &[Vec<f64>]) {
    for comp in comps {
        for i in 0..x.rows {
            let s = dot(x.row(i), comp);
            for (xj, cj) in x.row_mut(i).iter_mut().zip(comp) {
                *xj -= s * cj;
            }
        }
    }
}

// ------------------------------------------------------------- permutations

fn next_permutation(a: &mut [f64]) -> bool {
    if a.len() < 2 {
        return false;
    }
    let mut i = a.len() - 1;
    while i > 0 && a[i - 1] >= a[i] {
        i -= 1;
    }
    if i == 0 {
        return false;
    }
    let mut j = a.len() - 1;
    while a[j] <= a[i - 1] {
        j -= 1;
    }
    a.swap(i - 1, j);
    a[i..].reverse();
    true
}

fn factorial(n: usize) -> u64 {
    (1..=n as u64).product()
}

/// Exhaustive permutation limit. Below this the p-value is exact rather than
/// sampled, which is worth saying out loud in a paper.
pub const EXACT_PERMUTATION_LIMIT: u64 = 50_000;

// ----------------------------------------------------------------- results

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ScaleResult {
    pub scale_id: String,
    pub condition: String,
    pub remove_top: usize,
    pub n_items: usize,
    pub dim: usize,
    pub spearman_in_sample: f64,
    pub spearman_loo: f64,
    /// Variance share along the direction fitted **using the gold ranks**.
    /// Supervised, so it is bounded above by `pc1_share` and cannot on its own
    /// show that the items have a dominant axis.
    pub variance_share: f64,
    /// Variance share along the items' own leading principal component, fitted
    /// without the ranks. The gap between this and `variance_share` is the
    /// honest version of the order-versus-linearity question: a large
    /// `pc1_share` with a much smaller `variance_share` means the items do lie
    /// along an axis, but not the one the ranks pick out.
    #[serde(default)]
    pub pc1_share: f64,
    /// Fraction of arbitrary directions that order the items at least as well
    /// as the fitted direction does in sample.
    ///
    /// This is a collinearity diagnostic and deliberately not a p-value. When
    /// a scale really does lie along one axis, the centred item vectors are
    /// nearly collinear, so almost any direction orders them correctly and
    /// this rate is high. When the items are noise, the fitted direction
    /// overfits to in-sample perfection that random directions cannot match,
    /// and the rate collapses towards zero. Reading it as significance
    /// therefore inverts the truth. The only p-value here is the permutation
    /// one below.
    pub random_direction_match_rate: f64,
    pub random_draws: usize,
    /// Permutation p-value on the leave-one-out statistic.
    pub p_permutation: f64,
    pub permutations: u64,
    pub permutation_exact: bool,

    /// How many complete copies of the scale went into the estimate. One for
    /// the bare condition, one per frame-and-subject pair otherwise.
    #[serde(default)]
    pub n_contexts: usize,
    /// Percentile interval from resampling contexts. Absent with one context,
    /// because there is nothing to resample and a fabricated interval would be
    /// worse than none.
    #[serde(default)]
    pub spearman_loo_ci_low: Option<f64>,
    #[serde(default)]
    pub spearman_loo_ci_high: Option<f64>,
    #[serde(default)]
    pub bootstrap_draws: usize,
    /// Spread of the statistic across single contexts, which is the
    /// context-sensitivity measurement rather than an error bar.
    #[serde(default)]
    pub per_context_loo_mean: Option<f64>,
    #[serde(default)]
    pub per_context_loo_sd: Option<f64>,
}

fn mean_of(values: &[f64]) -> f64 {
    values.iter().sum::<f64>() / values.len() as f64
}

fn sd_of(values: &[f64]) -> f64 {
    if values.len() < 2 {
        return 0.0;
    }
    let m = mean_of(values);
    let var = values.iter().map(|v| (v - m).powi(2)).sum::<f64>() / (values.len() - 1) as f64;
    var.sqrt()
}

/// Percentile of an already sorted slice, by nearest rank.
fn percentile(sorted: &[f64], p: f64) -> f64 {
    let n = sorted.len();
    let index = ((p / 100.0) * n as f64).floor() as usize;
    sorted[index.min(n - 1)]
}

/// Element-wise mean of several equally shaped matrices.
pub fn mean_matrix(matrices: &[&Matrix]) -> Matrix {
    let first = matrices[0];
    assert!(
        matrices
            .iter()
            .all(|m| m.rows == first.rows && m.cols == first.cols),
        "contexts must all have the same shape"
    );
    let mut out = Matrix::zeros(first.rows, first.cols);
    for m in matrices {
        for (o, v) in out.data.iter_mut().zip(&m.data) {
            *o += v;
        }
    }
    let n = matrices.len() as f64;
    for o in out.data.iter_mut() {
        *o /= n;
    }
    out
}

pub fn prepared(items: &Matrix, comps: &[Vec<f64>]) -> Matrix {
    let mut x = items.clone();
    if !comps.is_empty() {
        remove_components(&mut x, comps);
    }
    x.center();
    x
}

/// Share of the items' variance along their own leading principal component.
///
/// This is the unsupervised counterpart to [`variance_share`], and the two
/// must not be confused. `variance_share` measures variance along a direction
/// fitted *using the gold ranks*, so it is bounded above by this number and
/// says nothing on its own about whether the items have a dominant axis.
/// Reporting both separates "these items lie along one axis" from "that axis
/// is the one the ranks pick out".
pub fn pc1_share(x: &Matrix, prng: &mut Prng) -> f64 {
    let total = x.frobenius_sq();
    if total == 0.0 {
        return 0.0;
    }
    let comps = top_components(x, 1, prng);
    let along: f64 = (0..x.rows).map(|i| dot(x.row(i), &comps[0]).powi(2)).sum();
    along / total
}

/// A permutation of `values`, drawn with Fisher-Yates from `prng`.
pub fn shuffled(values: &[f64], prng: &mut Prng) -> Vec<f64> {
    let mut out = values.to_vec();
    for i in (1..out.len()).rev() {
        let j = (prng.next_u64() % (i as u64 + 1)) as usize;
        out.swap(i, j);
    }
    out
}

/// The normalized direction a scale's pooled items imply for `gold`.
///
/// Exposed so that directions can be exported and compared across scales,
/// which is a different question from whether any single scale is ordered.
pub fn fitted_direction(contexts: &[&Matrix], gold: &[f64], comps: &[Vec<f64>]) -> Vec<f64> {
    let pooled = mean_matrix(contexts);
    let x = prepared(&pooled, comps);
    fit_direction(&x, gold)
}

// ------------------------------------------------------------- subspaces

/// Gram matrix of a set of unit directions.
///
/// Entry (i, j) is the cosine between directions i and j, so the diagonal is
/// one and the eigenvalues are the squared singular values of the stacked
/// direction matrix. With n directions in d dimensions and n much smaller than
/// d, working on the n by n Gram matrix rather than the d by d covariance is
/// both exact and trivially cheap.
pub fn gram(directions: &[Vec<f64>]) -> Matrix {
    let n = directions.len();
    let mut g = Matrix::zeros(n, n);
    for i in 0..n {
        for j in 0..n {
            g.row_mut(i)[j] = dot(&directions[i], &directions[j]);
        }
    }
    g
}

/// Mean absolute off-diagonal of a Gram matrix: how much the directions
/// overlap on average, ignoring sign, since a direction's sign is arbitrary.
pub fn mean_abs_offdiagonal(g: &Matrix) -> f64 {
    let n = g.rows;
    if n < 2 {
        return 0.0;
    }
    let mut total = 0.0;
    for i in 0..n {
        for j in 0..n {
            if i != j {
                total += g.row(i)[j].abs();
            }
        }
    }
    total / (n * (n - 1)) as f64
}

/// Eigenvalues of a symmetric matrix by cyclic Jacobi rotation, descending.
///
/// Jacobi rather than anything cleverer because the matrices here are at most
/// a few dozen rows, it needs no dependency, and it is accurate on exactly the
/// near-degenerate spectra this analysis produces.
pub fn symmetric_eigenvalues(matrix: &Matrix) -> Vec<f64> {
    assert_eq!(matrix.rows, matrix.cols, "eigenvalues need a square matrix");
    let n = matrix.rows;
    let mut a = matrix.clone();
    for _ in 0..100 {
        let mut off = 0.0;
        for i in 0..n {
            for j in (i + 1)..n {
                off += a.row(i)[j].powi(2);
            }
        }
        if off.sqrt() < 1e-13 {
            break;
        }
        for p in 0..n {
            for q in (p + 1)..n {
                let apq = a.row(p)[q];
                if apq.abs() < 1e-18 {
                    continue;
                }
                let theta = 0.5 * (a.row(q)[q] - a.row(p)[p]) / apq;
                // At theta == 0 the standard formula degenerates to t = 0,
                // which would skip a rotation that is actually needed.
                let t = if theta.abs() < 1e-30 {
                    1.0
                } else {
                    theta.signum() / (theta.abs() + (theta * theta + 1.0).sqrt())
                };
                let c = 1.0 / (t * t + 1.0).sqrt();
                let s = t * c;
                for k in 0..n {
                    let akp = a.row(k)[p];
                    let akq = a.row(k)[q];
                    a.row_mut(k)[p] = c * akp - s * akq;
                    a.row_mut(k)[q] = s * akp + c * akq;
                }
                for k in 0..n {
                    let apk = a.row(p)[k];
                    let aqk = a.row(q)[k];
                    a.row_mut(p)[k] = c * apk - s * aqk;
                    a.row_mut(q)[k] = s * apk + c * aqk;
                }
            }
        }
    }
    let mut eigenvalues: Vec<f64> = (0..n).map(|i| a.row(i)[i]).collect();
    eigenvalues.sort_by(|x, y| y.partial_cmp(x).expect("NaN eigenvalue"));
    eigenvalues
}

/// Participation ratio of a spectrum: an effective dimensionality that needs
/// no threshold. It is n for n orthogonal directions and 1 when they all
/// coincide, and it moves smoothly in between.
pub fn participation_ratio(eigenvalues: &[f64]) -> f64 {
    let sum: f64 = eigenvalues.iter().map(|v| v.max(0.0)).sum();
    let sum_sq: f64 = eigenvalues.iter().map(|v| v.max(0.0).powi(2)).sum();
    if sum_sq == 0.0 {
        return 0.0;
    }
    sum * sum / sum_sq
}

/// How many leading eigenvalues it takes to reach `fraction` of the total.
pub fn components_for_fraction(eigenvalues: &[f64], fraction: f64) -> usize {
    let total: f64 = eigenvalues.iter().map(|v| v.max(0.0)).sum();
    if total <= 0.0 {
        return 0;
    }
    let mut cumulative = 0.0;
    for (index, value) in eigenvalues.iter().enumerate() {
        cumulative += value.max(0.0);
        if cumulative / total >= fraction {
            return index + 1;
        }
    }
    eigenvalues.len()
}

#[allow(clippy::too_many_arguments)]
pub fn measure(
    scale_id: &str,
    condition: &str,
    items: &Matrix,
    gold: &[f64],
    comps: &[Vec<f64>],
    random_draws: usize,
    prng: &mut Prng,
) -> ScaleResult {
    let mut x = items.clone();
    if !comps.is_empty() {
        remove_components(&mut x, comps);
    }
    x.center();

    let w = fit_direction(&x, gold);
    let in_sample = spearman(&project(&x, &w), gold);
    let loo = spearman(&loo_scores(&x, gold), gold);
    let share = variance_share(&x, &w);
    // PC1 is an additional diagnostic.  Compute it from a cloned stream so
    // adding the diagnostic cannot perturb any pre-existing Monte Carlo or
    // bootstrap result produced from `prng` below.
    let mut pc1_prng = prng.clone();
    let unsupervised = pc1_share(&x, &mut pc1_prng);

    let mut at_least = 0usize;
    for _ in 0..random_draws {
        let r = prng.unit_vector(x.cols);
        if spearman(&project(&x, &r), gold).abs() >= in_sample.abs() {
            at_least += 1;
        }
    }
    // A rate, so no add-one smoothing: smoothing belongs on a p-value and
    // would misrepresent this as one.
    let match_rate = at_least as f64 / random_draws.max(1) as f64;

    let k = gold.len();
    let total = factorial(k);
    let exact = total <= EXACT_PERMUTATION_LIMIT;
    let (p_perm, used) = if exact {
        let mut labels: Vec<f64> = gold.to_vec();
        labels.sort_by(|a, b| a.partial_cmp(b).unwrap());
        let mut count = 0u64;
        let mut seen = 0u64;
        loop {
            let s = spearman(&loo_scores(&x, &labels), &labels);
            if s >= loo {
                count += 1;
            }
            seen += 1;
            if !next_permutation(&mut labels) {
                break;
            }
        }
        (count as f64 / seen as f64, seen)
    } else {
        let draws = 10_000u64;
        let mut labels: Vec<f64> = gold.to_vec();
        let mut count = 0u64;
        for _ in 0..draws {
            for i in (1..labels.len()).rev() {
                let j = (prng.next_u64() % (i as u64 + 1)) as usize;
                labels.swap(i, j);
            }
            if spearman(&loo_scores(&x, &labels), &labels) >= loo {
                count += 1;
            }
        }
        ((count as f64 + 1.0) / (draws as f64 + 1.0), draws)
    };

    ScaleResult {
        scale_id: scale_id.to_string(),
        condition: condition.to_string(),
        remove_top: comps.len(),
        n_items: k,
        dim: items.cols,
        spearman_in_sample: in_sample,
        spearman_loo: loo,
        variance_share: share,
        pc1_share: unsupervised,
        random_direction_match_rate: match_rate,
        random_draws,
        p_permutation: p_perm,
        permutations: used,
        permutation_exact: exact,
        n_contexts: 1,
        spearman_loo_ci_low: None,
        spearman_loo_ci_high: None,
        bootstrap_draws: 0,
        per_context_loo_mean: None,
        per_context_loo_sd: None,
    }
}

/// Measure a scale from several contexts.
///
/// The point estimate comes from the element-wise mean across contexts, which
/// is the scale as the model represents it on average. The interval comes from
/// resampling whole contexts, not from resampling the handful of items, since
/// contexts are the replicated unit and the items are not exchangeable: their
/// ordering is the thing being tested.
///
/// The per-context spread is reported separately and is not an error bar. It
/// answers a different question, namely how much the syntactic frame and the
/// subject noun move the gradient at all.
#[allow(clippy::too_many_arguments)]
pub fn measure_contexts(
    scale_id: &str,
    condition: &str,
    contexts: &[Matrix],
    gold: &[f64],
    comps: &[Vec<f64>],
    random_draws: usize,
    bootstrap_draws: usize,
    prng: &mut Prng,
) -> ScaleResult {
    assert!(!contexts.is_empty(), "a scale needs at least one context");
    let refs: Vec<&Matrix> = contexts.iter().collect();
    let pooled = mean_matrix(&refs);
    let mut result = measure(
        scale_id,
        condition,
        &pooled,
        gold,
        comps,
        random_draws,
        prng,
    );
    result.n_contexts = contexts.len();

    if contexts.len() > 1 {
        let per: Vec<f64> = contexts
            .iter()
            .map(|m| {
                let x = prepared(m, comps);
                spearman(&loo_scores(&x, gold), gold)
            })
            .collect();
        result.per_context_loo_mean = Some(mean_of(&per));
        result.per_context_loo_sd = Some(sd_of(&per));

        if bootstrap_draws > 0 {
            let n = contexts.len();
            let mut stats = Vec::with_capacity(bootstrap_draws);
            for _ in 0..bootstrap_draws {
                let picked: Vec<&Matrix> = (0..n)
                    .map(|_| &contexts[(prng.next_u64() % n as u64) as usize])
                    .collect();
                let x = prepared(&mean_matrix(&picked), comps);
                stats.push(spearman(&loo_scores(&x, gold), gold));
            }
            stats.sort_by(|a, b| a.partial_cmp(b).expect("NaN in bootstrap"));
            result.spearman_loo_ci_low = Some(percentile(&stats, 2.5));
            result.spearman_loo_ci_high = Some(percentile(&stats, 97.5));
            result.bootstrap_draws = bootstrap_draws;
        }
    }
    result
}

// ---------------------------------------------------------------- the tests

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ranks_average_ties() {
        assert_eq!(rank_of(&[1.0, 2.0, 2.0, 4.0]), vec![1.0, 2.5, 2.5, 4.0]);
    }

    #[test]
    fn spearman_is_one_on_a_monotone_pair() {
        let a = [0.1, 0.5, 0.9, 2.0];
        let b = [1.0, 2.0, 3.0, 4.0];
        assert!((spearman(&a, &b) - 1.0).abs() < 1e-12);
    }

    #[test]
    fn spearman_is_minus_one_when_reversed() {
        let a = [4.0, 3.0, 2.0, 1.0];
        let b = [1.0, 2.0, 3.0, 4.0];
        assert!((spearman(&a, &b) + 1.0).abs() < 1e-12);
    }

    /// A planted gradient: items lie along one axis with noise elsewhere, so
    /// the fitted direction should recover the order out of sample.
    #[test]
    fn recovers_a_planted_direction() {
        let mut prng = Prng::new(7);
        let dim = 64;
        let k = 6;
        let mut rows = Vec::new();
        for i in 0..k {
            let mut v: Vec<f64> = (0..dim).map(|_| 0.05 * prng.next_normal()).collect();
            v[0] += i as f64;
            rows.push(v);
        }
        let refs: Vec<&[f64]> = rows.iter().map(|r| r.as_slice()).collect();
        let items = Matrix::from_rows(&refs);
        let gold: Vec<f64> = (1..=k).map(|i| i as f64).collect();
        let result = measure("planted", "bare", &items, &gold, &[], 200, &mut prng);
        assert!(result.spearman_loo > 0.9, "loo was {}", result.spearman_loo);
        assert!(
            result.variance_share > 0.9,
            "share was {}",
            result.variance_share
        );
        assert!(result.permutation_exact);
        assert_eq!(result.permutations, 720);
        assert!(
            result.p_permutation < 0.01,
            "p was {}",
            result.p_permutation
        );
    }

    /// The same procedure on scrambled labels must not find anything. This is
    /// the control that catches a fitting procedure that fits noise.
    #[test]
    fn finds_nothing_in_noise() {
        let mut prng = Prng::new(11);
        let dim = 64;
        let k = 6;
        let rows: Vec<Vec<f64>> = (0..k)
            .map(|_| (0..dim).map(|_| prng.next_normal()).collect())
            .collect();
        let refs: Vec<&[f64]> = rows.iter().map(|r| r.as_slice()).collect();
        let items = Matrix::from_rows(&refs);
        let gold: Vec<f64> = (1..=k).map(|i| i as f64).collect();
        let result = measure("noise", "bare", &items, &gold, &[], 200, &mut prng);
        assert!(
            result.p_permutation > 0.05,
            "p was {}",
            result.p_permutation
        );
    }

    /// Many noisy copies of the same planted gradient. The pooled estimate
    /// should be solid, the interval should exclude zero, and the per-context
    /// spread should be reported rather than mistaken for an error bar.
    #[test]
    fn contexts_give_an_interval_and_a_spread() {
        let mut prng = Prng::new(23);
        let dim = 48;
        let k = 6;
        let n_contexts = 12;
        let contexts: Vec<Matrix> = (0..n_contexts)
            .map(|_| {
                let rows: Vec<Vec<f64>> = (0..k)
                    .map(|i| {
                        let mut v: Vec<f64> = (0..dim).map(|_| 0.4 * prng.next_normal()).collect();
                        v[0] += i as f64;
                        v
                    })
                    .collect();
                let refs: Vec<&[f64]> = rows.iter().map(|r| r.as_slice()).collect();
                Matrix::from_rows(&refs)
            })
            .collect();
        let gold: Vec<f64> = (1..=k).map(|i| i as f64).collect();

        let result = measure_contexts(
            "planted",
            "carrier",
            &contexts,
            &gold,
            &[],
            200,
            500,
            &mut prng,
        );
        assert_eq!(result.n_contexts, n_contexts);
        assert_eq!(result.bootstrap_draws, 500);
        let low = result.spearman_loo_ci_low.expect("interval missing");
        let high = result.spearman_loo_ci_high.expect("interval missing");
        assert!(low <= high);
        assert!(low > 0.0, "interval included zero: [{low}, {high}]");
        assert!(result.per_context_loo_mean.is_some());
        assert!(result.per_context_loo_sd.unwrap() >= 0.0);
        // Pooling across contexts averages the noise away, so the pooled
        // estimate should be at least as good as a single context typically is.
        assert!(result.spearman_loo >= result.per_context_loo_mean.unwrap() - 1e-9);
    }

    /// One context is not a sample, so no interval is invented for it.
    #[test]
    fn a_single_context_gets_no_interval() {
        let mut prng = Prng::new(29);
        let rows: Vec<Vec<f64>> = (0..5)
            .map(|i| {
                let mut v: Vec<f64> = (0..16).map(|_| 0.1 * prng.next_normal()).collect();
                v[0] += i as f64;
                v
            })
            .collect();
        let refs: Vec<&[f64]> = rows.iter().map(|r| r.as_slice()).collect();
        let only = Matrix::from_rows(&refs);
        let gold: Vec<f64> = (1..=5).map(|i| i as f64).collect();
        let result = measure_contexts("one", "bare", &[only], &gold, &[], 100, 500, &mut prng);
        assert_eq!(result.n_contexts, 1);
        assert_eq!(result.bootstrap_draws, 0);
        assert!(result.spearman_loo_ci_low.is_none());
        assert!(result.per_context_loo_sd.is_none());
    }

    /// The random-direction rate runs opposite to intuition, and that is the
    /// point. A planted gradient makes the items nearly collinear, so many
    /// arbitrary directions order them correctly and the rate is high. Noise
    /// makes the fitted direction overfit to a value random directions cannot
    /// match, so the rate collapses. If this test ever flips, someone has
    /// started treating the rate as a significance test.
    #[test]
    fn random_direction_rate_is_a_collinearity_diagnostic() {
        let mut prng = Prng::new(101);
        let dim = 64;
        let k = 6;
        let gold: Vec<f64> = (1..=k).map(|i| i as f64).collect();

        let planted: Vec<Vec<f64>> = (0..k)
            .map(|i| {
                let mut v: Vec<f64> = (0..dim).map(|_| 0.05 * prng.next_normal()).collect();
                v[0] += i as f64;
                v
            })
            .collect();
        let refs: Vec<&[f64]> = planted.iter().map(|r| r.as_slice()).collect();
        let signal = measure(
            "planted",
            "bare",
            &Matrix::from_rows(&refs),
            &gold,
            &[],
            2_000,
            &mut prng,
        );

        let noisy: Vec<Vec<f64>> = (0..k)
            .map(|_| (0..dim).map(|_| prng.next_normal()).collect())
            .collect();
        let refs: Vec<&[f64]> = noisy.iter().map(|r| r.as_slice()).collect();
        let noise = measure(
            "noise",
            "bare",
            &Matrix::from_rows(&refs),
            &gold,
            &[],
            2_000,
            &mut prng,
        );

        assert!(
            signal.random_direction_match_rate > noise.random_direction_match_rate,
            "signal rate {} should exceed noise rate {}",
            signal.random_direction_match_rate,
            noise.random_direction_match_rate
        );
        assert!(
            noise.random_direction_match_rate < 0.05,
            "noise rate was {}",
            noise.random_direction_match_rate
        );
    }

    #[test]
    fn eigenvalues_of_a_diagonal_matrix() {
        let mut m = Matrix::zeros(3, 3);
        m.row_mut(0)[0] = 3.0;
        m.row_mut(1)[1] = 1.0;
        m.row_mut(2)[2] = 2.0;
        let e = symmetric_eigenvalues(&m);
        for (got, want) in e.iter().zip([3.0, 2.0, 1.0]) {
            assert!((got - want).abs() < 1e-10, "got {e:?}");
        }
    }

    #[test]
    fn orthogonal_directions_span_everything() {
        let dim = 8;
        let n = 4;
        let dirs: Vec<Vec<f64>> = (0..n)
            .map(|i| {
                let mut v = vec![0.0; dim];
                v[i] = 1.0;
                v
            })
            .collect();
        let g = gram(&dirs);
        let e = symmetric_eigenvalues(&g);
        assert!((participation_ratio(&e) - n as f64).abs() < 1e-9);
        assert!(mean_abs_offdiagonal(&g) < 1e-12);
        assert_eq!(components_for_fraction(&e, 0.9), n);
    }

    #[test]
    fn identical_directions_collapse_to_one_dimension() {
        let d = vec![1.0, 0.0, 0.0, 0.0];
        let dirs = vec![d.clone(), d.clone(), d.clone(), d];
        let e = symmetric_eigenvalues(&gram(&dirs));
        assert!((participation_ratio(&e) - 1.0).abs() < 1e-9);
        assert_eq!(components_for_fraction(&e, 0.9), 1);
    }

    /// The verification the subspace analysis needs before it is trusted on
    /// real directions: directions drawn from a known low-dimensional subspace
    /// must produce an effective dimensionality near that number, and far
    /// below the count of directions.
    #[test]
    fn recovers_a_planted_shared_subspace() {
        let mut prng = Prng::new(555);
        let dim = 128;
        let n = 22;
        let planted = 3;
        let basis: Vec<Vec<f64>> = (0..planted)
            .map(|i| {
                let mut v = vec![0.0; dim];
                v[i] = 1.0;
                v
            })
            .collect();
        let dirs: Vec<Vec<f64>> = (0..n)
            .map(|_| {
                let mut v = vec![0.0; dim];
                for b in &basis {
                    let weight = prng.next_normal();
                    for (x, y) in v.iter_mut().zip(b) {
                        *x += weight * y;
                    }
                }
                normalize(&mut v);
                v
            })
            .collect();
        let e = symmetric_eigenvalues(&gram(&dirs));
        let pr = participation_ratio(&e);
        assert!(pr > 1.5 && pr < 4.0, "participation ratio was {pr}");
        assert_eq!(components_for_fraction(&e, 0.99), planted);
    }

    /// The matched contrast: independent directions in the same dimension must
    /// not look shared, or the measure above would be meaningless.
    #[test]
    fn independent_directions_do_not_look_shared() {
        let mut prng = Prng::new(556);
        let dirs: Vec<Vec<f64>> = (0..22).map(|_| prng.unit_vector(128)).collect();
        let pr = participation_ratio(&symmetric_eigenvalues(&gram(&dirs)));
        assert!(pr > 15.0, "participation ratio was {pr}");
    }

    /// The supervised share can never beat the leading principal component,
    /// because that component maximises variance by construction. If this ever
    /// fails, the two numbers have been swapped somewhere.
    #[test]
    fn supervised_share_cannot_exceed_pc1() {
        let mut prng = Prng::new(557);
        let dim = 32;
        let k = 6;
        let rows: Vec<Vec<f64>> = (0..k)
            .map(|i| {
                let mut v: Vec<f64> = (0..dim).map(|_| 0.6 * prng.next_normal()).collect();
                v[0] += i as f64;
                v
            })
            .collect();
        let refs: Vec<&[f64]> = rows.iter().map(|r| r.as_slice()).collect();
        let items = Matrix::from_rows(&refs);
        let gold: Vec<f64> = (1..=k).map(|i| i as f64).collect();
        let result = measure("x", "bare", &items, &gold, &[], 50, &mut prng);
        assert!(
            result.variance_share <= result.pc1_share + 1e-9,
            "supervised {} exceeded pc1 {}",
            result.variance_share,
            result.pc1_share
        );
        assert!(result.pc1_share > 0.0);
    }

    #[test]
    fn component_removal_kills_a_dominant_axis() {
        let mut prng = Prng::new(3);
        let dim = 32;
        // Background dominated by axis 0.
        let bg_rows: Vec<Vec<f64>> = (0..400)
            .map(|_| {
                let mut v: Vec<f64> = (0..dim).map(|_| 0.1 * prng.next_normal()).collect();
                v[0] += 5.0 * prng.next_normal();
                v
            })
            .collect();
        let bg_refs: Vec<&[f64]> = bg_rows.iter().map(|r| r.as_slice()).collect();
        let mut background = Matrix::from_rows(&bg_refs);
        background.center();
        let comps = top_components(&background, 1, &mut prng);
        assert!(
            comps[0][0].abs() > 0.9,
            "top component was {:?}",
            &comps[0][..3]
        );

        let mut x = Matrix::from_rows(&[&[1.0; 32][..]]);
        remove_components(&mut x, &comps);
        assert!(dot(x.row(0), &comps[0]).abs() < 1e-9);
    }
}
