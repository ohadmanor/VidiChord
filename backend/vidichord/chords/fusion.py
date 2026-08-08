"""Fuse three engines' chord predictions with a hidden Markov model.

Each beat has three noisy observations - one per engine - and one hidden truth.
Emission probabilities say how likely each engine's label is given a true
chord; transition probabilities encode that chords persist and, when they do
change, tend to move by small steps around the circle of fifths. Viterbi then
decodes the most likely chord sequence over the whole song at once, which is
what lets a confident engine override two mistaken ones without producing a
progression that lurches between unrelated keys.

Three things differ from the original implementation:

* The detected key conditions the emissions. Essentia already computes the key
  and it used to be discarded; weighting diatonic chords above foreign ones is
  the single largest reduction in chord noise.
* The madmom weight from the config is actually used. It was previously
  hardcoded, so the "different weights per model" setting only moved two of the
  three engines.
* Roots are compared as roots. The old code compared first characters, so
  ``C#m`` counted as a near-miss for ``Cm``.
"""

from __future__ import annotations

import numpy as np
from pydantic import BaseModel, Field

from ..models import NO_CHORD
from .vocabulary import (
    QUALITIES,
    all_states,
    diatonic_chords,
    scale_roots,
    split_chord,
    triad_of,
)

#: Circle-of-fifths position for each root, used as a transition distance.
_CIRCLE = {
    "C": 0, "G": 1, "D": 2, "A": 3, "E": 4, "B": 5,
    "F#": 6, "C#": 7, "Ab": 8, "Eb": 9, "Bb": 10, "F": 11,
}

#: Chord qualities sharing a root, over which near-miss probability is spread.
_QUALITIES_PER_ROOT = len(QUALITIES)


class EmissionWeights(BaseModel):
    """How much to trust each engine when it agrees with a candidate chord."""

    essentia_match: float = 0.50
    librosa_match: float = 0.90
    madmom_match: float = 0.50
    #: Probability mass given to "no chord" when an engine reports silence.
    none_state_bias: float = 0.80


class TransitionWeights(BaseModel):
    """How likely the chord is to stay put or move to a related chord."""

    self_transition: float = 0.75
    same_root_diff_quality: float = 0.05
    circle_of_fifths_dist_1: float = 0.70
    circle_of_fifths_dist_2: float = 0.40
    unrelated_chord: float = 0.001


class KeyPrior(BaseModel):
    """Weighting applied from the detected key.

    Set ``enabled`` to False to decode without any key bias.
    """

    enabled: bool = True
    #: Chord belongs to the key.
    diatonic: float = 1.0
    #: Root is in the scale but the quality is not diatonic (borrowed chords,
    #: secondary dominants) - plausible, just less common.
    same_root: float = 0.45
    #: Root is outside the scale entirely.
    foreign: float = 0.12


class FusionConfig(BaseModel):
    emission_weights: EmissionWeights = Field(default_factory=EmissionWeights)
    transition_probabilities: TransitionWeights = Field(default_factory=TransitionWeights)
    key_prior: KeyPrior = Field(default_factory=KeyPrior)


def _circle_distance(a: str, b: str) -> int:
    if a not in _CIRCLE or b not in _CIRCLE:
        return 6
    raw = abs(_CIRCLE[a] - _CIRCLE[b])
    return min(raw, 12 - raw)


def build_transition_matrix(states: list[str], config: FusionConfig) -> np.ndarray:
    """Row-stochastic transition matrix over the chord states."""
    weights = config.transition_probabilities
    size = len(states)
    matrix = np.empty((size, size))

    parsed = [split_chord(state)[:2] for state in states]

    for i, state_i in enumerate(states):
        root_i, _quality_i = parsed[i]
        for j, state_j in enumerate(states):
            if state_i == NO_CHORD or state_j == NO_CHORD:
                matrix[i, j] = (
                    weights.self_transition
                    if state_i == state_j
                    else (1.0 - weights.self_transition) / (size - 1)
                )
                continue

            root_j, _quality_j = parsed[j]
            if state_i == state_j:
                matrix[i, j] = weights.self_transition
            elif root_i == root_j:
                matrix[i, j] = weights.same_root_diff_quality
            else:
                distance = _circle_distance(root_i, root_j)
                if distance == 1:
                    matrix[i, j] = weights.circle_of_fifths_dist_1
                elif distance == 2:
                    matrix[i, j] = weights.circle_of_fifths_dist_2
                else:
                    matrix[i, j] = weights.unrelated_chord

    matrix /= matrix.sum(axis=1, keepdims=True)
    return matrix


class _EmissionModel:
    """Builds per-beat emission vectors over all chord states.

    Each engine contributes an independent factor. Because the factor for a
    given observation is the same at every beat, the vectors are cached by
    observation rather than recomputed T times.
    """

    def __init__(self, states: list[str], config: FusionConfig, key: str = "") -> None:
        self.states = states
        self.config = config
        self.size = len(states)

        self._roots = [split_chord(state)[0] for state in states]
        self._triads = [triad_of(state) for state in states]

        self._librosa_cache: dict[str, np.ndarray] = {}
        self._essentia_cache: dict[str, np.ndarray] = {}
        self._madmom_cache: dict[str, np.ndarray] = {}

        self.key_weights = self._build_key_weights(key)

    def _build_key_weights(self, key: str) -> np.ndarray:
        prior = self.config.key_prior
        weights = np.ones(self.size)
        if not prior.enabled or not key:
            return weights

        diatonic = diatonic_chords(key)
        in_scale = scale_roots(key)
        if not diatonic:
            return weights

        for index, state in enumerate(self.states):
            if state == NO_CHORD:
                continue
            if self._triads[index] in diatonic:
                weights[index] = prior.diatonic
            elif self._roots[index] in in_scale:
                weights[index] = prior.same_root
            else:
                weights[index] = prior.foreign
        return weights

    def _librosa_vector(self, observed: str) -> np.ndarray:
        cached = self._librosa_cache.get(observed)
        if cached is not None:
            return cached

        weights = self.config.emission_weights
        observed_root = split_chord(observed)[0]
        base = observed.split("/")[0] if "/" in observed else observed

        vector = np.empty(self.size)
        for index, state in enumerate(self.states):
            if state == NO_CHORD:
                vector[index] = (
                    weights.none_state_bias
                    if base == NO_CHORD
                    else (1.0 - weights.none_state_bias) / (self.size - 1)
                )
            elif base == state:
                vector[index] = weights.librosa_match
            elif observed_root != NO_CHORD and observed_root == self._roots[index]:
                # Right root, wrong quality - a common and forgivable error.
                vector[index] = 0.15 / _QUALITIES_PER_ROOT
            else:
                vector[index] = 0.10 / (self.size - 2)

        self._librosa_cache[observed] = vector
        return vector

    def _triad_vector(
        self, observed: str, match_weight: str, cache: dict[str, np.ndarray]
    ) -> np.ndarray:
        cached = cache.get(observed)
        if cached is not None:
            return cached

        weights = self.config.emission_weights
        matched = getattr(weights, match_weight)
        observed_triad = triad_of(observed)
        observed_root = split_chord(observed)[0]

        vector = np.empty(self.size)
        for index in range(self.size):
            triad = self._triads[index]
            if triad == NO_CHORD:
                vector[index] = (
                    weights.none_state_bias
                    if observed_triad == NO_CHORD
                    else (1.0 - weights.none_state_bias) / (self.size - 1)
                )
            elif observed_triad == triad:
                vector[index] = matched
            elif observed_root != NO_CHORD and observed_root == self._roots[index]:
                vector[index] = 0.12
            else:
                vector[index] = 0.08 / 23

        cache[observed] = vector
        return vector

    def matrix(
        self,
        librosa_obs: list[str] | None,
        essentia_obs: list[str] | None,
        madmom_obs: list[str] | None,
        n_beats: int,
    ) -> np.ndarray:
        """Log-emission matrix of shape ``(beats, states)``.

        An engine passed as None contributed no observation - because it is not
        installed, or it failed - and is left out of the product entirely. That
        is not the same as an engine reporting silence, which is evidence for
        the no-chord state.
        """
        rows = []
        for beat in range(n_beats):
            probability = np.array(self.key_weights)
            if librosa_obs is not None:
                probability = probability * self._librosa_vector(librosa_obs[beat])
            if essentia_obs is not None:
                probability = probability * self._triad_vector(
                    essentia_obs[beat], "essentia_match", self._essentia_cache
                )
            if madmom_obs is not None:
                probability = probability * self._triad_vector(
                    madmom_obs[beat], "madmom_match", self._madmom_cache
                )
            rows.append(probability)
        return np.log(np.array(rows) + 1e-100)


def decode(
    librosa_obs: list[str] | None,
    essentia_obs: list[str] | None = None,
    madmom_obs: list[str] | None = None,
    config: FusionConfig | None = None,
    key: str = "",
) -> list[str]:
    """Decode the most likely chord sequence from the engines' observations.

    Pass None for any engine that produced nothing, so it abstains rather than
    voting for silence.
    """
    config = config or FusionConfig()
    observations = [o for o in (librosa_obs, essentia_obs, madmom_obs) if o is not None]
    if not observations:
        return []
    n_beats = len(observations[0])
    if n_beats == 0:
        return []

    states = all_states()
    size = len(states)
    none_index = states.index(NO_CHORD)

    emissions = _EmissionModel(states, config, key).matrix(
        librosa_obs, essentia_obs, madmom_obs, n_beats
    )
    log_transitions = np.log(build_transition_matrix(states, config) + 1e-100)

    # Songs are far more likely to start in silence than mid-chord.
    log_initial = np.full(size, np.log(0.15 / (size - 1)))
    log_initial[none_index] = np.log(0.85)

    scores = np.empty((n_beats, size))
    backpointers = np.zeros((n_beats, size), dtype=np.int32)
    scores[0] = log_initial + emissions[0]

    for beat in range(1, n_beats):
        # candidates[i, j] = score of arriving at j from i.
        candidates = scores[beat - 1][:, None] + log_transitions
        best = np.argmax(candidates, axis=0)
        scores[beat] = candidates[best, np.arange(size)] + emissions[beat]
        backpointers[beat] = best

    path = np.empty(n_beats, dtype=np.int32)
    path[-1] = int(np.argmax(scores[-1]))
    for beat in range(n_beats - 1, 0, -1):
        path[beat - 1] = backpointers[beat, path[beat]]

    decoded = [states[index] for index in path]
    if librosa_obs is None:
        return decoded
    return _restore_slash_chords(decoded, librosa_obs)


def _restore_slash_chords(decoded: list[str], librosa_obs: list[str]) -> list[str]:
    """Re-attach bass notes that only librosa detects.

    The decoder works over root-position chords, so an inversion librosa found
    is carried back over whenever the fused chord agrees with its base triad.
    """
    out: list[str] = []
    for chord, observed in zip(decoded, librosa_obs):
        if "/" in observed:
            base, _, bass = observed.partition("/")
            if chord == base:
                out.append(f"{chord}/{bass}")
                continue
        out.append(chord)
    return out
