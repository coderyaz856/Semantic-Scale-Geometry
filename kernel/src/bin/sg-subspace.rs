//! `sg-subspace`: do different linguistic scales reuse a smaller set of
//! directions?
//!
//! Reads the directions `sg-measure --directions` exports and, for each group
//! of one model, one layer, one condition and one preprocessing setting,
//! reports:
//!
//! * the mean absolute pairwise cosine between scale directions, which is how
//!   much they overlap at all;
//! * the singular spectrum of the stacked directions, summarised as a
//!   participation ratio and as the count of components needed to carry 90%
//!   and 99% of the energy;
//! * the same figures for shuffled-label replicates, which is the matched
//!   null: identical items, contexts, preprocessing and fitting procedure,
//!   with only the ordering destroyed;
//! * the cosine between directions fitted on two disjoint halves of the
//!   contexts, because a direction that depends on which contexts it saw is
//!   not a direction.
//!
//! Two limits are printed with every result rather than left to the reader.
//! With n scales the directions can span at most n dimensions, so this
//! estimates the dimensionality of *these selected scales*, not of semantics.
//! And each scale's direction necessarily lies inside the row space of its own
//! centred item matrix, which has rank at most k-1; the shuffled-label null is
//! matched on that too, which is the reason to prefer it over random vectors.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use clap::Parser;
use serde::{Deserialize, Serialize};
use sg_measure::{
    components_for_fraction, dot, gram, mean_abs_offdiagonal, participation_ratio,
    symmetric_eigenvalues,
};

#[derive(Parser, Debug)]
#[command(about = "Shared-subspace analysis over exported scale directions")]
struct Args {
    /// One or more directories written by `sg-measure --directions`
    directions: Vec<PathBuf>,

    /// Write the full analysis here as JSON
    #[arg(long)]
    out: Option<PathBuf>,

    /// Condition to analyse; directions are only comparable within one
    #[arg(long, default_value = "carrier")]
    condition: String,
}

#[derive(Debug, Deserialize)]
struct DirectionMeta {
    scale_id: String,
    condition: String,
    remove_top: usize,
    kind: String,
    replicate: usize,
}

#[derive(Debug, Deserialize)]
struct DirectionIndex {
    format: String,
    model: String,
    layer: usize,
    dim: usize,
    pooling: String,
    null_replicates: usize,
    rows: Vec<DirectionMeta>,
}

#[derive(Debug, Serialize)]
struct Spectrum {
    n_directions: usize,
    mean_abs_cosine: f64,
    participation_ratio: f64,
    components_90: usize,
    components_99: usize,
    eigenvalues: Vec<f64>,
}

#[derive(Debug, Serialize)]
struct GroupReport {
    model: String,
    layer: usize,
    pooling: String,
    condition: String,
    remove_top: usize,
    dim: usize,
    observed: Spectrum,
    null_replicates: usize,
    null_participation_mean: f64,
    null_participation_sd: f64,
    null_mean_abs_cosine_mean: f64,
    /// One-sided: the share of shuffled-label replicates whose participation
    /// ratio is at or below the observed one. Sharing predicts a *lower*
    /// ratio, so a small value here is the evidence for sharing.
    p_participation: f64,
    n_half_pairs: usize,
    half_split_cosine_median: Option<f64>,
}

fn mean(values: &[f64]) -> f64 {
    if values.is_empty() {
        return f64::NAN;
    }
    values.iter().sum::<f64>() / values.len() as f64
}

fn sd(values: &[f64]) -> f64 {
    if values.len() < 2 {
        return 0.0;
    }
    let m = mean(values);
    (values.iter().map(|v| (v - m).powi(2)).sum::<f64>() / (values.len() - 1) as f64).sqrt()
}

fn median(values: &mut [f64]) -> Option<f64> {
    if values.is_empty() {
        return None;
    }
    values.sort_by(|a, b| a.partial_cmp(b).expect("NaN"));
    Some(values[values.len() / 2])
}

fn spectrum(directions: &[Vec<f64>]) -> Spectrum {
    let g = gram(directions);
    let eigenvalues = symmetric_eigenvalues(&g);
    Spectrum {
        n_directions: directions.len(),
        mean_abs_cosine: mean_abs_offdiagonal(&g),
        participation_ratio: participation_ratio(&eigenvalues),
        components_90: components_for_fraction(&eigenvalues, 0.90),
        components_99: components_for_fraction(&eigenvalues, 0.99),
        eigenvalues,
    }
}

fn read_directions(dir: &Path) -> Result<(DirectionIndex, Vec<Vec<f64>>), String> {
    let index_path = dir.join("directions.json");
    let text =
        fs::read_to_string(&index_path).map_err(|e| format!("{}: {e}", index_path.display()))?;
    let index: DirectionIndex =
        serde_json::from_str(&text).map_err(|e| format!("{}: {e}", index_path.display()))?;
    if index.format != "scalar-geometry/directions/1" {
        return Err(format!("unsupported directions format: {}", index.format));
    }

    let payload_path = dir.join("directions.f32");
    let bytes = fs::read(&payload_path).map_err(|e| format!("{}: {e}", payload_path.display()))?;
    let expected = index.rows.len() * index.dim * 4;
    if bytes.len() != expected {
        return Err(format!(
            "{}: expected {expected} bytes, found {}",
            payload_path.display(),
            bytes.len()
        ));
    }
    let values: Vec<f64> = bytes
        .as_chunks::<4>()
        .0
        .iter()
        .map(|c| f32::from_le_bytes(*c) as f64)
        .collect();
    let rows = values
        .chunks(index.dim)
        .map(|chunk| chunk.to_vec())
        .collect();
    Ok((index, rows))
}

fn analyse(index: &DirectionIndex, rows: &[Vec<f64>], condition: &str) -> Vec<GroupReport> {
    let mut removals: Vec<usize> = index
        .rows
        .iter()
        .filter(|m| m.condition == condition)
        .map(|m| m.remove_top)
        .collect();
    removals.sort_unstable();
    removals.dedup();

    let mut reports = Vec::new();
    for removal in removals {
        let mut fitted: Vec<Vec<f64>> = Vec::new();
        let mut nulls: BTreeMap<usize, Vec<Vec<f64>>> = BTreeMap::new();
        let mut halves: BTreeMap<String, (Option<usize>, Option<usize>)> = BTreeMap::new();

        for (position, meta) in index.rows.iter().enumerate() {
            if meta.condition != condition || meta.remove_top != removal {
                continue;
            }
            match meta.kind.as_str() {
                "fitted" => fitted.push(rows[position].clone()),
                "null" => nulls
                    .entry(meta.replicate)
                    .or_default()
                    .push(rows[position].clone()),
                "half_a" => halves.entry(meta.scale_id.clone()).or_default().0 = Some(position),
                "half_b" => halves.entry(meta.scale_id.clone()).or_default().1 = Some(position),
                other => panic!("unknown direction kind: {other}"),
            }
        }
        if fitted.len() < 2 {
            continue;
        }

        let observed = spectrum(&fitted);
        let mut null_ratios = Vec::new();
        let mut null_cosines = Vec::new();
        for replicate in nulls.values() {
            // Only compare like with like: a replicate missing a scale would
            // have a smaller matrix and a mechanically smaller ratio.
            if replicate.len() != fitted.len() {
                continue;
            }
            let s = spectrum(replicate);
            null_ratios.push(s.participation_ratio);
            null_cosines.push(s.mean_abs_cosine);
        }
        let at_or_below = null_ratios
            .iter()
            .filter(|r| **r <= observed.participation_ratio)
            .count();
        let p = (at_or_below as f64 + 1.0) / (null_ratios.len() as f64 + 1.0);

        // The declared count is checked rather than assumed. A replicate is
        // dropped when it does not cover every scale, and silently comparing
        // against fewer replicates than were exported would inflate the
        // smallest p-value this can report.
        if null_ratios.len() != index.null_replicates {
            eprintln!(
                "sg-subspace: layer {} removal {}: {} usable null replicates                  of {} exported",
                index.layer,
                removal,
                null_ratios.len(),
                index.null_replicates
            );
        }

        let mut cosines: Vec<f64> = halves
            .values()
            .filter_map(|(a, b)| match (a, b) {
                (Some(a), Some(b)) => Some(dot(&rows[*a], &rows[*b]).abs()),
                _ => None,
            })
            .collect();
        let n_pairs = cosines.len();

        reports.push(GroupReport {
            model: index.model.clone(),
            layer: index.layer,
            pooling: index.pooling.clone(),
            condition: condition.to_string(),
            remove_top: removal,
            dim: index.dim,
            observed,
            null_replicates: null_ratios.len(),
            null_participation_mean: mean(&null_ratios),
            null_participation_sd: sd(&null_ratios),
            null_mean_abs_cosine_mean: mean(&null_cosines),
            p_participation: p,
            n_half_pairs: n_pairs,
            half_split_cosine_median: median(&mut cosines),
        });
    }
    reports
}

fn main() {
    let args = Args::parse();
    if args.directions.is_empty() {
        eprintln!("sg-subspace: give at least one directions directory");
        std::process::exit(2);
    }

    let mut reports = Vec::new();
    for dir in &args.directions {
        match read_directions(dir) {
            Ok((index, rows)) => reports.extend(analyse(&index, &rows, &args.condition)),
            Err(message) => {
                eprintln!("sg-subspace: {message}");
                std::process::exit(2);
            }
        }
    }
    if reports.is_empty() {
        eprintln!(
            "sg-subspace: nothing to analyse for condition '{}'",
            args.condition
        );
        std::process::exit(2);
    }

    println!(
        "condition {}, {} group(s). n scale directions span at most n dimensions,",
        args.condition,
        reports.len()
    );
    println!("so these are the dimensionalities of the selected scales, not of semantics.");
    println!();
    println!(
        "{:>5}  {:>4}  {:>5}  {:>9}  {:>9}  {:>7}  {:>7}  {:>8}  {:>9}",
        "layer", "rmv", "n", "mean|cos|", "null|cos|", "pr", "null pr", "p", "half cos"
    );
    println!("{}", "-".repeat(84));
    for r in &reports {
        let half = r
            .half_split_cosine_median
            .map(|v| format!("{v:9.3}"))
            .unwrap_or_else(|| "     none".to_string());
        println!(
            "{:>5}  {:>4}  {:>5}  {:>9.3}  {:>9.3}  {:>7.2}  {:>7.2}  {:>8.4}  {}",
            r.layer,
            r.remove_top,
            r.observed.n_directions,
            r.observed.mean_abs_cosine,
            r.null_mean_abs_cosine_mean,
            r.observed.participation_ratio,
            r.null_participation_mean,
            r.p_participation,
            half
        );
    }
    println!();
    println!("pr        participation ratio: effective dimensions, needs no threshold");
    println!("null pr   same statistic on shuffled-label fits, averaged over replicates");
    println!("p         share of null replicates at or below the observed pr, plus one");
    println!("half cos  median |cosine| between directions fitted on disjoint context halves");

    if let Some(path) = &args.out {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).expect("could not create output directory");
        }
        let json = serde_json::to_string_pretty(&reports).expect("serialisation failed");
        fs::write(path, json + "\n").expect("could not write output");
        eprintln!("sg-subspace: wrote {}", path.display());
    }
}
