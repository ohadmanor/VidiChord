"""Stage 3 - chords on a beat and bar grid.

Three engines each predict a chord per beat; an HMM fuses them; a cleanup pass
removes the flicker that per-beat decoding leaves behind. The beat grid itself
comes from downbeat tracking, so bars line up with the music rather than being
counted off from the first chord.

Every engine is optional. If madmom is unavailable the grid falls back to
librosa's beat tracker and the fusion runs on two engines instead of three -
degraded, but still useful.
"""

from __future__ import annotations

import sys

import numpy as np

from ..chords import beats as beats_mod
from ..chords import cleanup as cleanup_mod
from ..chords import engines
from ..chords.fusion import FusionConfig, decode
from ..config import ESSENTIA_BIN
from ..models import Bar, Beat, ChordsDoc, SourceDoc, NO_CHORD
from . import StageContext


def _load_audio(path: str) -> tuple[np.ndarray, float, np.ndarray, float]:
    """Load the track at its native rate and at 44.1 kHz for madmom."""
    import librosa
    import soundfile as sf

    info = sf.info(path)
    y_native, sr_native = librosa.load(path, sr=None)
    if int(sr_native) == 44100:
        y_44k = y_native
    else:
        y_44k = librosa.resample(y_native, orig_sr=sr_native, target_sr=44100)
    return y_native, float(sr_native), y_44k, float(info.duration)


def _essentia_predictions(
    audio_path: str, intervals: list[tuple[float, float]], context: StageContext
) -> tuple[list[str] | None, str]:
    """Run the Essentia binary; returns beat labels and the detected key.

    Labels are None when the engine could not run, so the fusion step treats it
    as silent rather than as reporting silence.
    """
    try:
        summary, frames = engines.run_essentia(ESSENTIA_BIN, audio_path)
    except Exception as exc:
        context.report(f"Essentia unavailable: {exc}", None)
        return None, ""

    tonal = summary.get("tonal", {})
    key_info = tonal.get("key_edma") or tonal.get("key_krumhansl") or {}
    key = f"{key_info.get('key', '')} {key_info.get('scale', '')}".strip()

    labels = (frames.get("tonal", {}) or {}).get("chords_progression") or tonal.get(
        "chords_progression"
    ) or []
    if not labels:
        return None, key

    smoothed = engines.smooth_essentia_frames(list(labels))
    beat_labels = engines.map_essentia_to_beats(
        smoothed, engines.ESSENTIA_FRAME_SECONDS, intervals
    )
    return beat_labels, key


def _madmom_predictions(
    y_44k: np.ndarray, intervals: list[tuple[float, float]], context: StageContext
) -> list[str] | None:
    try:
        segments = engines.madmom_segments(y_44k)
    except Exception as exc:
        context.report(f"madmom chord engine unavailable: {exc}", None)
        return None
    return engines.map_madmom_to_beats(segments, intervals)


def _build_bars(grid: beats_mod.BeatGrid, labels: list[str]) -> list[Bar]:
    """Group beats into bars using the tracked bar positions."""
    intervals = grid.intervals()
    bars: list[Bar] = []

    for number, group in enumerate(grid.bar_groups(), start=1):
        beat_models = [
            Beat(
                index=index,
                beat_in_bar=grid.beat_in_bar[index],
                start=round(intervals[index][0], 4),
                end=round(intervals[index][1], 4),
                chord=labels[index],
            )
            for index in group
        ]
        bars.append(
            Bar(
                index=number,
                start=beat_models[0].start,
                end=beat_models[-1].end,
                beats=beat_models,
            )
        )
    return bars


def run(context: StageContext) -> None:
    project = context.project
    audio_path = str(project.audio_path)
    if not project.audio_path.is_file():
        raise RuntimeError("Stage 1 must run before chords can be extracted")

    config = context.param("fusion") or FusionConfig()
    if isinstance(config, dict):
        config = FusionConfig.model_validate(config)

    cleanup_config = context.param("cleanup") or cleanup_mod.CleanupConfig()
    if isinstance(cleanup_config, dict):
        cleanup_config = cleanup_mod.CleanupConfig.model_validate(cleanup_config)

    context.report("Loading audio...", 5.0)
    y_native, sr_native, y_44k, duration = _load_audio(audio_path)
    source = project.read_optional(SourceDoc)
    if source is not None and source.duration <= 0:
        source.duration = duration
        project.write(source)

    context.report("Tracking beats and downbeats...", 15.0)
    grid = beats_mod.track(y_44k, y_native, sr_native, duration)
    intervals = grid.intervals()
    context.report(
        f"{len(grid)} beats at {grid.bpm:.1f} BPM, {grid.time_signature}/4"
        + ("" if grid.tracked else " (estimated bar lines)"),
        25.0,
    )

    context.report("Separating harmonic content...", 30.0)
    harmonic = engines.harmonic_component(y_native)

    context.report("Running librosa chord estimation...", 40.0)
    librosa_labels = engines.librosa_beat_chords(harmonic, sr_native, intervals)

    context.report("Running Essentia...", 55.0)
    essentia_labels, key = _essentia_predictions(audio_path, intervals, context)

    context.report("Running madmom chord recognition...", 70.0)
    madmom_labels = _madmom_predictions(y_44k, intervals, context)

    engine_names = [
        name
        for name, labels in (
            ("librosa", librosa_labels),
            ("essentia", essentia_labels),
            ("madmom", madmom_labels),
        )
        if labels is not None
    ]
    context.report(f"Fusing {', '.join(engine_names)}...", 82.0)
    fused = decode(librosa_labels, essentia_labels, madmom_labels, config, key=key)

    before = cleanup_mod.measure(fused, bars=len(grid.bar_groups()))
    context.report("Removing chord noise...", 90.0)
    cleaned = cleanup_mod.clean(fused, grid.bar_groups(), cleanup_config)
    after = cleanup_mod.measure(cleaned, bars=len(grid.bar_groups()))
    print(
        f"Chord noise: {before.summary()} -> {after.summary()}",
        file=sys.stderr,
    )

    available = {
        name: labels
        for name, labels in (
            ("librosa", librosa_labels),
            ("essentia", essentia_labels),
            ("madmom", madmom_labels),
        )
        if labels is not None
    }
    bars = _build_bars(grid, cleaned)
    for bar, group in zip(bars, grid.bar_groups()):
        for beat, index in zip(bar.beats, group):
            beat.sources = {name: labels[index] for name, labels in available.items()}

    project.write(
        ChordsDoc(
            bpm=grid.bpm,
            time_signature=grid.time_signature,
            key=key,
            pickup_beats=grid.pickup_beats,
            downbeats_tracked=grid.tracked,
            bars=bars,
        )
    )

    context.report(
        f"{len(bars)} bars, key {key or 'unknown'}, {after.summary()}.", 100.0
    )
