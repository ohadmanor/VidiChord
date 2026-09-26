"""The pipeline.

Each stage reads the artifacts written before it and produces exactly one of
its own, so a stage can be re-run on its own after the user edits an earlier
result or changes a setting.

    1. audio   YouTube URL or local file -> audio.wav + 01_source.json
    5. stems   the mix split four ways -> stems/*.ogg + 05_stems.json
    2. lyrics  transcript + official lyrics -> 02_lyrics.json
    3. chords  three engines fused onto a beat grid -> 03_chords.json
    4. sheet   lyrics x chords -> 04_sheet.json + sheet.txt

Stage numbers are stable identifiers, not the order the stages run in - see
:data:`DEFAULT_ORDER`. Stems came last and took the next free number, because
renumbering would have meant renaming 02_lyrics.json, 03_chords.json and
04_sheet.json in every song folder already on disk; it runs second, where it
has to, to hand stage 2 a clean vocal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..config import Settings
from ..models import StageState
from ..project import SongProject

#: ``(message, percent)`` - percent is within the running stage, or None.
ProgressFn = Callable[[str, float | None], None]


class NeedsUserInput(Exception):
    """Raised by a stage that cannot continue without a decision.

    Stage 2 raises this when no lyrics could be found automatically: the user
    must choose between transcribing with Whisper alone or pasting lyrics.
    """

    def __init__(self, message: str, options: list[str] | None = None) -> None:
        super().__init__(message)
        self.options = options or []


@dataclass
class StageContext:
    """Everything a stage needs to do its work and report on it."""

    project: SongProject
    settings: Settings
    _report: ProgressFn | None = None
    #: Stage-specific parameters, e.g. fusion weights or a language override.
    params: dict = field(default_factory=dict)
    #: Why the running stage chose to do nothing, set by :meth:`skip`. The job
    #: manager clears it before each stage.
    skipped: str = ""

    def report(self, message: str, percent: float | None = None) -> None:
        if self._report is not None:
            self._report(message, percent)

    def skip(self, reason: str) -> None:
        """Finish the running stage having deliberately done nothing, and say why.

        The app then shows the stage as skipped, with the reason, rather than
        as done - separation that is switched off did not separate anything.
        """
        self.skipped = reason
        self.report(reason, 100.0)

    def param(self, name: str, default=None):
        return self.params.get(name, default)


#: Stage number -> human label, used in progress messages and the manifest.
STAGE_LABELS = {
    1: "Audio",
    2: "Lyrics",
    3: "Chords",
    4: "Sheet",
    5: "Stems",
}

#: The order stages actually run in. Separation sits between the audio and the
#: lyrics because stage 2 transcribes the vocals stem when there is one.
DEFAULT_ORDER = (1, 5, 2, 3, 4)

#: Where a reviewed run stops: the sheet is laid out only once the lyrics and
#: chords have been corrected. See ``server.REVIEW_FINAL_STAGE``.
REVIEW_FINAL_STAGE = 3


def stages_from(first: int, review: bool = False) -> tuple[int, ...]:
    """The stages a run starting at ``first`` should work through.

    Follows :data:`DEFAULT_ORDER` rather than counting upwards, so re-running
    separation carries on into the lyrics that depend on it, and a stage
    number nobody schedules automatically still runs on its own when asked.
    """
    order = DEFAULT_ORDER
    if review:
        order = order[: order.index(REVIEW_FINAL_STAGE) + 1]
    if first not in order:
        return (first,)
    return order[order.index(first):]


def run_stage(number: int, context: StageContext) -> None:
    """Run one stage, recording its state in the project manifest."""
    from . import stage1_audio, stage2_lyrics, stage3_chords, stage4_sheet, stage5_stems

    runners = {
        1: stage1_audio.run,
        2: stage2_lyrics.run,
        3: stage3_chords.run,
        4: stage4_sheet.run,
        5: stage5_stems.run,
    }
    if number not in runners:
        raise ValueError(f"No such stage: {number}")

    project = context.project
    project.update_stage(number, StageState.RUNNING, message=STAGE_LABELS[number])
    try:
        runners[number](context)
    except NeedsUserInput as pause:
        project.update_stage(number, StageState.NEEDS_INPUT, message=str(pause))
        raise
    except Exception as error:
        project.update_stage(number, StageState.FAILED, error=str(error))
        raise
    else:
        project.update_stage(number, StageState.DONE)


def run_pipeline(context: StageContext, stages: tuple[int, ...] = DEFAULT_ORDER) -> None:
    """Run a series of stages in order, stopping at the first failure."""
    for number in stages:
        context.report(f"Stage {number}: {STAGE_LABELS[number]}", None)
        run_stage(number, context)
