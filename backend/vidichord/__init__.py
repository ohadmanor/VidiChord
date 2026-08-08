"""VidiChord - turn a song into a chord sheet with time-synced lyrics.

The work is split into four stages, each reading the artifacts produced before
it and writing exactly one of its own (see :mod:`vidichord.project`):

    1. :mod:`vidichord.pipeline.stage1_audio`   YouTube or local file -> WAV
    2. :mod:`vidichord.pipeline.stage2_lyrics`  transcript + official lyrics
    3. :mod:`vidichord.pipeline.stage3_chords`  three engines fused onto a beat grid
    4. :mod:`vidichord.pipeline.stage4_sheet`   lyrics x chords -> song sheet
"""

__version__ = "1.0.0"
