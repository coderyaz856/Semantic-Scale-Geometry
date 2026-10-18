//! `sg-measure`: read one shard, measure every scale in it, write results.
//!
//! One invocation is one grid cell's worth of work, which is what makes the
//! cloud fan-out trivial: the array index picks a shard directory and the
//! process writes a single output file. There is no coordinator and no shared
//! state. A re-run overwrites its own output and nothing else, so an
//! interrupted sweep is resumed by running it again.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use clap::Parser;
use serde::{Deserialize, Serialize};
use sg_measure::{
    background_components, fitted_direction, measure_contexts, shuffled, Matrix, Prng, ScaleResult,
};

#[derive(Parser, Debug)]
#[command(about = "Measure linguistic gradients in one embedding shard")]
struct Args {
    /// Shard directory containing shard.json and embeddings.f32
    #[arg(long)]
    shard: PathBuf,

    /// Output JSON file
    #[arg(long)]
    out: PathBuf,

    /// Numbers of dominant background components to remove, comma separated
    #[arg(long, default_value = "0,1,3,10")]
    remove_top: String,

    /// Random directions drawn for the null
    #[arg(long, default_value_t = 10_000)]
    random_draws: usize,

    /// Bootstrap resamples over contexts, for the interval
    #[arg(long, default_value_t = 2_000)]
    bootstrap_draws: usize,

    /// Master seed. One seed fixes the whole run.
    #[arg(long, default_value_t = 1)]
    seed: u64,

    /// Also export the fitted directions to this directory, for the
    /// shared-subspace analysis. Exports the gold-fitted direction, two
    /// half-of-the-contexts directions for stability, and shuffled-label
    /// directions as a matched null.
    #[arg(long)]
    directions: Option<PathBuf>,

    /// Shuffled-label replicates exported per scale, as the null the observed
    /// spectrum is compared against. Matched on everything except the
    /// ordering: same items, same contexts, same preprocessing, same fit.
    #[arg(long, default_value_t = 50)]
    null_replicates: usize,
}

#[derive(Debug, Serialize)]
struct DirectionMeta {
    scale_id: String,
    condition: String,
    remove_top: usize,
    /// "fitted", "half_a", "half_b" or "null".
    kind: String,
    replicate: usize,
}

#[derive(Debug, Serialize)]
struct DirectionIndex {
    format: &'static str,
    model: String,
    layer: usize,
    dim: usize,
    pooling: String,
    seed: u64,
    null_replicates: usize,
    rows: Vec<DirectionMeta>,
}

/// Directions are unit vectors, so comparing them across scales only makes
/// sense inside one model, one layer and one preprocessing setting. The index
/// records all three, and the subspace tool groups on them rather than
/// trusting the caller.
struct DirectionWriter {
    rows: Vec<DirectionMeta>,
    payload: Vec<f32>,
}

impl DirectionWriter {
    fn new() -> Self {
        Self {
            rows: Vec::new(),
            payload: Vec::new(),
        }
    }

    fn push(
        &mut self,
        scale_id: &str,
        condition: &str,
        remove_top: usize,
        kind: &str,
        replicate: usize,
        direction: &[f64],
    ) {
        self.rows.push(DirectionMeta {
            scale_id: scale_id.to_string(),
            condition: condition.to_string(),
            remove_top,
            kind: kind.to_string(),
            replicate,
        });
        self.payload.extend(direction.iter().map(|v| *v as f32));
    }
}

#[derive(Debug, Deserialize)]
struct RowMeta {
    scale_id: String,
    rank: i64,
    condition: String,
    context: String,
}

#[derive(Debug, Deserialize)]
struct Shard {
    format: String,
    model: String,
    layer: usize,
    dim: usize,
    n_rows: usize,
    n_background: usize,
    pooling: String,
    rows: Vec<RowMeta>,
}

#[derive(Debug, Serialize)]
struct Output {
    model: String,
    layer: usize,
    pooling: String,
    dim: usize,
    seed: u64,
    results: Vec<ScaleResult>,
}

/// Cell seed derived from the master seed and the cell's identity, so cells are
/// order independent and a single seed reproduces the whole sweep.
fn cell_seed(master: u64, parts: &[&str]) -> u64 {
    let mut h: u64 = master ^ 0x517C_C1B7_2722_0A95;
    for part in parts {
        for byte in part.as_bytes() {
            h ^= *byte as u64;
            h = h.wrapping_mul(0x0100_0000_01B3);
        }
        h ^= 0xFF;
    }
    h | 1
}

fn read_shard(dir: &Path) -> Result<(Shard, Vec<f32>), String> {
    let meta_path = dir.join("shard.json");
    let text =
        fs::read_to_string(&meta_path).map_err(|e| format!("{}: {e}", meta_path.display()))?;
    let shard: Shard =
        serde_json::from_str(&text).map_err(|e| format!("{}: {e}", meta_path.display()))?;
    if shard.format != "scalar-geometry/shard/1" {
        return Err(format!("unsupported shard format: {}", shard.format));
    }
    if shard.rows.len() != shard.n_rows {
        return Err(format!(
            "sidecar says {} rows but lists {}",
            shard.n_rows,
            shard.rows.len()
        ));
    }

    let bin_path = dir.join("embeddings.f32");
    let bytes = fs::read(&bin_path).map_err(|e| format!("{}: {e}", bin_path.display()))?;
    let expected = (shard.n_rows + shard.n_background) * shard.dim * 4;
    if bytes.len() != expected {
        return Err(format!(
            "{}: expected {expected} bytes, found {}",
            bin_path.display(),
            bytes.len()
        ));
    }
    let values: Vec<f32> = bytes
        .as_chunks::<4>()
        .0
        .iter()
        .map(|c| f32::from_le_bytes(*c))
        .collect();
    Ok((shard, values))
}

fn slice_rows(values: &[f32], dim: usize, indices: &[usize]) -> Matrix {
    let mut m = Matrix::zeros(indices.len(), dim);
    for (target, &source) in indices.iter().enumerate() {
        let src = &values[source * dim..(source + 1) * dim];
        for (dst, v) in m.row_mut(target).iter_mut().zip(src) {
            *dst = *v as f64;
        }
    }
    m
}

fn main() {
    let args = Args::parse();
    let removals: Vec<usize> = args
        .remove_top
        .split(',')
        .filter(|s| !s.trim().is_empty())
        .map(|s| s.trim().parse().expect("remove-top must be integers"))
        .collect();

    let (shard, values) = match read_shard(&args.shard) {
        Ok(pair) => pair,
        Err(message) => {
            eprintln!("sg-measure: {message}");
            std::process::exit(2);
        }
    };

    // Group row indices by scale and condition, then by context. Rows sharing
    // a scale, a condition and a context are one complete copy of the scale,
    // which is the unit the bootstrap resamples.
    type Members = Vec<(i64, usize)>;
    let mut groups: BTreeMap<(String, String), BTreeMap<String, Members>> = BTreeMap::new();
    for (index, row) in shard.rows.iter().enumerate() {
        groups
            .entry((row.scale_id.clone(), row.condition.clone()))
            .or_default()
            .entry(row.context.clone())
            .or_default()
            .push((row.rank, index));
    }

    let background_indices: Vec<usize> =
        (shard.n_rows..shard.n_rows + shard.n_background).collect();
    let background = if background_indices.is_empty() {
        None
    } else {
        Some(slice_rows(&values, shard.dim, &background_indices))
    };

    // Dominant directions depend on the model, the layer and the number
    // removed, never on the scale, so they are computed once per shard here
    // rather than inside the scale loop.
    let mut components: BTreeMap<usize, Vec<Vec<f64>>> = BTreeMap::new();
    for &remove_top in &removals {
        if remove_top == 0 {
            components.insert(0, Vec::new());
            continue;
        }
        let Some(bg) = background.as_ref() else {
            eprintln!("sg-measure: remove-top {remove_top} needs background rows; skipping");
            continue;
        };
        let seed = cell_seed(
            args.seed,
            &[
                &shard.model,
                &shard.layer.to_string(),
                "components",
                &remove_top.to_string(),
            ],
        );
        let mut prng = Prng::new(seed);
        components.insert(remove_top, background_components(bg, remove_top, &mut prng));
    }

    let mut results = Vec::new();
    let mut directions = DirectionWriter::new();
    for ((scale_id, condition), by_context) in groups {
        let mut contexts = Vec::with_capacity(by_context.len());
        let mut gold: Option<Vec<f64>> = None;
        let mut bad = false;
        for (context_id, mut members) in by_context {
            members.sort_by_key(|(rank, _)| *rank);
            let ranks: Vec<f64> = members.iter().map(|(rank, _)| *rank as f64).collect();
            match &gold {
                None => gold = Some(ranks),
                Some(first) if *first != ranks => {
                    eprintln!(
                        "sg-measure: {scale_id}/{condition}/{context_id} has a \
                         different set of ranks; skipping the scale"
                    );
                    bad = true;
                    break;
                }
                Some(_) => {}
            }
            let indices: Vec<usize> = members.iter().map(|(_, i)| *i).collect();
            contexts.push(slice_rows(&values, shard.dim, &indices));
        }
        if bad {
            continue;
        }
        let Some(gold) = gold else { continue };

        for (&remove_top, comps) in &components {
            let seed = cell_seed(
                args.seed,
                &[
                    &shard.model,
                    &shard.layer.to_string(),
                    &scale_id,
                    &condition,
                    &remove_top.to_string(),
                ],
            );
            let mut prng = Prng::new(seed);
            results.push(measure_contexts(
                &scale_id,
                &condition,
                &contexts,
                &gold,
                comps,
                args.random_draws,
                args.bootstrap_draws,
                &mut prng,
            ));

            if args.directions.is_none() {
                continue;
            }
            let refs: Vec<&Matrix> = contexts.iter().collect();
            directions.push(
                &scale_id,
                &condition,
                remove_top,
                "fitted",
                0,
                &fitted_direction(&refs, &gold, comps),
            );

            // Stability across contexts: fit on one random half, fit on the
            // other, and let the subspace tool take the cosine. A scale whose
            // direction depends on which contexts it saw is not a direction.
            if contexts.len() >= 4 {
                let mut split = Prng::new(seed ^ 0xA5A5_A5A5_A5A5_A5A5);
                let mut order: Vec<usize> = (0..contexts.len()).collect();
                for i in (1..order.len()).rev() {
                    let j = (split.next_u64() % (i as u64 + 1)) as usize;
                    order.swap(i, j);
                }
                let middle = order.len() / 2;
                for (kind, part) in [("half_a", &order[..middle]), ("half_b", &order[middle..])] {
                    let half: Vec<&Matrix> = part.iter().map(|&i| &contexts[i]).collect();
                    directions.push(
                        &scale_id,
                        &condition,
                        remove_top,
                        kind,
                        0,
                        &fitted_direction(&half, &gold, comps),
                    );
                }
            }

            // The matched null: the identical fit on a shuffled ordering.
            let mut null_prng = Prng::new(seed ^ 0x5A5A_5A5A_5A5A_5A5A);
            for replicate in 0..args.null_replicates {
                let permuted = shuffled(&gold, &mut null_prng);
                directions.push(
                    &scale_id,
                    &condition,
                    remove_top,
                    "null",
                    replicate,
                    &fitted_direction(&refs, &permuted, comps),
                );
            }
        }
    }

    if let Some(dir) = &args.directions {
        fs::create_dir_all(dir).expect("could not create directions directory");
        let index = DirectionIndex {
            format: "scalar-geometry/directions/1",
            model: shard.model.clone(),
            layer: shard.layer,
            dim: shard.dim,
            pooling: shard.pooling.clone(),
            seed: args.seed,
            null_replicates: args.null_replicates,
            rows: directions.rows,
        };
        let mut bytes = Vec::with_capacity(directions.payload.len() * 4);
        for value in &directions.payload {
            bytes.extend_from_slice(&value.to_le_bytes());
        }
        fs::write(dir.join("directions.f32"), &bytes).expect("could not write directions payload");
        let json = serde_json::to_string_pretty(&index).expect("serialisation failed");
        fs::write(dir.join("directions.json"), json + "\n")
            .expect("could not write directions index");
        eprintln!(
            "sg-measure: {} directions -> {}",
            index.rows.len(),
            dir.display()
        );
    }

    let output = Output {
        model: shard.model,
        layer: shard.layer,
        pooling: shard.pooling,
        dim: shard.dim,
        seed: args.seed,
        results,
    };
    if let Some(parent) = args.out.parent() {
        fs::create_dir_all(parent).expect("could not create output directory");
    }
    let json = serde_json::to_string_pretty(&output).expect("serialisation failed");
    fs::write(&args.out, json + "\n").expect("could not write output");
    eprintln!(
        "sg-measure: {} results -> {}",
        output.results.len(),
        args.out.display()
    );
}
