"""Loading, validating and expanding the scale definitions.

The YAML file is the only place an ordering is declared, and it is validated
strictly on load. A silently malformed scale would produce a plausible looking
number, which is worse than a crash.

Expansion is the other job here. A lexical item becomes one row per context,
where a context is a frame crossed with a subject, plus one bare row with no
frame at all. An utterance item becomes one row per object. The row order this
produces is part of the shard contract, so it is deterministic by construction
and tested rather than assumed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

BARE = "bare"
CARRIER = "carrier"
UTTERANCE = "utterance"

BARE_CONTEXT = "bare"


@dataclass(frozen=True)
class Item:
    text: str
    rank: int


@dataclass(frozen=True)
class Scale:
    id: str
    family: str
    polarity: str
    unit: str
    gold_confidence: str
    items: tuple[Item, ...]
    frames: tuple[str, ...] = ()
    subjects: tuple[str, ...] = ()
    objects: tuple[str, ...] = ()
    slot: str | None = None
    note: str | None = None

    @property
    def is_utterance(self) -> bool:
        return self.unit == UTTERANCE

    @property
    def n_contexts(self) -> int:
        """Contexts in the non-bare condition."""
        if self.is_utterance:
            return len(self.objects)
        return len(self.frames) * len(self.subjects)


@dataclass(frozen=True)
class Row:
    """One text to embed, with everything needed to interpret it later.

    ``context`` identifies which replicate the row belongs to. Rows sharing a
    scale, a condition and a context form one complete copy of the scale, and
    that is the unit the bootstrap resamples.
    """

    text: str
    scale_id: str
    rank: int
    condition: str
    context: str


def load_scales(path: str | Path) -> list[Scale]:
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if doc.get("language") != "en":
        raise ValueError("this study is English only; language must be 'en'")

    frames = _parse_frames(doc.get("frames", {}))

    scales: list[Scale] = []
    seen: set[str] = set()
    for raw in doc["scales"]:
        scale = _parse_scale(raw, frames)
        if scale.id in seen:
            raise ValueError(f"duplicate scale id: {scale.id}")
        seen.add(scale.id)
        scales.append(scale)
    if not scales:
        raise ValueError("no scales defined")
    return scales


def _parse_frames(raw: dict) -> dict[str, tuple[str, ...]]:
    frames: dict[str, tuple[str, ...]] = {}
    for slot, templates in raw.items():
        if not templates:
            raise ValueError(f"frame pool '{slot}' is empty")
        for template in templates:
            if "{subj}" not in template or "{item}" not in template:
                raise ValueError(
                    f"frame pool '{slot}': every frame needs {{subj}} and {{item}}, "
                    f"got {template!r}"
                )
        if len(set(templates)) != len(templates):
            raise ValueError(f"frame pool '{slot}' has duplicate frames")
        frames[slot] = tuple(templates)
    return frames


def _parse_scale(raw: dict, frames: dict[str, tuple[str, ...]]) -> Scale:
    sid = raw["id"]
    unit = raw["unit"]
    if unit not in ("lexical", UTTERANCE):
        raise ValueError(f"{sid}: unit must be 'lexical' or 'utterance'")
    if raw["polarity"] not in ("unipolar", "bipolar"):
        raise ValueError(f"{sid}: polarity must be 'unipolar' or 'bipolar'")
    if raw["gold_confidence"] not in ("high", "medium"):
        raise ValueError(f"{sid}: gold_confidence must be 'high' or 'medium'")

    items = tuple(Item(text=i["text"], rank=int(i["rank"])) for i in raw["items"])
    if len(items) < 4:
        raise ValueError(f"{sid}: a scale needs at least four points")
    ranks = sorted(i.rank for i in items)
    if ranks != list(range(1, len(items) + 1)):
        raise ValueError(f"{sid}: ranks must be 1..k with no gaps or ties, got {ranks}")

    slot = raw.get("slot")
    subjects = tuple(raw.get("subjects", ()))
    objects = tuple(raw.get("objects", ()))

    if unit == "lexical":
        if objects:
            raise ValueError(f"{sid}: lexical scales use 'subjects', not 'objects'")
        if not slot:
            raise ValueError(f"{sid}: lexical scales need a 'slot'")
        if slot not in frames:
            raise ValueError(f"{sid}: unknown slot '{slot}'")
        if not subjects:
            raise ValueError(f"{sid}: lexical scales need at least one subject")
        if len(set(subjects)) != len(subjects):
            raise ValueError(f"{sid}: duplicate subjects")
        for item in items:
            if "{" in item.text:
                raise ValueError(f"{sid}/{item.text}: lexical items take no slots")
        scale_frames = frames[slot]
    else:
        if subjects or slot:
            raise ValueError(
                f"{sid}: utterance scales take neither 'slot' nor 'subjects'"
            )
        if not objects:
            raise ValueError(f"{sid}: utterance scales need at least one object")
        if len(set(objects)) != len(objects):
            raise ValueError(f"{sid}: duplicate objects")
        for item in items:
            if "{obj}" not in item.text:
                raise ValueError(f"{sid}/{item.text!r}: utterance items need '{{obj}}'")
        scale_frames = ()

    return Scale(
        id=sid,
        family=raw["family"],
        polarity=raw["polarity"],
        unit=unit,
        gold_confidence=raw["gold_confidence"],
        items=items,
        frames=scale_frames,
        subjects=subjects,
        objects=objects,
        slot=slot,
        note=raw.get("note"),
    )


def capitalize_first(text: str) -> str:
    """Capitalise the first character and leave the rest alone.

    ``str.capitalize`` would lower-case the remainder, which would quietly
    rewrite the items. Subjects are written in mid-sentence form in the YAML so
    that a frame can place them anywhere, and this is what fixes them up when a
    frame places one first.
    """
    return text[:1].upper() + text[1:] if text else text


def contexts_of(scale: Scale) -> list[str]:
    """Context identifiers in a fixed order.

    An identifier is everything ``render`` needs, so there is exactly one
    representation of a context rather than an id and a separate rendering key
    that could drift apart. For an utterance scale it is the object; for a
    lexical scale it is the frame index and the subject.
    """
    if scale.is_utterance:
        return list(scale.objects)
    return [
        f"f{frame_index}:{subject}"
        for frame_index in range(len(scale.frames))
        for subject in scale.subjects
    ]


def render(scale: Scale, item: Item, condition: str, context: str) -> str:
    if condition == BARE:
        return item.text
    if condition == UTTERANCE:
        return capitalize_first(item.text.format(obj=context))
    frame_index, subject = context.split(":", 1)
    frame = scale.frames[int(frame_index[1:])]
    return capitalize_first(frame.format(subj=subject, item=item.text))


def enumerate_rows(scales: list[Scale]) -> list[Row]:
    """Every text that has to be embedded, in a stable order.

    The order is part of the shard contract: the binary payload is row major in
    exactly this sequence. It is scales in file order, then condition, then
    context, then ascending rank, and it depends on nothing outside this file.
    """
    rows: list[Row] = []
    for scale in scales:
        ordered = sorted(scale.items, key=lambda i: i.rank)
        if not scale.is_utterance:
            for item in ordered:
                rows.append(
                    Row(
                        text=render(scale, item, BARE, BARE_CONTEXT),
                        scale_id=scale.id,
                        rank=item.rank,
                        condition=BARE,
                        context=BARE_CONTEXT,
                    )
                )
        condition = UTTERANCE if scale.is_utterance else CARRIER
        for context_id in contexts_of(scale):
            for item in ordered:
                rows.append(
                    Row(
                        text=render(scale, item, condition, context_id),
                        scale_id=scale.id,
                        rank=item.rank,
                        condition=condition,
                        context=context_id,
                    )
                )
    return rows


def summarize(scales: list[Scale]) -> dict:
    """Counts used by the dry run, so the size of a sweep is known before it runs."""
    rows = enumerate_rows(scales)
    return {
        "scales": len(scales),
        "lexical_scales": sum(1 for s in scales if not s.is_utterance),
        "utterance_scales": sum(1 for s in scales if s.is_utterance),
        "items": sum(len(s.items) for s in scales),
        "rows": len(rows),
        "contexts_per_scale": {s.id: s.n_contexts for s in scales},
    }
