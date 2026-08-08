"""Chord spelling, parsing and key theory.

One canonical spelling per pitch class keeps the three engines comparable: they
disagree about enharmonics constantly (madmom says ``A#``, Essentia says
``Bb``), and without normalising that away the fusion step would treat
identical chords as conflicting evidence.
"""

from __future__ import annotations

from ..models import NO_CHORD

#: Canonical root spellings, one per pitch class.
ROOTS: tuple[str, ...] = (
    "C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B",
)

#: Chord qualities the fusion decoder can emit, in template order.
QUALITIES: tuple[str, ...] = (
    "", "m", "7", "maj7", "m7", "dim", "aug", "sus4", "sus2",
)

#: Semitone offsets from the root for each quality.
QUALITY_INTERVALS: dict[str, tuple[int, ...]] = {
    "": (0, 4, 7),
    "m": (0, 3, 7),
    "7": (0, 4, 7, 10),
    "maj7": (0, 4, 7, 11),
    "m7": (0, 3, 7, 10),
    "dim": (0, 3, 6),
    "aug": (0, 4, 8),
    "sus4": (0, 5, 7),
    "sus2": (0, 2, 7),
}

#: Enharmonic spellings folded onto the canonical root above.
_ENHARMONIC = {
    "Db": "C#", "D#": "Eb", "Gb": "F#", "G#": "Ab", "A#": "Bb",
    "Cb": "B", "B#": "C", "Fb": "E", "E#": "F",
}

_ROOT_INDEX = {root: index for index, root in enumerate(ROOTS)}

#: Two-character roots, checked before falling back to a single character.
_TWO_CHAR_ROOTS = frozenset(
    root for root in list(ROOTS) + list(_ENHARMONIC) if len(root) == 2
)

#: madmom quality labels mapped onto ours.
_MADMOM_QUALITIES = {
    "maj": "", "min": "m", "dim": "dim", "aug": "aug",
    "maj7": "maj7", "min7": "m7", "7": "7",
    "sus4": "sus4", "sus2": "sus2", "minmaj7": "m",
    "maj6": "", "min6": "m", "hdim7": "dim", "dim7": "dim",
}


def all_states() -> list[str]:
    """Every chord the decoder may emit, plus the no-chord state."""
    return [f"{root}{quality}" for root in ROOTS for quality in QUALITIES] + [NO_CHORD]


def split_chord(label: str) -> tuple[str, str, str]:
    """Split a chord label into ``(root, quality, bass)``.

    Returns ``(NO_CHORD, "", "")`` for anything unparseable, so callers never
    have to guard against malformed engine output.
    """
    if not label:
        return NO_CHORD, "", ""

    text = label.replace("*", "").strip()
    if text in ("", "-", NO_CHORD):
        return NO_CHORD, "", ""

    bass = ""
    if "/" in text:
        text, _, bass = text.partition("/")
        bass = _ENHARMONIC.get(bass, bass)

    if len(text) >= 2 and text[:2] in _TWO_CHAR_ROOTS:
        root, quality = text[:2], text[2:]
    else:
        root, quality = text[:1], text[1:]

    root = _ENHARMONIC.get(root, root)
    if root not in _ROOT_INDEX:
        return NO_CHORD, "", ""
    return root, quality, bass


def normalize_chord(label: str) -> str:
    """Rewrite a chord label using canonical enharmonic spellings."""
    root, quality, bass = split_chord(label)
    if root == NO_CHORD:
        return NO_CHORD
    return f"{root}{quality}/{bass}" if bass else f"{root}{quality}"


def triad_of(label: str) -> str:
    """Reduce a chord to its major/minor triad, dropping extensions and bass.

    The Essentia and madmom engines only predict triads, so their output has to
    be compared against the triad of a richer decoder state.
    """
    root, quality, _bass = split_chord(label)
    if root == NO_CHORD:
        return NO_CHORD
    return f"{root}m" if quality in ("m", "m7", "dim") else root


def chord_tones(root: str, quality: str) -> list[str]:
    """Note names belonging to a chord, or an empty list if unrecognised."""
    if root not in _ROOT_INDEX or quality not in QUALITY_INTERVALS:
        return []
    base = _ROOT_INDEX[root]
    return [ROOTS[(base + step) % 12] for step in QUALITY_INTERVALS[quality]]


def parse_madmom_label(label: str) -> str:
    """Convert a madmom ``root:quality`` label into our spelling."""
    if not label or label == NO_CHORD:
        return NO_CHORD
    root, _, quality = label.partition(":")
    # madmom appends inversions after a slash, e.g. "C:maj/3" - drop them.
    quality = quality.split("/")[0]
    root = _ENHARMONIC.get(root, root)
    if root not in _ROOT_INDEX:
        return NO_CHORD
    return f"{root}{_MADMOM_QUALITIES.get(quality, '')}"


def root_index(root: str) -> int:
    """Pitch-class index of a root, or -1 if unknown."""
    return _ROOT_INDEX.get(root, -1)


# ---------------------------------------------------------------------------
# Key
# ---------------------------------------------------------------------------

_MAJOR_STEPS = (0, 2, 4, 5, 7, 9, 11)
_MAJOR_QUALITIES = ("", "m", "m", "", "", "m", "dim")

_MINOR_STEPS = (0, 2, 3, 5, 7, 8, 10)
_MINOR_QUALITIES = ("m", "dim", "", "m", "m", "", "")


def parse_key(key: str) -> tuple[str, str] | None:
    """Parse ``"G major"`` / ``"E minor"`` into ``(root, mode)``."""
    if not key:
        return None
    parts = key.replace("\t", " ").split()
    if not parts:
        return None

    root = _ENHARMONIC.get(parts[0], parts[0])
    if root not in _ROOT_INDEX:
        return None

    mode = parts[1].lower() if len(parts) > 1 else "major"
    mode = "minor" if mode.startswith("min") else "major"
    return root, mode


def diatonic_chords(key: str) -> set[str]:
    """Triads belonging to a key.

    Minor keys also include the major V and V7 borrowed from harmonic minor -
    a chord so common in popular music that excluding it would misfire more
    often than it helped.
    """
    parsed = parse_key(key)
    if parsed is None:
        return set()

    root, mode = parsed
    base = _ROOT_INDEX[root]
    steps, qualities = (
        (_MAJOR_STEPS, _MAJOR_QUALITIES) if mode == "major" else (_MINOR_STEPS, _MINOR_QUALITIES)
    )

    chords: set[str] = set()
    for step, quality in zip(steps, qualities):
        chords.add(f"{ROOTS[(base + step) % 12]}{quality}")

    if mode == "minor":
        dominant = ROOTS[(base + 7) % 12]
        chords.update({dominant, f"{dominant}7"})

    return chords


def scale_roots(key: str) -> set[str]:
    """Root notes of a key's scale, ignoring chord quality."""
    parsed = parse_key(key)
    if parsed is None:
        return set()
    root, mode = parsed
    base = _ROOT_INDEX[root]
    steps = _MAJOR_STEPS if mode == "major" else _MINOR_STEPS
    return {ROOTS[(base + step) % 12] for step in steps}
