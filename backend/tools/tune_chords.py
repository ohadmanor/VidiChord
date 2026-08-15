"""Fit FusionConfig/CleanupConfig against hand-verified reference sheets.

This is what produced the current defaults in :mod:`vidichord.chords.fusion`.

Why it is fast: the engines are deterministic and every beat's per-engine
prediction is stored on ``03_chords.json``, so a trial re-fuses stored
predictions rather than re-running the engines - milliseconds per song instead
of minutes. Only fusion and cleanup are tunable this way; the beat grid, the
detected key and the engines' own constants are baked into the stored labels.

Two things keep the score honest:

* Per-song time offset and tempo scale are fitted ONCE against the stored
  chords and then frozen. Re-fitting per trial would let the optimiser raise
  its score by sliding the reference around instead of predicting better.
* Songs whose reference cannot be aligned at all are dropped rather than
  scored, so no config is rewarded for fitting an artefact.

The objective is duration-weighted agreement at majmin level, which is blind to
flicker - a config that changes chord every beat can score well and still be
unplayable. ``--report`` prints the app's own noise metrics alongside, so that
cost is visible before anything is adopted.

Usage::

    python -m tools.chordify_reference <pdf-dir> -o reference.json
    python -m tools.tune_chords reference.json --trials 500 --report
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vidichord.chords import cleanup as cleanup_mod            # noqa: E402
from vidichord.chords.fusion import FusionConfig, decode       # noqa: E402
from vidichord.chords.vocabulary import split_chord, triad_of  # noqa: E402
from vidichord.config import Settings                          # noqa: E402

#: Reference/estimate tempo ratios: a fine sweep around 1.0 for ordinary drift,
#: plus the metrical-level confusions (half/double/triple time) two independent
#: beat trackers fall into on a song in 6/8.
SCALES = np.array(sorted(set(
    [round(x, 3) for x in np.arange(0.88, 1.121, 0.01)]
    + [0.5, 2 / 3, 0.75, 4 / 3, 1.5, 2.0])))
OFFSETS = np.arange(-12.0, 12.001, 0.10)
#: Seconds per comparison sample.
GRID = 0.05
#: Below this alignment agreement a song is considered unalignable.
MIN_FIT = 0.35

_CODES: dict[str, int] = {}


def code(label: str, level: str) -> int:
    """Integer code for a label reduced to the given vocabulary."""
    reduced = triad_of(label) if level == "majmin" else split_chord(label)[0]
    key = f"{level}:{reduced}"
    if key not in _CODES:
        _CODES[key] = len(_CODES)
    return _CODES[key]


class Song:
    """A song's stored engine predictions plus its time-aligned reference."""

    def __init__(self, name: str, doc: dict, reference: list[str], duration: float):
        self.name = name
        self.key = doc.get("key", "")
        beats = [b for bar in doc["bars"] for b in bar["beats"]]
        self.starts = np.array([b["start"] for b in beats])
        self.bar_groups = [[b["index"] for b in bar["beats"]] for bar in doc["bars"]]
        engines = sorted({k for b in beats for k in (b.get("sources") or {})})
        self.sources = {e: [b["sources"][e] for b in beats] for e in engines}
        self.stored = [b["chord"] for b in beats]

        self.duration = duration
        self.ref_beats = reference
        self.times = np.arange(0, duration, GRID)
        self.beat_at = np.clip(
            np.searchsorted(self.starts, self.times, side="right") - 1,
            0, len(self.stored) - 1)

        self.offset, self.scale, self.fit = self._fit_alignment()
        self.ref_idx, self.valid = self._index(self.offset, self.scale)
        self._cache: dict[str, np.ndarray] = {}

    def _index(self, offset: float, scale: float):
        n = len(self.ref_beats)
        idx = ((self.times - offset) / (self.duration * scale) * n).astype(int)
        return idx, (idx >= 0) & (idx < n)

    def _fit_alignment(self):
        estimate = np.array([code(c, "majmin") for c in self.stored])[self.beat_at]
        reference = np.array([code(c, "majmin") for c in self.ref_beats])
        n = len(reference)

        best = (0.0, 1.0, -1.0)
        for scale in SCALES:
            span = self.duration * scale
            for offset in OFFSETS:
                idx = ((self.times - offset) / span * n).astype(int)
                valid = (idx >= 0) & (idx < n)
                if valid.sum() < len(self.times) * 0.5:
                    continue
                hit = float(np.mean(reference[idx[valid]] == estimate[valid]))
                if hit > best[2]:
                    best = (round(float(offset), 2), round(float(scale), 3), hit)
        return best

    def reference(self, level: str) -> np.ndarray:
        if level not in self._cache:
            codes = np.array([code(c, level) for c in self.ref_beats])
            self._cache[level] = codes[self.ref_idx[self.valid]]
        return self._cache[level]

    def score(self, chords: list[str], level: str = "majmin") -> float:
        estimate = np.array([code(c, level) for c in chords])[self.beat_at][self.valid]
        return float(np.mean(estimate == self.reference(level)))

    def chords_for(self, fusion, cleanup_config) -> list[str]:
        fused = decode(self.sources.get("librosa"), self.sources.get("essentia"),
                       self.sources.get("madmom"), fusion, key=self.key)
        return cleanup_mod.clean(fused, self.bar_groups, cleanup_config)

    def evaluate(self, fusion, cleanup_config, level: str = "majmin") -> float:
        return self.score(self.chords_for(fusion, cleanup_config), level)


def load_songs(reference_path: Path, library: Path | None = None) -> list[Song]:
    reference = json.loads(reference_path.read_text(encoding="utf-8"))
    library = library or Settings.load().library_dir

    by_video = {}
    for folder in Path(library).iterdir():
        source = folder / "01_source.json"
        if source.is_file():
            try:
                data = json.loads(source.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if data.get("video_id"):
                by_video[data["video_id"]] = (folder, float(data.get("duration") or 0))

    songs = []
    for name, entry in sorted(reference.items()):
        folder, duration = by_video.get(entry.get("video_id"), (None, 0))
        chords = folder / "03_chords.json" if folder else None
        if not chords or not chords.is_file() or duration <= 0:
            print(f"  skipped (not in library): {name[:60]}")
            continue
        doc = json.loads(chords.read_text(encoding="utf-8"))
        if doc.get("bars"):
            songs.append(Song(name, doc, entry["beats"], duration))
    return songs


def suggest(trial):
    fusion = FusionConfig()
    e = fusion.emission_weights
    t = fusion.transition_probabilities
    k = fusion.key_prior
    e.librosa_match = trial.suggest_float("librosa_match", 0.05, 0.99)
    e.essentia_match = trial.suggest_float("essentia_match", 0.05, 0.99)
    e.madmom_match = trial.suggest_float("madmom_match", 0.05, 0.99)
    e.none_state_bias = trial.suggest_float("none_state_bias", 0.05, 0.99)
    t.self_transition = trial.suggest_float("self_transition", 0.30, 0.999)
    t.same_root_diff_quality = trial.suggest_float("same_root_diff_quality",
                                                   1e-3, 0.5, log=True)
    t.circle_of_fifths_dist_1 = trial.suggest_float("cof1", 1e-3, 0.95, log=True)
    t.circle_of_fifths_dist_2 = trial.suggest_float("cof2", 1e-3, 0.95, log=True)
    t.unrelated_chord = trial.suggest_float("unrelated", 1e-5, 0.2, log=True)
    k.enabled = trial.suggest_categorical("key_prior_enabled", [True, False])
    k.diatonic = trial.suggest_float("diatonic", 0.5, 1.0)
    k.same_root = trial.suggest_float("same_root", 0.05, 1.0)
    k.foreign = trial.suggest_float("foreign", 0.01, 1.0)

    cleanup_config = cleanup_mod.CleanupConfig(
        min_chord_beats=trial.suggest_int("min_chord_beats", 0, 8),
        bar_snap_threshold=trial.suggest_float("bar_snap_threshold", 0.3, 1.0),
        fill_isolated_silence=trial.suggest_categorical("fill_silence", [True, False]),
    )
    return fusion, cleanup_config


def report(songs, fusion, cleanup_config, label: str) -> None:
    """Agreement plus the readability cost the objective cannot see."""
    agreement, changes, short = [], [], []
    for song in songs:
        chords = song.chords_for(fusion, cleanup_config)
        agreement.append(song.score(chords))
        metrics = cleanup_mod.measure(chords, bars=len(song.bar_groups))
        changes.append(metrics.changes_per_bar)
        short.append(metrics.short_run_fraction)
    print(f"{label:28} {np.mean(agreement):8.1%} "
          f"{np.mean(changes):11.2f} {np.mean(short):11.1%}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path, help="reference.json")
    parser.add_argument("--trials", type=int, default=500)
    parser.add_argument("--library", type=Path, default=None)
    parser.add_argument("--report", action="store_true",
                        help="also print noise metrics for default vs tuned")
    args = parser.parse_args()

    import optuna

    songs = load_songs(args.reference, args.library)
    base_fusion, base_cleanup = FusionConfig(), cleanup_mod.CleanupConfig()

    print(f"\n{'song':44} {'off':>6} {'scale':>6} {'majmin':>8}")
    print("-" * 68)
    for song in songs:
        print(f"{song.name[:44]:44} {song.offset:+6.2f} {song.scale:6.2f} "
              f"{song.evaluate(base_fusion, base_cleanup):8.1%}")

    usable = [s for s in songs if s.fit >= MIN_FIT]
    for song in songs:
        if song.fit < MIN_FIT:
            print(f"\nexcluded, reference would not align: {song.name}")

    holdout = usable[::3]
    train = [s for s in usable if s not in holdout]
    print(f"\ntrain {len(train)} / holdout {len(holdout)}, {args.trials} trials")

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    study = optuna.create_study(direction="maximize",
                                sampler=optuna.samplers.TPESampler(seed=7))
    study.optimize(lambda t: float(np.mean([s.evaluate(*suggest(t)) for s in train])),
                   n_trials=args.trials)

    fusion, cleanup_config = suggest(study.best_trial)
    print()
    for label, group in (("train", train), ("holdout", holdout), ("all", usable)):
        if not group:
            continue
        before = np.mean([s.evaluate(base_fusion, base_cleanup) for s in group])
        after = np.mean([s.evaluate(fusion, cleanup_config) for s in group])
        print(f"{label:>8}: {before:.1%} -> {after:.1%} "
              f"({(after - before) * 100:+.1f} pts, n={len(group)})")

    if args.report:
        print(f"\n{'config':28} {'majmin':>8} {'changes/bar':>11} {'short runs':>11}")
        print("-" * 62)
        report(usable, base_fusion, base_cleanup, "default")
        report(usable, fusion, cleanup_config, "tuned (as found)")
        report(usable, fusion, base_cleanup, "tuned + default cleanup")

    print("\nbest params:")
    for name, value in sorted(study.best_params.items()):
        print(f"  {name:24} {value}")


if __name__ == "__main__":
    main()
