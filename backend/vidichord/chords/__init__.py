"""Chord recognition: three engines, HMM fusion, and noise cleanup."""

from .beats import BeatGrid, track
from .cleanup import CleanupConfig, NoiseMetrics, clean, measure
from .fusion import (
    EmissionWeights,
    FusionConfig,
    KeyPrior,
    TransitionWeights,
    build_transition_matrix,
    decode,
)
from .vocabulary import (
    QUALITIES,
    ROOTS,
    all_states,
    chord_tones,
    diatonic_chords,
    normalize_chord,
    parse_key,
    split_chord,
    triad_of,
)

__all__ = [
    "BeatGrid",
    "track",
    "CleanupConfig",
    "NoiseMetrics",
    "clean",
    "measure",
    "FusionConfig",
    "EmissionWeights",
    "TransitionWeights",
    "KeyPrior",
    "decode",
    "build_transition_matrix",
    "ROOTS",
    "QUALITIES",
    "all_states",
    "split_chord",
    "normalize_chord",
    "triad_of",
    "chord_tones",
    "diatonic_chords",
    "parse_key",
]
