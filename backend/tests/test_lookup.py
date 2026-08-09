"""Turning a video title into something a lyrics database will match.

A YouTube title names an upload, not a song: it carries the uploader's own
additions - "(Official Video)", "| הקליפ הרשמי", the project a recording came
from. LRClib is indexed by track and artist, so those additions are the
difference between a hit and none, which in turn is the difference between
aligned lyrics and the run stopping to ask the user for them.
"""

from vidichord.pipeline.stage2_lyrics import _title_variants


class TestTitleVariants:
    def test_a_plain_title_is_searched_as_it_is(self):
        assert _title_variants("Plain Title") == ["Plain Title"]

    def test_the_full_title_is_always_tried_first(self):
        # It is the most specific form, and right whenever the upload happens
        # to be named after the song alone.
        variants = _title_variants("Song (Official Video)")
        assert variants[0] == "Song (Official Video)"

    def test_bracketed_additions_are_offered_stripped(self):
        assert "Song" in _title_variants("Song (Official Video)")
        assert "Song" in _title_variants("Song [HD]")

    def test_an_annotation_after_a_bar_is_offered_stripped(self):
        # The real case: LRClib has this song, but not under the tail.
        variants = _title_variants("רמי קלינשטיין קרן פלס - לחיות | הקליפ הרשמי")
        assert "רמי קלינשטיין קרן פלס - לחיות" in variants

    def test_brackets_and_a_bar_together(self):
        variants = _title_variants("Some Song (Official Video) | Live [HD]")
        assert "Some Song" in variants

    def test_variants_are_unique(self):
        variants = _title_variants("Song (Official Video) | Extra")
        assert len(variants) == len(set(variants))

    def test_a_title_that_is_only_an_annotation_yields_no_empty_query(self):
        # Searching for "" would match everything; it must never be offered.
        assert all(v.strip() for v in _title_variants("(Official Video)"))

    def test_inner_whitespace_is_collapsed(self):
        assert "A B" in _title_variants("A (x) B")
