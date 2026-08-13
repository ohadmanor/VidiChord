"""The three chord-recognition engines, each reduced to beat-level labels.

None of them is reliable alone, and they fail in different ways, which is why
their outputs are fused rather than one being trusted:

``librosa``
    Chroma template matching. Rich vocabulary (sevenths, sus, slash chords) but
    easily confused by percussion, so it runs on the harmonic component of an
    HPSS split rather than the raw mix.

``Essentia``
    A bundled native binary. Triads only, but it also reports the key, which
    the fusion step uses as a prior.

``madmom``
    A CNN feature extractor with a CRF decoder. Triads only, and strongly
    smoothed, so it anchors the others when they disagree.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..models import NO_CHORD
from .vocabulary import (
    QUALITY_INTERVALS,
    ROOTS,
    chord_tones,
    normalize_chord,
    parse_madmom_label,
    split_chord,
)

_HOP_LENGTH = 512

#: The rate madmom's models were trained at; its front-ends hard-code it.
_MADMOM_SR = 44100

#: Essentia emits one chord frame per 2048 samples at 44.1 kHz.
ESSENTIA_FRAME_SECONDS = 2048.0 / 44100.0

#: Qualities are down-weighted so unusual ones need stronger evidence to win.
_QUALITY_PRIOR = {
    "": 1.00, "m": 1.00,
    "7": 0.85, "maj7": 0.80, "m7": 0.80,
    "sus4": 0.80, "sus2": 0.80,
    "dim": 0.40, "aug": 0.40,
}

#: Template correlation below which a beat is called "no chord".
_MIN_SIMILARITY = 0.35

#: Bass note must be this prominent before a slash chord is emitted.
_MIN_BASS_ENERGY = 0.18
_MIN_BASS_SHARE = 0.40


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def harmonic_component(y: np.ndarray):
    """Return the harmonic part of the signal, with percussion removed.

    Drum transients smear energy across every chroma bin; removing them before
    template matching is the cheapest large reduction in chord noise.
    """
    import librosa

    try:
        harmonic, _percussive = librosa.effects.hpss(y)
        return harmonic
    except Exception as exc:  # pragma: no cover - depends on signal length
        print(f"HPSS failed, using the raw mix: {exc}", file=sys.stderr)
        return y


def running_mode(labels: list[str], window: int = 3) -> list[str]:
    """Median-style smoothing over a sequence of labels.

    Ties keep the original label so the filter never invents a chord that was
    not predicted at that position.
    """
    if window <= 1:
        return list(labels)

    half = window // 2
    out: list[str] = []
    for index in range(len(labels)):
        neighbourhood = labels[max(0, index - half): index + half + 1]
        counts = Counter(neighbourhood).most_common()
        if len(counts) > 1 and counts[0][1] == counts[1][1]:
            tied = [label for label, count in counts if count == counts[0][1]]
            out.append(labels[index] if labels[index] in tied else tied[0])
        else:
            out.append(counts[0][0])
    return out


def _beat_frame_bounds(start: float, end: float, sr: float, n_frames: int) -> tuple[int, int]:
    first = int(round(start * sr / _HOP_LENGTH))
    last = int(round(end * sr / _HOP_LENGTH))
    first = max(0, min(first, n_frames - 1))
    last = max(0, min(last, n_frames - 1))
    return first, last


# ---------------------------------------------------------------------------
# librosa
# ---------------------------------------------------------------------------


def _build_templates() -> tuple[list[str], np.ndarray, np.ndarray]:
    """Chroma templates and quality priors for every chord state."""
    states: list[str] = []
    templates: list[np.ndarray] = []
    priors: list[float] = []

    for root_index, root in enumerate(ROOTS):
        for quality, intervals in QUALITY_INTERVALS.items():
            template = np.zeros(12)
            for step in intervals:
                template[(root_index + step) % 12] = 1.0
            template /= np.linalg.norm(template)

            states.append(f"{root}{quality}")
            templates.append(template)
            priors.append(_QUALITY_PRIOR.get(quality, 0.5))

    return states, np.array(templates), np.array(priors)


#: Chroma is computed at this rate rather than the mix's native 44.1 kHz.
#: The information chroma reads tops out well below the 11 kHz Nyquist this
#: leaves, and HPSS plus two CQTs are roughly twice as fast on half the
#: samples - they are the bulk of this engine's cost.
_CHROMA_SR = 22050


@dataclass
class ChromaFeatures:
    """Frame-level features, computed once and mapped onto any beat grid."""

    chroma: np.ndarray
    bass_chroma: np.ndarray
    sr: float


def librosa_chord_features(y: np.ndarray, sr: float) -> ChromaFeatures:
    """The frame-level half of the librosa engine.

    Needs no beat grid, so it can run concurrently with beat tracking.
    """
    import librosa

    if sr != _CHROMA_SR:
        y = librosa.resample(y, orig_sr=sr, target_sr=_CHROMA_SR)
        sr = _CHROMA_SR

    harmonic = harmonic_component(y)
    chroma = librosa.feature.chroma_cqt(y=harmonic, sr=sr, hop_length=_HOP_LENGTH)

    # A separate low-register CQT gives the bass note for slash chords.
    bass_cqt = np.abs(
        librosa.cqt(y=harmonic, sr=sr, fmin=32.7, n_bins=36, hop_length=_HOP_LENGTH)
    )
    bass_chroma = np.zeros((12, bass_cqt.shape[1]))
    for pitch in range(12):
        bass_chroma[pitch, :] = np.sum(bass_cqt[pitch::12, :], axis=0)

    return ChromaFeatures(chroma=chroma, bass_chroma=bass_chroma, sr=float(sr))


def librosa_beat_chords(
    features: ChromaFeatures, intervals: list[tuple[float, float]]
) -> list[str]:
    """Template-match chroma against chord templates, one label per beat."""
    states, templates, priors = _build_templates()
    chroma, bass_chroma, sr = features.chroma, features.bass_chroma, features.sr

    n_beats = len(intervals)
    beat_chroma = np.zeros((12, n_beats))
    beat_bass = np.zeros((12, n_beats))

    for index, (start, end) in enumerate(intervals):
        first, last = _beat_frame_bounds(start, end, sr, chroma.shape[1])
        if first >= last:
            beat_chroma[:, index] = chroma[:, first]
            beat_bass[:, index] = bass_chroma[:, first]
        else:
            beat_chroma[:, index] = np.mean(chroma[:, first:last + 1], axis=1)
            beat_bass[:, index] = np.mean(bass_chroma[:, first:last + 1], axis=1)

    normalised = beat_chroma / (np.linalg.norm(beat_chroma, axis=0) + 1e-9)
    similarity = templates @ normalised

    raw: list[str] = []
    for index in range(n_beats):
        if np.sum(beat_chroma[:, index]) < 1e-3:
            raw.append(NO_CHORD)
            continue
        best = int(np.argmax(similarity[:, index] * priors))
        raw.append(NO_CHORD if similarity[best, index] < _MIN_SIMILARITY else states[best])

    return _add_slash_chords(running_mode(raw), beat_bass)


def _add_slash_chords(labels: list[str], beat_bass: np.ndarray) -> list[str]:
    """Promote a chord to a slash chord when the bass sits on a chord tone."""
    out: list[str] = []
    for index, label in enumerate(labels):
        root, quality, _ = split_chord(label)
        if root == NO_CHORD:
            out.append(NO_CHORD)
            continue

        bass_vector = beat_bass[:, index]
        normalised = bass_vector / (np.linalg.norm(bass_vector) + 1e-9)
        bass_index = int(np.argmax(normalised))
        bass_note = ROOTS[bass_index]

        is_inversion = (
            bass_note != root
            and bass_note in chord_tones(root, quality)
            and float(np.max(bass_vector)) > _MIN_BASS_ENERGY
            and normalised[bass_index] > _MIN_BASS_SHARE
        )
        out.append(normalize_chord(f"{label}/{bass_note}" if is_inversion else label))
    return out


# ---------------------------------------------------------------------------
# Essentia
# ---------------------------------------------------------------------------


def run_essentia(binary: Path, audio_path: Path) -> tuple[dict, dict]:
    """Run the bundled Essentia extractor, returning its summary and frames."""
    if not binary.is_file():
        raise FileNotFoundError(f"Essentia binary not found at {binary}")

    with tempfile.TemporaryDirectory(prefix="vidichord-essentia-") as workspace:
        out_path = Path(workspace) / "essentia.json"

        env_path = str(binary.parent)
        env = os.environ.copy()
        if env_path not in env.get("PATH", ""):
            env["PATH"] = env_path + os.pathsep + env.get("PATH", "")

        result = subprocess.run(
            [str(binary), str(audio_path), str(out_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Essentia exited with code {result.returncode}: "
                f"{(result.stderr or '').strip()}"
            )
        if not out_path.is_file():
            raise FileNotFoundError("Essentia reported success but wrote no output")

        with out_path.open("r", encoding="utf-8") as handle:
            summary = json.load(handle)

        # The extractor writes per-frame data to a sibling file.
        frames: dict = {}
        for candidate in (
            out_path.with_name(out_path.name + "_frames"),
            out_path.with_name(out_path.name + "_frames.json"),
        ):
            if candidate.is_file():
                try:
                    with candidate.open("r", encoding="utf-8") as handle:
                        frames = json.load(handle)
                except ValueError as exc:
                    print(f"Unreadable Essentia frames file: {exc}", file=sys.stderr)
                break

        return summary, frames


def smooth_essentia_frames(labels: list[str]) -> list[str]:
    """Viterbi-smooth Essentia's frame-level output over its 24 triads."""
    import librosa

    triads = [f"{root}{quality}" for root in ROOTS for quality in ("", "m")]
    index_of = {label: index for index, label in enumerate(triads)}
    n_states, n_frames = len(triads), len(labels)
    if n_frames == 0:
        return labels

    try:
        emissions = np.full((n_states, n_frames), 0.05 / (n_states - 1))
        for frame, label in enumerate(labels):
            cleaned = label.replace(NO_CHORD, "")
            position = index_of.get(cleaned)
            if position is None:
                emissions[:, frame] = 1.0 / n_states
            else:
                emissions[position, frame] = 0.95

        transitions = np.full((n_states, n_states), 0.05 / (n_states - 1))
        np.fill_diagonal(transitions, 0.95)

        path = librosa.sequence.viterbi(emissions, transitions)
        return [triads[index] for index in path]
    except Exception as exc:  # pragma: no cover - numerical edge cases
        print(f"Essentia smoothing skipped: {exc}", file=sys.stderr)
        return labels


def map_essentia_to_beats(
    frames: list[str], frame_seconds: float, intervals: list[tuple[float, float]]
) -> list[str]:
    """Reduce frame-level Essentia labels to one label per beat by majority."""
    out: list[str] = []
    for start, end in intervals:
        first = int(round(start / frame_seconds))
        last = int(round(end / frame_seconds))
        window = [label for label in frames[first:last + 1] if label]
        if not window:
            out.append(NO_CHORD)
            continue

        ranked = Counter(window).most_common(2)
        # Prefer a real chord over silence when silence only just leads.
        if (
            ranked[0][0] == NO_CHORD
            and len(ranked) > 1
            and ranked[1][1] > len(window) * 0.3
        ):
            out.append(normalize_chord(ranked[1][0]))
        else:
            out.append(normalize_chord(ranked[0][0]))
    return out


# ---------------------------------------------------------------------------
# madmom
# ---------------------------------------------------------------------------


def madmom_segments(y_44k: np.ndarray) -> list[tuple[float, float, str]]:
    """Recognise chords with madmom's CNN features and CRF decoder."""
    from ._madmom_compat import require_madmom

    require_madmom()

    from madmom.audio.signal import Signal
    from madmom.features.chords import (
        CNNChordFeatureProcessor,
        CRFChordRecognitionProcessor,
    )
    from madmom.processors import SequentialProcessor

    signal = Signal(np.asarray(y_44k, dtype=np.float32), sample_rate=_MADMOM_SR)
    recognise = SequentialProcessor(
        [CNNChordFeatureProcessor(), CRFChordRecognitionProcessor()]
    )
    return [(float(s), float(e), str(label)) for s, e, label in recognise(signal)]


#: Audio either side of a chunk that is analysed but not reported, so the CNN
#: and CRF see full context at every reported label. Their temporal horizon is
#: a couple of seconds; ten is comfortably beyond it.
_CHUNK_OVERLAP_SECONDS = 10.0

#: Songs shorter than this are not worth the worker start-up cost.
_MIN_PARALLEL_SECONDS = 120.0


def _chord_workers() -> int:
    from ..config import PROCESS_POOLS_OK, int_env

    default = min(4, max(1, (os.cpu_count() or 4) // 4)) if PROCESS_POOLS_OK else 1
    return int_env("VIDICHORD_CHORD_WORKERS", default)


def _madmom_chunk(
    chunk: np.ndarray, offset: float, core_start: float, core_end: float
) -> list[tuple[float, float, str]]:
    """Worker-process entry: recognise one chunk, report only its core."""
    out: list[tuple[float, float, str]] = []
    for start, end, label in madmom_segments(chunk):
        start = max(start + offset, core_start)
        end = min(end + offset, core_end)
        if end > start:
            out.append((start, end, label))
    return out


def madmom_segments_parallel(y_44k: np.ndarray) -> list[tuple[float, float, str]]:
    """Chord recognition split across worker processes.

    madmom's nets are pure NumPy driven by Python loops that never release the
    GIL, so threads cannot speed them up - only processes can. The signal is
    cut into per-worker chunks whose overlap is trimmed away on merge; chord
    boundaries are only consumed at beat resolution (~0.4 s), far coarser than
    any drift chunking can introduce. Falls back to a single process whenever
    workers are unavailable or not worth their start-up cost.
    """
    duration = len(y_44k) / _MADMOM_SR
    workers = _chord_workers()
    if workers <= 1 or duration < _MIN_PARALLEL_SECONDS:
        return madmom_segments(y_44k)

    core_edges = np.linspace(0.0, duration, workers + 1)
    jobs = []
    for index in range(workers):
        core_start, core_end = float(core_edges[index]), float(core_edges[index + 1])
        padded_start = max(0.0, core_start - _CHUNK_OVERLAP_SECONDS)
        padded_end = min(duration, core_end + _CHUNK_OVERLAP_SECONDS)
        chunk = y_44k[int(padded_start * _MADMOM_SR): int(padded_end * _MADMOM_SR)]
        jobs.append((chunk, padded_start, core_start, core_end))

    try:
        from concurrent.futures import ProcessPoolExecutor

        with ProcessPoolExecutor(max_workers=workers) as pool:
            parts = list(pool.map(_madmom_chunk, *zip(*jobs)))
    except Exception as exc:  # pragma: no cover - depends on host process
        print(f"Parallel chord recognition failed ({exc}); "
              "falling back to one process", file=sys.stderr)
        return madmom_segments(y_44k)

    return _coalesce([segment for part in parts for segment in part])


def _coalesce(
    segments: list[tuple[float, float, str]]
) -> list[tuple[float, float, str]]:
    """Merge segments a chunk boundary cut in two.

    Beat mapping picks each beat's label by its single largest overlapping
    segment, so a chord split at a seam would count as two half-weight
    segments and could lose to a chord it actually outweighs.
    """
    merged: list[tuple[float, float, str]] = []
    for start, end, label in segments:
        if merged and merged[-1][2] == label and abs(merged[-1][1] - start) < 1e-6:
            merged[-1] = (merged[-1][0], end, label)
        else:
            merged.append((start, end, label))
    return merged


def map_madmom_to_beats(
    segments: list[tuple[float, float, str]], intervals: list[tuple[float, float]]
) -> list[str]:
    """Assign each beat the madmom segment it overlaps most."""
    out: list[str] = []
    for start, end in intervals:
        best_label = NO_CHORD
        best_overlap = 0.0
        for seg_start, seg_end, label in segments:
            overlap = min(end, seg_end) - max(start, seg_start)
            if overlap > best_overlap:
                best_overlap = overlap
                best_label = label
        out.append(parse_madmom_label(best_label))
    return out
