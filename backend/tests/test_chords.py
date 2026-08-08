"""Chord vocabulary, key theory, fusion and noise cleanup.

The cleanup tests carry the "make the chords clean" requirement: they assert on
the measured noise metrics, not on impressions.
"""

import pytest

from vidichord.chords import cleanup, fusion, vocabulary
from vidichord.models import NO_CHORD


class TestVocabulary:
    @pytest.mark.parametrize(
        "label,expected",
        [
            ("C", ("C", "", "")),
            ("Am", ("A", "m", "")),
            ("A#m7/D#", ("Bb", "m7", "Eb")),
            ("Dbmaj7", ("C#", "maj7", "")),
            ("G/B", ("G", "", "B")),
            ("N", (NO_CHORD, "", "")),
            ("", (NO_CHORD, "", "")),
            ("-", (NO_CHORD, "", "")),
            ("nonsense", (NO_CHORD, "", "")),
        ],
    )
    def test_split_chord(self, label, expected):
        assert vocabulary.split_chord(label) == expected

    def test_normalize_folds_enharmonics(self):
        assert vocabulary.normalize_chord("A#m7/D#") == "Bbm7/Eb"
        assert vocabulary.normalize_chord("Db") == "C#"
        assert vocabulary.normalize_chord("G#7") == "Ab7"

    @pytest.mark.parametrize(
        "label,triad",
        [
            ("Cmaj7", "C"), ("C7", "C"), ("Csus4", "C"),
            ("Cm7", "Cm"), ("Cdim", "Cm"), ("Cm", "Cm"),
            ("G/B", "G"), ("N", NO_CHORD),
        ],
    )
    def test_triad_reduction(self, label, triad):
        assert vocabulary.triad_of(label) == triad

    def test_chord_tones(self):
        assert vocabulary.chord_tones("C", "") == ["C", "E", "G"]
        assert vocabulary.chord_tones("A", "m") == ["A", "C", "E"]
        assert vocabulary.chord_tones("C", "7") == ["C", "E", "G", "Bb"]
        assert vocabulary.chord_tones("H", "") == []

    @pytest.mark.parametrize(
        "label,expected",
        [
            ("C:maj", "C"), ("C:min", "Cm"), ("A#:min7", "Bbm7"),
            ("G:maj/3", "G"), ("N", NO_CHORD), ("", NO_CHORD),
        ],
    )
    def test_madmom_labels(self, label, expected):
        assert vocabulary.parse_madmom_label(label) == expected

    def test_state_space_size(self):
        # 12 roots x 9 qualities, plus the no-chord state.
        assert len(vocabulary.all_states()) == 12 * 9 + 1


class TestKey:
    def test_parses_key_strings(self):
        assert vocabulary.parse_key("G major") == ("G", "major")
        assert vocabulary.parse_key("E minor") == ("E", "minor")
        assert vocabulary.parse_key("Bb") == ("Bb", "major")
        assert vocabulary.parse_key("A# min") == ("Bb", "minor")
        assert vocabulary.parse_key("") is None
        assert vocabulary.parse_key("nonsense") is None

    def test_major_diatonic_set(self):
        assert vocabulary.diatonic_chords("C major") == {
            "C", "Dm", "Em", "F", "G", "Am", "Bdim",
        }

    def test_minor_includes_the_borrowed_dominant(self):
        """Pop and rock in a minor key almost always use a major V."""
        chords = vocabulary.diatonic_chords("A minor")
        assert {"Am", "C", "Dm", "Em", "F", "G"} <= chords
        assert "E" in chords and "E7" in chords

    def test_unknown_key_yields_nothing(self):
        assert vocabulary.diatonic_chords("") == set()


class TestFusion:
    def test_majority_overrules_a_single_wrong_engine(self):
        agree = ["C"] * 4 + ["G"] * 4
        wrong = ["C"] * 4 + ["Em"] * 4
        result = fusion.decode(agree, agree, wrong, key="C major")
        assert result == ["C"] * 4 + ["G"] * 4

    def test_a_missing_engine_abstains_rather_than_voting_for_silence(self):
        """None means "produced nothing", which must not favour no-chord.

        Passing all-N for an engine that failed to run used to drag the whole
        decode towards silence.
        """
        observed = ["C"] * 8
        abstained = fusion.decode(observed, observed, None, key="C major")
        voted_silent = fusion.decode(observed, observed, [NO_CHORD] * 8, key="C major")
        assert abstained == ["C"] * 8
        assert abstained.count(NO_CHORD) <= voted_silent.count(NO_CHORD)

    def test_key_prior_pulls_towards_diatonic_chords(self):
        # Two engines disagree; the key should break the tie towards F.
        librosa = ["F"] * 8
        essentia = ["F#"] * 8
        in_key = fusion.decode(librosa, essentia, None, key="C major")
        assert set(in_key) == {"F"}

    def test_key_prior_can_be_disabled(self):
        config = fusion.FusionConfig()
        config.key_prior.enabled = False
        result = fusion.decode(["C"] * 4, ["C"] * 4, None, config, key="F# major")
        assert result == ["C"] * 4

    def test_slash_chords_survive_decoding(self):
        librosa = ["G/B"] * 8
        result = fusion.decode(librosa, ["G"] * 8, None, key="G major")
        assert result == ["G/B"] * 8

    def test_empty_input(self):
        assert fusion.decode([], [], []) == []
        assert fusion.decode(None, None, None) == []

    def test_transition_matrix_rows_are_probabilities(self):
        states = vocabulary.all_states()
        matrix = fusion.build_transition_matrix(states, fusion.FusionConfig())
        assert matrix.shape == (len(states), len(states))
        assert all(abs(row.sum() - 1.0) < 1e-9 for row in matrix)

    def test_configured_madmom_weight_is_honoured(self):
        """The madmom weight used to be hardcoded, so this setting did nothing."""
        librosa = ["C"] * 6
        madmom = ["Am"] * 6

        trusting = fusion.FusionConfig()
        trusting.emission_weights.madmom_match = 0.99
        trusting.emission_weights.librosa_match = 0.10

        ignoring = fusion.FusionConfig()
        ignoring.emission_weights.madmom_match = 0.01
        ignoring.emission_weights.librosa_match = 0.99

        assert fusion.decode(librosa, None, madmom, trusting) != fusion.decode(
            librosa, None, madmom, ignoring
        )


class TestCleanup:
    def test_absorbs_a_one_beat_flicker(self):
        noisy = ["C", "C", "G", "C", "C", "C", "C", "C"]
        assert cleanup.enforce_minimum_duration(noisy, 2) == ["C"] * 8

    def test_keeps_genuinely_long_chords(self):
        stable = ["C", "C", "C", "C", "G", "G", "G", "G"]
        assert cleanup.enforce_minimum_duration(stable, 2) == stable

    def test_silence_runs_are_left_alone(self):
        # A gap is information, not noise.
        with_gap = ["C", "C", NO_CHORD, "G", "G", "G", "G", "G"]
        result = cleanup.enforce_minimum_duration(with_gap, 2)
        assert NO_CHORD in result

    def test_bridges_a_single_silent_beat(self):
        assert cleanup.fill_isolated_silence(["C", NO_CHORD, "C"]) == ["C", "C", "C"]
        # Not between different chords - that is a real gap.
        assert cleanup.fill_isolated_silence(["C", NO_CHORD, "G"]) == ["C", NO_CHORD, "G"]

    def test_bar_snap_gives_a_bar_to_its_dominant_chord(self):
        labels = ["C", "C", "C", "G"]
        assert cleanup.snap_bars(labels, [[0, 1, 2, 3]], 0.75) == ["C"] * 4

    def test_bar_snap_leaves_a_genuine_split_bar(self):
        labels = ["C", "C", "G", "G"]
        assert cleanup.snap_bars(labels, [[0, 1, 2, 3]], 0.75) == labels

    def test_disabled_bar_snap(self):
        labels = ["C", "C", "C", "G"]
        assert cleanup.snap_bars(labels, [[0, 1, 2, 3]], 1.0) == labels

    def test_full_chain_reduces_measured_noise(self):
        noisy = ["C", "C", "G", "C", "C", "C", "Am", "Am",
                 "F", "Am", "Am", "Am", "G", "G", "G", "G"]
        bars = [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10, 11], [12, 13, 14, 15]]

        before = cleanup.measure(noisy, bars=4)
        cleaned = cleanup.clean(noisy, bars)
        after = cleanup.measure(cleaned, bars=4)

        assert after.changes_per_bar < before.changes_per_bar
        assert after.short_run_fraction == 0.0
        assert after.distinct_chords <= before.distinct_chords
        assert len(cleaned) == len(noisy)

    def test_metrics_on_empty_input(self):
        metrics = cleanup.measure([])
        assert metrics.total_beats == 0
        assert metrics.changes_per_bar == 0.0
