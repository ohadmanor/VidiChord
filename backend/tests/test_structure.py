"""Verse/chorus structure recovery.

The user requirement these cover: the song's structure must survive the
pipeline. Structure comes from three places - explicit headers, repeated
stanzas, or repeated lines - and each needs to work on Hebrew as well as
English.
"""

from vidichord.lyrics import structure
from vidichord.models import SectionKind


def names(sections):
    return [section.name for section in sections]


def kinds(sections):
    return [section.kind for section in sections]


class TestExplicitHeaders:
    def test_parses_genius_style_headers(self):
        lines, sections = structure.analyse(
            "[Verse 1]\nfirst line\nsecond line\n\n[Chorus]\nchorus line\n"
        )
        assert lines == ["first line", "second line", "chorus line"]
        assert names(sections) == ["Verse 1", "Chorus"]
        assert sections[0].line_start == 0 and sections[0].line_end == 1
        assert sections[1].line_start == 2 and sections[1].line_end == 2

    def test_strips_the_artist_annotation_genius_adds(self):
        _lines, sections = structure.analyse(
            "[Verse 1: Some Artist & Another]\nline one\nline two\n"
        )
        assert names(sections) == ["Verse 1"]

    def test_lrc_timestamps_are_not_mistaken_for_headers(self):
        lines, _sections = structure.analyse("[00:12.34]\nsung words\n")
        # A timestamp line is not a section header, so it stays a lyric line.
        assert "[00:12.34]" in lines

    def test_classifies_header_names(self):
        assert structure.classify("Chorus 2") is SectionKind.CHORUS
        assert structure.classify("Pre-Chorus") is SectionKind.PRE_CHORUS
        assert structure.classify("Guitar Solo") is SectionKind.SOLO
        assert structure.classify("Outro") is SectionKind.OUTRO
        assert structure.classify("Wibble") is SectionKind.OTHER


class TestStanzaInference:
    def test_finds_chorus_from_repeated_stanzas(self):
        text = (
            "verse one line a\nverse one line b\n\n"
            "we are the champions\nwe are the champions my friend\n\n"
            "verse two line a\nverse two line b\n\n"
            "we are the champions\nwe are the champions my friend\n"
        )
        _lines, sections = structure.analyse(text)
        assert names(sections) == ["Verse 1", "Chorus", "Verse 2", "Chorus"]

    def test_structurally_similar_verses_stay_separate(self):
        """Two verses sharing a skeleton must not collapse into one cluster.

        Comparing whole stanzas as characters scores these at ~0.9 similarity;
        comparing them word by word keeps them apart.
        """
        text = (
            "line a one\nline a two\n\n"
            "we are the champions\nwe are the champions my friend\n\n"
            "line b one\nline b two\n\n"
            "we are the champions\nwe are the champions my friend\n"
        )
        _lines, sections = structure.analyse(text)
        assert names(sections) == ["Verse 1", "Chorus", "Verse 2", "Chorus"]

    def test_identifies_a_bridge_after_the_second_chorus(self):
        text = (
            "verse one here\nsecond line here\n\n"
            "chorus words repeated\nchorus second line\n\n"
            "verse two here\nanother second line\n\n"
            "chorus words repeated\nchorus second line\n\n"
            "a different idea entirely\nnot heard before now\n"
        )
        _lines, sections = structure.analyse(text)
        assert names(sections)[-1] == "Bridge"
        assert kinds(sections)[-1] is SectionKind.BRIDGE


class TestHebrew:
    def test_final_letter_spelling_does_not_split_a_chorus(self):
        """The same chorus written with a final nun and a medial one.

        Hebrew is spelled inconsistently across lyric sources; normalising
        final forms is what keeps these clustered together.
        """
        text = (
            "שיר ראשון כאן\nשורה שניה\n\n"
            "הפזמון שלנו\nעוד שורה\n\n"
            "שיר שני כאן\nשורה אחרת\n\n"
            "הפזמונ שלנו\nעוד שורה\n"
        )
        _lines, sections = structure.analyse(text)
        assert names(sections) == ["Verse 1", "Chorus", "Verse 2", "Chorus"]

    def test_ktiv_haser_and_male_compare_equal(self):
        text = (
            "פתיחה של השיר\nשורה נוספת\n\n"
            "שלום עולם גדול\nעוד משפט\n\n"
            "בית שני מתחיל\nשורה אחרונה\n\n"
            "שלם עלם גדול\nעוד משפט\n"
        )
        _lines, sections = structure.analyse(text)
        assert sections[1].kind is SectionKind.CHORUS
        assert sections[3].kind is SectionKind.CHORUS


class TestRepetitionFallback:
    def test_recovers_chorus_when_the_source_has_no_blank_lines(self):
        """LRClib often returns lyrics as one undivided block.

        Stanza clustering has nothing to work with there, so the chorus has to
        be found from the longest repeated run of lines instead.
        """
        lines = [
            "maybe i didnt treat you",
            "quite as good as i should",
            "you were always on my mind",
            "you were always on my mind",
            "maybe i didnt hold you",
            "all those lonely times",
            "you were always on my mind",
            "you were always on my mind",
        ]
        sections = structure.infer_sections_by_repetition(lines)
        assert names(sections) == ["Verse 1", "Chorus", "Verse 2", "Chorus"]
        assert sections[1].line_start == 2 and sections[1].line_end == 3

    def test_analyse_falls_back_for_one_huge_stanza(self):
        text = "\n".join(
            [
                "opening line here",
                "second opening line",
                "the chorus line one",
                "the chorus line two",
                "another verse line",
                "and one more of them",
                "the chorus line one",
                "the chorus line two",
            ]
        )
        _lines, sections = structure.analyse(text)
        assert any(s.kind is SectionKind.CHORUS for s in sections)

    def test_a_single_repeated_line_is_not_a_section(self):
        # One repeated line is a refrain inside a verse, not a chorus.
        lines = ["one", "hello", "two", "hello", "three"]
        assert structure.infer_sections_by_repetition(lines) == []


class TestRendering:
    def test_round_trips_through_tagged_text(self):
        original = "[Verse 1]\nfirst\nsecond\n\n[Chorus]\nhook\n"
        lines, sections = structure.analyse(original)
        rendered = structure.render(lines, sections)
        again_lines, again_sections = structure.analyse(rendered)
        assert again_lines == lines
        assert names(again_sections) == names(sections)


def test_empty_input_is_handled():
    assert structure.analyse("") == ([], [])
    assert structure.analyse("   \n\n  ") == ([], [])
