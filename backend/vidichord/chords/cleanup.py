"""Post-decoding noise removal, plus the metrics used to prove it works.

Viterbi decodes each beat independently of how *long* a chord has been
sounding, so a single mis-heard beat can still flip the chord and flip back.
Real music does not do that: chords last at least half a bar most of the time.

Two passes, both operating on the decoded beat sequence:

``enforce_minimum_duration``
    Absorb runs shorter than a threshold into whichever neighbour is stronger.

``snap_bars``
    Where one chord already dominates a bar, give it the whole bar.

The metrics at the bottom quantify the result, so "cleaner chords" can be
checked against a number rather than an impression.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from ..models import NO_CHORD
from pydantic import BaseModel


class CleanupConfig(BaseModel):
    """Tuning for the noise-removal passes."""

    #: Chord runs shorter than this many beats are absorbed. 0 disables.
    min_chord_beats: int = 2
    #: Give a bar entirely to one chord once it covers this fraction of it.
    #: 1.0 or above disables bar snapping.
    bar_snap_threshold: float = 0.75
    #: Drop isolated "no chord" beats sitting inside a sounding chord.
    fill_isolated_silence: bool = True


@dataclass
class _Run:
    label: str
    start: int
    end: int  # exclusive

    @property
    def length(self) -> int:
        return self.end - self.start


def _to_runs(labels: list[str]) -> list[_Run]:
    runs: list[_Run] = []
    for index, label in enumerate(labels):
        if runs and runs[-1].label == label:
            runs[-1].end = index + 1
        else:
            runs.append(_Run(label, index, index + 1))
    return runs


def _from_runs(runs: list[_Run], length: int) -> list[str]:
    out = [NO_CHORD] * length
    for run in runs:
        for index in range(run.start, run.end):
            out[index] = run.label
    return out


def enforce_minimum_duration(labels: list[str], min_beats: int) -> list[str]:
    """Absorb chord runs shorter than ``min_beats`` into a neighbour.

    The stronger (longer) neighbour wins; ties go to the preceding chord, since
    a chord change is more often late than early relative to the beat grid.
    Runs of silence are left alone - a genuine gap is information.
    """
    if min_beats <= 1 or len(labels) < 2:
        return list(labels)

    runs = _to_runs(labels)
    changed = True
    while changed:
        changed = False
        for position, run in enumerate(runs):
            if run.length >= min_beats or run.label == NO_CHORD:
                continue

            previous = runs[position - 1] if position > 0 else None
            following = runs[position + 1] if position + 1 < len(runs) else None
            if previous is None and following is None:
                continue

            if previous is None:
                winner = following
            elif following is None:
                winner = previous
            else:
                winner = previous if previous.length >= following.length else following

            run.label = winner.label
            changed = True
            # Re-derive runs so the absorbed span merges with its neighbour.
            runs = _to_runs(_from_runs(runs, len(labels)))
            break

    return _from_runs(runs, len(labels))


def fill_isolated_silence(labels: list[str]) -> list[str]:
    """Bridge a single silent beat sitting between two identical chords."""
    out = list(labels)
    for index in range(1, len(out) - 1):
        if (
            out[index] == NO_CHORD
            and out[index - 1] != NO_CHORD
            and out[index - 1] == out[index + 1]
        ):
            out[index] = out[index - 1]
    return out


def snap_bars(labels: list[str], bar_groups: list[list[int]], threshold: float) -> list[str]:
    """Give a bar entirely to the chord that already dominates it."""
    if threshold >= 1.0:
        return list(labels)

    out = list(labels)
    for group in bar_groups:
        chords = [out[index] for index in group if out[index] != NO_CHORD]
        if not chords:
            continue
        winner, count = Counter(chords).most_common(1)[0]
        if count / len(group) >= threshold:
            for index in group:
                out[index] = winner
    return out


def clean(
    labels: list[str],
    bar_groups: list[list[int]] | None = None,
    config: CleanupConfig | None = None,
) -> list[str]:
    """Run the full cleanup chain over a decoded chord sequence."""
    config = config or CleanupConfig()
    out = list(labels)

    if config.fill_isolated_silence:
        out = fill_isolated_silence(out)
    out = enforce_minimum_duration(out, config.min_chord_beats)
    if bar_groups:
        out = snap_bars(out, bar_groups, config.bar_snap_threshold)
        # Snapping can create new short runs at bar edges; settle them again.
        out = enforce_minimum_duration(out, config.min_chord_beats)
    return out


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


class NoiseMetrics(BaseModel):
    """How restless a chord sequence is. Lower is cleaner."""

    total_beats: int
    total_bars: int
    chord_changes: int
    changes_per_bar: float
    short_run_fraction: float
    distinct_chords: int

    def summary(self) -> str:
        return (
            f"{self.changes_per_bar:.2f} changes/bar, "
            f"{self.short_run_fraction:.1%} short runs, "
            f"{self.distinct_chords} distinct chords"
        )


def measure(labels: list[str], bars: int = 0, min_beats: int = 2) -> NoiseMetrics:
    """Quantify chord-sequence noise.

    ``changes_per_bar`` counts how often the chord changes; ``short_run_fraction``
    is the share of chord runs lasting fewer than ``min_beats`` beats, which is
    the flicker the cleanup passes target.
    """
    if not labels:
        return NoiseMetrics(
            total_beats=0, total_bars=0, chord_changes=0,
            changes_per_bar=0.0, short_run_fraction=0.0, distinct_chords=0,
        )

    runs = _to_runs(labels)
    sounding = [run for run in runs if run.label != NO_CHORD]
    short = [run for run in sounding if run.length < min_beats]
    changes = max(0, len(runs) - 1)
    bar_count = bars or max(1, len(labels) // 4)

    return NoiseMetrics(
        total_beats=len(labels),
        total_bars=bar_count,
        chord_changes=changes,
        changes_per_bar=round(changes / bar_count, 4),
        short_run_fraction=round(len(short) / len(sounding), 4) if sounding else 0.0,
        distinct_chords=len({run.label for run in sounding}),
    )
