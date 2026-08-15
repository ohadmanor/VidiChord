"""Stage 2 - deciding which language is being sung.

The decision picks the transcription model, so getting it wrong costs a full
transcription by the wrong one and leaves a transcript no lyrics provider can
be searched with. On music the detector is rarely confident, which is what
these cover: how the windows' opinions are added up.
"""

from vidichord.lyrics import whisper_engine


def test_a_window_contributes_its_whole_distribution():
    """Not just the language that topped it."""
    votes = whisper_engine._window_votes("es", 0.43, [{"es": 0.43, "he": 0.2}])

    assert dict(votes) == {"es": 0.43, "he": 0.2}


def test_a_distribution_spelled_as_pairs_is_understood_too():
    """faster-whisper has returned both shapes across releases."""
    votes = whisper_engine._window_votes("es", 0.43, [[("es", 0.43), ("he", 0.2)]])

    assert dict(votes) == {"es": 0.43, "he": 0.2}


def test_without_a_distribution_the_winner_still_votes():
    """Older releases return the pick alone; it beats not voting at all."""
    assert whisper_engine._window_votes("he", 0.68, []) == [("he", 0.68)]
    assert whisper_engine._window_votes("he", 0.68, [None]) == [("he", 0.68)]


def test_a_language_present_throughout_beats_one_that_spikes_once():
    """The failure this scoring exists to prevent.

    A Hebrew recording whose windows each read as a different European
    language, with Hebrew placed second every time. Counting only winners
    sends the song to the wrong model; counting whole distributions does not.
    """
    windows = [
        ("en", 0.26, [{"en": 0.26, "he": 0.15}]),
        ("es", 0.43, [{"es": 0.43, "he": 0.14}]),
        ("ca", 0.25, [{"ca": 0.25, "he": 0.13}]),
        ("pt", 0.37, [{"pt": 0.37, "he": 0.12}]),
    ]

    scores: dict[str, float] = {}
    for language, probability, rest in windows:
        for name, weight in whisper_engine._window_votes(language, probability, rest):
            scores[name] = scores.get(name, 0.0) + weight

    assert max(scores, key=lambda name: scores[name]) == "he"


def test_the_detector_is_not_the_tiniest_model():
    """tiny cannot tell Hebrew singing from Spanish; small can."""
    assert whisper_engine.DETECTION_MODEL != "tiny"


# -- the cached transcript --------------------------------------------------
#
# A transcript is only as good as the language the detector chose for it, so
# changing the detector has to retire what the old one decided.


def _project(tmp_path):
    from vidichord.project import SongProject

    return SongProject.create(tmp_path, "Someone - A Song [abc123]")


def _context(project):
    from vidichord.config import Settings
    from vidichord.pipeline import StageContext

    return StageContext(project=project, settings=Settings(library_dir=project.root.parent))


def test_a_transcript_from_this_detector_is_reused(tmp_path):
    from vidichord.pipeline import stage2_lyrics

    context = _context(_project(tmp_path))
    stage2_lyrics._save_transcript(context, "he", [{"text": "שלום"}], vocals_detected=True)

    assert stage2_lyrics._load_transcript(context) == ("he", [{"text": "שלום"}], True)


def test_a_transcript_from_a_different_detector_is_discarded(tmp_path):
    """Upgrading must not leave the wrong-language transcript in place."""
    import json

    from vidichord.pipeline import stage2_lyrics

    project = _project(tmp_path)
    context = _context(project)
    (project.root / stage2_lyrics.TRANSCRIPT_FILENAME).write_text(
        json.dumps({"detector": "tiny", "language": "es", "segments": [{"text": "Mama"}]}),
        encoding="utf-8",
    )

    assert stage2_lyrics._load_transcript(context) is None


def test_a_transcript_predating_the_stamp_is_discarded(tmp_path):
    """No detector recorded means it came from the build that had no stamp."""
    import json

    from vidichord.pipeline import stage2_lyrics

    project = _project(tmp_path)
    context = _context(project)
    (project.root / stage2_lyrics.TRANSCRIPT_FILENAME).write_text(
        json.dumps({"language": "es", "segments": [{"text": "Mama"}]}), encoding="utf-8"
    )

    assert stage2_lyrics._load_transcript(context) is None


def test_a_missing_or_corrupt_transcript_is_not_an_error(tmp_path):
    from vidichord.pipeline import stage2_lyrics

    project = _project(tmp_path)
    context = _context(project)
    assert stage2_lyrics._load_transcript(context) is None

    (project.root / stage2_lyrics.TRANSCRIPT_FILENAME).write_text("{not json", encoding="utf-8")
    assert stage2_lyrics._load_transcript(context) is None
