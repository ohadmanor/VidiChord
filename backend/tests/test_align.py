"""Aligning official lyrics onto a rough transcript.

Whisper gets the timing right and the words wrong; the published lyrics are the
reverse. These check that the aligner survives the ways a transcript actually
fails: mis-heard words, dropped words, and invented ones.
"""

from vidichord.lyrics.align import TimedWord, align, flatten_words, line_times
from vidichord.lyrics.normalize import normalize_word


def words(*pairs) -> list[TimedWord]:
    """Build transcript words from ``(text, start)`` pairs."""
    return [
        TimedWord(normalize_word(text), start, start + 0.4) for text, start in pairs
    ]


def keys(*texts) -> list[str]:
    return [normalize_word(t) for t in texts]


class TestAlign:
    def test_exact_match_maps_one_to_one(self):
        raw = words(("hello", 0.0), ("darkness", 1.0), ("friend", 2.0))
        assert align(raw, keys("hello", "darkness", "friend")) == [0, 1, 2]

    def test_tolerates_a_misheard_word(self):
        # "darkness" heard as "darknes" - close enough to still match.
        raw = words(("hello", 0.0), ("darknes", 1.0), ("friend", 2.0))
        assert align(raw, keys("hello", "darkness", "friend")) == [0, 1, 2]

    def test_handles_a_word_the_transcript_dropped(self):
        raw = words(("hello", 0.0), ("friend", 2.0))
        result = align(raw, keys("hello", "darkness", "friend"))
        assert result[0] == 0
        assert result[1] is None
        assert result[2] == 1

    def test_handles_a_word_the_transcript_invented(self):
        raw = words(("hello", 0.0), ("umm", 0.5), ("friend", 2.0))
        result = align(raw, keys("hello", "friend"))
        assert result == [0, 2]

    def test_empty_inputs(self):
        assert align([], keys("a", "b")) == [None, None]
        assert align(words(("a", 0.0)), []) == []

    def test_unrelated_text_does_not_force_matches(self):
        raw = words(("completely", 0.0), ("different", 1.0))
        result = align(raw, keys("nothing", "alike"))
        assert all(match is None for match in result)


class TestFlattenWords:
    def test_uses_word_timings_when_present(self):
        segments = [
            {
                "start": 0.0,
                "end": 2.0,
                "text": "hello there",
                "words": [
                    {"text": "hello", "start": 0.0, "end": 0.5},
                    {"text": "there", "start": 0.6, "end": 1.1},
                ],
            }
        ]
        flat = flatten_words(segments)
        assert [w.key for w in flat] == ["hello", "there"]
        assert flat[1].start == 0.6

    def test_interpolates_when_word_timings_are_missing(self):
        segments = [{"start": 0.0, "end": 3.0, "text": "one two three", "words": []}]
        flat = flatten_words(segments)
        assert [w.key for w in flat] == ["one", "two", "three"]
        # Evenly spread across the segment.
        assert flat[0].start == 0.0
        assert abs(flat[1].start - 1.0) < 1e-9
        assert abs(flat[2].start - 2.0) < 1e-9

    def test_skips_empty_segments(self):
        assert flatten_words([{"start": 0, "end": 1, "text": "   ", "words": []}]) == []


class TestLineTimes:
    def test_each_line_takes_its_first_matched_word(self):
        raw = words(
            ("hello", 0.0), ("darkness", 1.0),
            ("my", 5.0), ("old", 6.0), ("friend", 7.0),
        )
        times = line_times(raw, ["hello darkness", "my old friend"])
        assert times == [0.0, 5.0]

    def test_unmatched_lines_are_spread_not_stacked(self):
        """A run of unmatched lines must not all land on the same instant."""
        raw = words(("first", 0.0), ("last", 10.0))
        times = line_times(raw, ["first", "unknown a", "unknown b", "last"])
        assert times[0] == 0.0
        assert times[-1] == 10.0
        # The two unmatched lines sit strictly between, in order.
        assert times[0] < times[1] < times[2] < times[3]

    def test_trailing_unmatched_lines_still_advance(self):
        raw = words(("only", 0.0))
        times = line_times(raw, ["only", "after one", "after two"])
        assert times[0] < times[1] < times[2]

    def test_empty_lines_give_empty_times(self):
        assert line_times(words(("a", 0.0)), []) == []

    def test_no_transcript_still_returns_a_time_per_line(self):
        times = line_times([], ["one", "two", "three"])
        assert len(times) == 3


class TestHebrewMatching:
    def test_ktiv_variants_align(self):
        # Transcript spells it defectively, lyrics spell it fully.
        raw = words(("שלם", 0.0), ("עלם", 1.0))
        assert align(raw, keys("שלום", "עולם")) == [0, 1]

    def test_final_letters_align(self):
        raw = words(("פזמונ", 0.0),)
        assert align(raw, keys("פזמון")) == [0]

    def test_niqqud_is_ignored(self):
        raw = words(("שָׁלוֹם", 0.0),)
        assert align(raw, keys("שלום")) == [0]
