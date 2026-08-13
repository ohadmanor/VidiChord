"""Stage 3 - chords on a beat and bar grid.

Three engines each predict a chord per beat; an HMM fuses them; a cleanup pass
removes the flicker that per-beat decoding leaves behind. The beat grid itself
comes from downbeat tracking, so bars line up with the music rather than being
counted off from the first chord.

Every engine is optional. If madmom is unavailable the grid falls back to
librosa's beat tracker and the fusion runs on two engines instead of three -
degraded, but still useful.

Two things keep this stage fast:

* The engines are independent of each other, so Essentia (a subprocess) and
  the librosa features (FFT work that releases the GIL) run concurrently with
  madmom, instead of one after another.
* Each beat's per-engine predictions are stored on the artifact, and the
  engines are deterministic over unchanged audio - so a re-run whose only
  point is different fusion or cleanup settings re-fuses the stored
  predictions in under a second instead of re-running every engine.
"""

from __future__ import annotations

import hashlib
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from ..chords import beats as beats_mod
from ..chords import cleanup as cleanup_mod
from ..chords import engines
from ..chords.fusion import FusionConfig, decode
from ..config import ESSENTIA_BIN
from ..models import Bar, Beat, ChordsDoc, SourceDoc
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


def _essentia_frames(
    audio_path: str, context: StageContext
) -> tuple[list[str] | None, str]:
    """Run the Essentia binary; returns smoothed frame labels and the key.

    Needs no beat grid, so it runs concurrently with beat tracking. Labels are
    None when the engine could not run, so the fusion step treats it as silent
    rather than as reporting silence.
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

    return engines.smooth_essentia_frames(list(labels)), key


def _librosa_features(
    y_native: np.ndarray, sr_native: float, context: StageContext
) -> engines.ChromaFeatures | None:
    try:
        return engines.librosa_chord_features(y_native, sr_native)
    except Exception as exc:
        context.report(f"librosa chord engine unavailable: {exc}", None)
        return None


def _madmom_raw_segments(
    y_44k: np.ndarray, context: StageContext
) -> list[tuple[float, float, str]] | None:
    try:
        return engines.madmom_segments_parallel(y_44k)
    except Exception as exc:
        context.report(f"madmom chord engine unavailable: {exc}", None)
        return None


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


def _fuse_and_clean(
    labels: dict[str, list[str] | None],
    bar_groups: list[list[int]],
    key: str,
    config,
    cleanup_config,
) -> tuple[list[str], cleanup_mod.NoiseMetrics]:
    """Fusion and cleanup, shared verbatim by the fresh and re-fuse paths.

    Both paths must produce identical chords from identical inputs - that is
    the premise that lets a re-run skip the engines - so the sequence lives in
    exactly one place.
    """
    fused = decode(
        labels.get("librosa"),
        labels.get("essentia"),
        labels.get("madmom"),
        config,
        key=key,
    )
    before = cleanup_mod.measure(fused, bars=len(bar_groups))
    cleaned = cleanup_mod.clean(fused, bar_groups, cleanup_config)
    after = cleanup_mod.measure(cleaned, bars=len(bar_groups))
    print(f"Chord noise: {before.summary()} -> {after.summary()}", file=sys.stderr)
    return cleaned, after


#: Bump when any engine's output for the same audio can change (algorithm,
#: sample rate, model), so re-runs stop trusting predictions it stored.
ENGINE_VERSION = 1


def audio_fingerprint(path: Path) -> str:
    """A cheap identity for the audio file's content.

    Size plus the first and last megabyte - enough to notice a re-added local
    file whose content changed, without hashing a whole WAV.
    """
    digest = hashlib.md5()
    size = path.stat().st_size
    digest.update(str(size).encode())
    with path.open("rb") as handle:
        digest.update(handle.read(1 << 20))
        if size > (1 << 20):
            handle.seek(-min(size - (1 << 20), 1 << 20), 2)
            digest.update(handle.read(1 << 20))
    return digest.hexdigest()


def _stored_engine_labels(bars: list[Bar]) -> dict[str, list[str]] | None:
    """Per-engine beat labels recovered from a previous run's artifact.

    Returns None unless every beat carries the same set of engine predictions
    and the beats are numbered 0..N-1 in order - a partial or hand-edited
    record cannot reproduce the original fusion input.
    """
    beats = [beat for bar in bars for beat in bar.beats]
    if not beats:
        return None
    if [beat.index for beat in beats] != list(range(len(beats))):
        return None

    names = set(beats[0].sources or {})
    if not names or any(set(beat.sources or {}) != names for beat in beats):
        return None

    return {name: [beat.sources[name] for beat in beats] for name in names}


def _available_engines() -> set[str]:
    """The engines a fresh run would consult on this machine."""
    from ..chords._madmom_compat import madmom

    names = {"librosa"}
    if ESSENTIA_BIN.is_file():
        names.add("essentia")
    if madmom is not None:
        names.add("madmom")
    return names


def _refuse_stored(context: StageContext, config, cleanup_config) -> bool:
    """Re-fuse a previous run's engine predictions, skipping the engines.

    The engines are deterministic over unchanged audio, so when their per-beat
    predictions are already on the artifact the only work a re-run can add is
    fusion and cleanup - milliseconds, not minutes. This is what makes trying
    out fusion settings interactive. ``force`` runs the engines regardless.

    The stored predictions must come from the same engine line-up a fresh run
    would use: a song first processed while madmom was broken must not be
    locked out of madmom - or of a properly tracked beat grid - forever.
    """
    existing = context.project.read_optional(ChordsDoc)
    if existing is None or not existing.bars:
        return False
    if existing.audio_fingerprint != audio_fingerprint(context.project.audio_path):
        # The audio on disk is not what these predictions were computed from
        # (or the document predates fingerprinting) - run the engines.
        return False
    if existing.engine_version != ENGINE_VERSION:
        # The engines have changed since these predictions were stored.
        return False

    labels = _stored_engine_labels(existing.bars)
    if labels is None or set(labels) != _available_engines():
        return False
    if not existing.downbeats_tracked and "madmom" in labels:
        # The grid was estimated although madmom is available - a fresh run
        # may well track it properly, so don't cement the fallback.
        return False

    context.report("Re-fusing stored engine predictions...", 40.0)
    bar_groups = [[beat.index for beat in bar.beats] for bar in existing.bars]
    cleaned, after = _fuse_and_clean(
        labels, bar_groups, existing.key, config, cleanup_config
    )

    for bar in existing.bars:
        for beat in bar.beats:
            beat.chord = cleaned[beat.index]
    context.project.write(existing)

    context.report(
        f"{len(existing.bars)} bars re-fused from stored predictions, "
        f"key {existing.key or 'unknown'}, {after.summary()}.",
        100.0,
    )
    return True


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

    if not context.param("force", False) and _refuse_stored(context, config, cleanup_config):
        return

    # Every engine except beat tracking runs off the main thread: Essentia is
    # a subprocess, madmom chord recognition fans out to worker processes, and
    # the librosa features are GIL-releasing FFT work. Beat tracking keeps the
    # main thread; the rest overlaps it. Essentia reads the file itself, so it
    # starts before the in-memory audio is even decoded.
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="chords") as pool:
        essentia_future = pool.submit(_essentia_frames, audio_path, context)

        context.report("Loading audio...", 5.0)
        y_native, sr_native, y_44k, duration = _load_audio(audio_path)
        source = project.read_optional(SourceDoc)
        if source is not None and source.duration <= 0:
            source.duration = duration
            project.write(source)

        madmom_future = pool.submit(_madmom_raw_segments, y_44k, context)
        librosa_future = pool.submit(_librosa_features, y_native, sr_native, context)

        context.report("Tracking beats and downbeats...", 15.0)
        grid = beats_mod.track(y_44k, y_native, sr_native, duration)
        intervals = grid.intervals()
        context.report(
            f"{len(grid)} beats at {grid.bpm:.1f} BPM, {grid.time_signature}/4"
            + ("" if grid.tracked else " (estimated bar lines)"),
            40.0,
        )

        context.report("Collecting chord engines...", 55.0)
        features = librosa_future.result()
        madmom_segments = madmom_future.result()
        essentia_frames, key = essentia_future.result()

    librosa_labels = (
        engines.librosa_beat_chords(features, intervals) if features is not None else None
    )
    essentia_labels = (
        engines.map_essentia_to_beats(
            essentia_frames, engines.ESSENTIA_FRAME_SECONDS, intervals
        )
        if essentia_frames
        else None
    )
    madmom_labels = (
        engines.map_madmom_to_beats(madmom_segments, intervals)
        if madmom_segments is not None
        else None
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
    if not available:
        raise RuntimeError("Every chord engine failed; see the log for details")
    context.report(f"Fusing {', '.join(available)}...", 82.0)

    cleaned, after = _fuse_and_clean(
        available, grid.bar_groups(), key, config, cleanup_config
    )
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
            audio_fingerprint=audio_fingerprint(project.audio_path),
            engine_version=ENGINE_VERSION,
            bars=bars,
        )
    )

    context.report(
        f"{len(bars)} bars, key {key or 'unknown'}, {after.summary()}.", 100.0
    )
