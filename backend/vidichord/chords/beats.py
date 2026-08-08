"""Beat and downbeat tracking - the metrical grid every chord hangs off.

madmom's *downbeat* tracker is used rather than its plain beat tracker, because
it reports each beat's position within the bar. That single change supplies the
BPM, the time signature and the bar boundaries from one model, and it removes
the two hacks that used to stand in for it: an onset-energy guess at the
downbeat offset, and a frontend routine that rotated the entire chord array to
force the first chord onto a bar line.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from statistics import median

import numpy as np

#: Time signatures the tracker is allowed to consider.
SUPPORTED_METERS = (3, 4)

#: madmom operates on a 100 fps activation function.
_FPS = 100


@dataclass
class BeatGrid:
    """Beat onsets with their position in the bar."""

    #: Beat onset times in seconds, ascending.
    times: list[float]
    #: 1-based position within the bar; 1 is the downbeat.
    beat_in_bar: list[int]
    bpm: float
    time_signature: int
    duration: float
    #: False when the grid came from a fallback rather than downbeat tracking.
    tracked: bool = True

    def __len__(self) -> int:
        return len(self.times)

    def intervals(self) -> list[tuple[float, float]]:
        """One ``(start, end)`` span per beat; the last runs to end of audio."""
        ends = self.times[1:] + [self.duration]
        return list(zip(self.times, ends))

    @property
    def pickup_beats(self) -> int:
        """Number of beats before the first downbeat."""
        for index, position in enumerate(self.beat_in_bar):
            if position == 1:
                return index
        return 0

    def bar_groups(self) -> list[list[int]]:
        """Beat indices grouped into bars, in order.

        A pickup - beats sounding before the first downbeat - forms its own
        leading group.
        """
        groups: list[list[int]] = []
        current: list[int] = []
        for index, position in enumerate(self.beat_in_bar):
            if position == 1 and current:
                groups.append(current)
                current = []
            current.append(index)
        if current:
            groups.append(current)
        return groups


def _median_period(times: list[float]) -> float:
    if len(times) < 2:
        return 0.5
    diffs = [b - a for a, b in zip(times, times[1:]) if b > a]
    return median(diffs) if diffs else 0.5


def _extend_to_start(times: list[float], positions: list[int], meter: int) -> None:
    """Prepend beats back towards zero so an intro is not dropped.

    The metrical grid is continued backwards rather than a single long beat
    being inserted, so bar numbering stays consistent through the intro.
    """
    if not times:
        return
    period = _median_period(times)
    if period <= 0:
        return

    while times[0] - period > 0.0:
        times.insert(0, times[0] - period)
        positions.insert(0, ((positions[0] - 2) % meter) + 1)

    if times[0] > 0.0:
        # The first beat is partial; start it at zero so bar 1 covers the audio.
        times[0] = 0.0


def _track_downbeats(signal) -> tuple[list[float], list[int], int] | None:
    """Run madmom's downbeat tracker. Returns None if it is unavailable."""
    from ._madmom_compat import madmom

    if madmom is None:
        return None

    try:
        from madmom.features.downbeats import (
            DBNDownBeatTrackingProcessor,
            RNNDownBeatProcessor,
        )

        activations = RNNDownBeatProcessor()(signal)
        processor = DBNDownBeatTrackingProcessor(
            beats_per_bar=list(SUPPORTED_METERS), fps=_FPS
        )
        result = processor(activations)
    except Exception as exc:  # pragma: no cover - model/runtime dependent
        print(f"Downbeat tracking failed: {exc}", file=sys.stderr)
        return None

    if result is None or len(result) < 4:
        return None

    times = [float(row[0]) for row in result]
    positions = [int(row[1]) for row in result]
    meter = max(positions) if positions else 4
    if meter not in SUPPORTED_METERS:
        meter = 4
    return times, positions, meter


def _track_beats_only(signal) -> list[float] | None:
    """Plain beat tracking, used when downbeat tracking fails."""
    from ._madmom_compat import madmom

    if madmom is None:
        return None

    try:
        from madmom.features.beats import DBNBeatTrackingProcessor, RNNBeatProcessor

        activations = RNNBeatProcessor()(signal)
        beats = DBNBeatTrackingProcessor(fps=_FPS)(activations)
    except Exception as exc:  # pragma: no cover - model/runtime dependent
        print(f"Beat tracking failed: {exc}", file=sys.stderr)
        return None

    return [float(b) for b in beats] if beats is not None and len(beats) >= 4 else None


def _estimate_downbeat_offset(y, sr, times: list[float], meter: int) -> int:
    """Pick the bar phase whose downbeats carry the most onset energy."""
    import librosa

    if len(times) < meter:
        return 0

    onset_strength = librosa.onset.onset_strength(y=y, sr=sr)
    frames = librosa.time_to_frames(np.asarray(times), sr=sr)

    best_offset, best_score = 0, -1.0
    for offset in range(meter):
        candidates = [f for f in frames[offset::meter] if 0 <= f < len(onset_strength)]
        if not candidates:
            continue
        score = float(np.mean(onset_strength[candidates]))
        if score > best_score:
            best_offset, best_score = offset, score
    return best_offset


def track(y_44k, y_native, sr_native: float, duration: float) -> BeatGrid:
    """Build the beat grid for a track.

    Tries madmom downbeat tracking, then madmom beat tracking with an
    onset-energy phase estimate, then librosa's beat tracker.
    """
    import librosa

    meter = 4
    positions: list[int] | None = None
    times: list[float] | None = None
    tracked = False

    signal = None
    from ._madmom_compat import madmom

    if madmom is not None:
        try:
            from madmom.audio.signal import Signal

            signal = Signal(np.asarray(y_44k, dtype=np.float32), sample_rate=44100)
        except Exception as exc:  # pragma: no cover
            print(f"Could not wrap audio for madmom: {exc}", file=sys.stderr)

    if signal is not None:
        downbeats = _track_downbeats(signal)
        if downbeats is not None:
            times, positions, meter = downbeats
            tracked = True
        else:
            beats_only = _track_beats_only(signal)
            if beats_only is not None:
                times = beats_only

    if times is None:
        tempo, frames = librosa.beat.beat_track(y=y_native, sr=sr_native)
        times = [float(t) for t in librosa.frames_to_time(frames, sr=sr_native)]

    if not times:
        # Nothing detected at all - lay down a plain 120 BPM grid.
        period = 0.5
        times = [i * period for i in range(max(1, int(duration / period)))]

    if positions is None:
        offset = _estimate_downbeat_offset(y_native, sr_native, times, meter)
        positions = [((index - offset) % meter) + 1 for index in range(len(times))]

    _extend_to_start(times, positions, meter)

    period = _median_period(times)
    bpm = 60.0 / period if period > 0 else 120.0

    return BeatGrid(
        times=times,
        beat_in_bar=positions,
        bpm=round(bpm, 2),
        time_signature=meter,
        duration=duration,
        tracked=tracked,
    )
