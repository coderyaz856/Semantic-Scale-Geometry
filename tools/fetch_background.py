"""Fetch a background corpus for the anisotropy control.

The dominant directions of an embedding space have to be estimated from text
that has nothing to do with the scales. Estimated from the scale items they
would absorb the gradient along with the artifact, and the control would
quietly return nothing.

WikiText is used because it is public, citable and stylistically neutral. But
it ships space-tokenized, with punctuation detached ("Chronicles ( Japanese :")
and hyphens encoded as "@-@", and it contains passages in other scripts. None
of that is natural English, and feeding it in raw would estimate the dominant
directions from a region of the embedding space that the scale sentences never
occupy, which is exactly the mismatch this control exists to avoid. So the text
is detokenized and filtered before use, and every filter is recorded alongside
the corpus.

    python tools/fetch_background.py --count 2000
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DATASET = "Salesforce/wikitext"
CONFIG = "wikitext-2-raw-v1"

# WikiText encodes hyphens and decimal separators inside tokens as @-@, @,@
# and @.@ so that its tokenizer can split on them.
# The surrounding whitespace is consumed too, otherwise "role @-@ playing"
# becomes "role - playing" rather than "role-playing".
TOKEN_JOIN = re.compile(r"\s*@([-,.])@\s*")
SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.;:!?%)\]}])")
SPACE_AFTER_OPEN = re.compile(r"([(\[{])\s+")
MULTISPACE = re.compile(r"\s{2,}")
CLITICS = (
    (" 's", "'s"),
    (" 't", "'t"),
    (" n't", "n't"),
    (" 'll", "'ll"),
    (" 're", "'re"),
    (" 've", "'ve"),
    (" 'd", "'d"),
    (" 'm", "'m"),
)

# Split only where a terminal mark is followed by something that starts a
# sentence. This keeps "lit ." and "No. 3" from cutting a sentence in half.
SENTENCE_SPLIT = re.compile(r'(?<=[.!?])\s+(?=[A-Z"\'(])')
HEADING = re.compile(r"^\s*=")

LATIN_RATIO = 0.97
MIN_WORDS = 6


def detokenize(text: str) -> str:
    text = TOKEN_JOIN.sub(r"\1", text)
    text = SPACE_BEFORE_PUNCT.sub(r"\1", text)
    text = SPACE_AFTER_OPEN.sub(r"\1", text)
    for spaced, joined in CLITICS:
        text = text.replace(spaced, joined)
    return MULTISPACE.sub(" ", text).strip()


def pick_train_file(files: list[str]) -> str:
    candidates = [
        f for f in files if CONFIG in f and f.endswith(".parquet") and "train" in f
    ]
    if not candidates:
        raise SystemExit(
            f"no train parquet for {CONFIG} in {DATASET}; files looked like: "
            f"{files[:10]}"
        )
    return min(candidates)


def usable(sentence: str, min_chars: int, max_chars: int) -> bool:
    if not sentence or HEADING.match(sentence):
        return False
    if not min_chars <= len(sentence) <= max_chars:
        return False
    if sentence[-1] not in ".!?":
        return False
    if not sentence[0].isupper():
        return False
    if "<unk>" in sentence or "@" in sentence:
        return False
    if len(sentence.split()) < MIN_WORDS:
        return False
    # Drop passages in other scripts: the control needs the English region of
    # the space, not a multilingual average of it.
    if sum(1 for c in sentence if ord(c) < 128) < LATIN_RATIO * len(sentence):
        return False
    # Require real lower-case content, which drops tables and heading debris.
    return sum(c.islower() for c in sentence) >= len(sentence) // 3


def sentences_from(table, min_chars: int, max_chars: int, count: int) -> list[str]:
    seen: set[str] = set()
    kept: list[str] = []
    for chunk in table.column("text"):
        values = chunk.to_pylist() if hasattr(chunk, "to_pylist") else [chunk]
        for raw in values:
            if not raw:
                continue
            for piece in SENTENCE_SPLIT.split(str(raw)):
                sentence = detokenize(piece)
                if not usable(sentence, min_chars, max_chars) or sentence in seen:
                    continue
                seen.add(sentence)
                kept.append(sentence)
                if len(kept) >= count:
                    return kept
    return kept


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=2000)
    parser.add_argument("--min-chars", type=int, default=40)
    parser.add_argument("--max-chars", type=int, default=240)
    parser.add_argument("--out", default="data/background/wikitext2.txt")
    args = parser.parse_args(argv)

    try:
        import pyarrow.parquet as pq
        from huggingface_hub import HfApi, hf_hub_download
    except ImportError as exc:  # pragma: no cover - depends on the environment
        print(
            f"sg: {exc}. Install the extraction dependencies first:\n"
            "    pip install transformers pyarrow",
            file=sys.stderr,
        )
        return 2

    api = HfApi()
    info = api.dataset_info(DATASET)
    revision = info.sha
    filename = pick_train_file([s.rfilename for s in info.siblings])
    local = hf_hub_download(
        repo_id=DATASET,
        filename=filename,
        repo_type="dataset",
        revision=revision,
    )

    table = pq.read_table(local, columns=["text"])
    kept = sentences_from(table, args.min_chars, args.max_chars, args.count)
    if len(kept) < args.count:
        print(
            f"sg: only {len(kept)} sentences passed the filters, wanted {args.count}",
            file=sys.stderr,
        )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(kept) + "\n", encoding="utf-8")

    provenance = {
        "dataset": DATASET,
        "config": CONFIG,
        "file": filename,
        "revision": revision,
        "sentences": len(kept),
        "cleaning": {
            "detokenized": "punctuation rejoined, @-@ artifacts resolved, "
            "clitics reattached",
            "sentence_split": SENTENCE_SPLIT.pattern,
        },
        "filters": {
            "min_chars": args.min_chars,
            "max_chars": args.max_chars,
            "min_words": MIN_WORDS,
            "latin_ratio": LATIN_RATIO,
            "must_start_uppercase": True,
            "must_end_terminal": True,
            "drop_headings": True,
            "drop_unk": True,
            "drop_remaining_at_sign": True,
            "deduplicated": True,
        },
    }
    out.with_suffix(".provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {len(kept)} sentences to {out}")
    print(f"provenance in {out.with_suffix('.provenance.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
