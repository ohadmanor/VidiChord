"""Stage 5 - separate the mix into stems.

Runs between stages 1 and 2, whatever its number says. Demucs splits the
recording into vocals, drums, bass and everything else, which buys two things:
the player gets four faders, and stage 2 transcribes an isolated vocal instead
of a whole band - and Whisper's timings, the only part of the transcript that
survives into the sheet, are much better for it.

This stage never fails a run. Demucs is optional, it is heavy, and it is not
in the release executable at all, so "there are no stems" has to be an
ordinary outcome rather than an error: the reason is written onto
``05_stems.json`` and everything downstream carries on from the full mix.

Its progress messages are matched, to be reworded for the app, in
frontend/src/app/components/run-progress/run-progress.model.ts - keep the
two in step.
"""

from __future__ import annotations

import shutil
import sys
import traceback

from .. import stems as stems_mod
from ..models import StemFile, StemsDoc
from ..project import audio_fingerprint
from . import StageContext


def _reusable(document: StemsDoc | None, fingerprint: str, model: str, project) -> bool:
    """Whether a stored separation still describes the audio on disk.

    Separation costs minutes, so a re-run of the pipeline must not repeat it
    for nothing - but a re-imported local file, or a different model, means
    the stems on disk are of something else.
    """
    if document is None or not document.separated:
        return False
    if document.audio_fingerprint != fingerprint or document.model != model:
        return False
    return all((project.root / stem.filename).is_file() for stem in document.stems)


def _skip(context: StageContext, fingerprint: str, reason: str) -> None:
    """Finish the stage having separated nothing, and say why.

    Recorded rather than merely reported, so the app can tell "not tried yet"
    from "cannot" - and so the player can explain itself instead of quietly
    offering no mixer.
    """
    context.project.write(StemsDoc(unavailable=reason, audio_fingerprint=fingerprint))
    context.skip(reason)
    print(f"Stems: {reason}", file=sys.stderr)


def run(context: StageContext) -> None:
    project = context.project
    if not project.audio_path.is_file():
        raise RuntimeError("Stage 1 must run before the audio can be separated")

    fingerprint = audio_fingerprint(project.audio_path)
    model = stems_mod.model_name(context.settings)

    existing = project.read_optional(StemsDoc)
    if not context.param("force", False) and _reusable(existing, fingerprint, model, project):
        context.report(f"Stems already separated with {existing.model}.", 100.0)
        return

    # Availability is decided here, on every run, and never read back off the
    # stored document: installing demucs has to be enough to make the next run
    # separate, without anyone having to know to force it.
    reason = stems_mod.unavailable_reason(context.settings)
    if reason:
        _skip(context, fingerprint, reason)
        return

    context.report(f"Separating the mix with {model}...", 0.0)
    try:
        result = stems_mod.separate(
            project.audio_path,
            project.stems_dir,
            settings=context.settings,
            report=context.report,
        )
    except Exception as error:  # noqa: BLE001 - see the module docstring
        # A missing model download, an out-of-memory kill, a torch that will
        # not load: none of them is a reason to lose the transcription and the
        # chords that would otherwise have followed.
        traceback.print_exc()
        shutil.rmtree(project.stems_dir, ignore_errors=True)
        _skip(context, fingerprint, f"Stem separation failed: {error}")
        return

    project.write(
        StemsDoc(
            model=result.model,
            device=result.device,
            format=result.format,
            duration=result.duration,
            vocals_rms_db=result.vocals_rms_db,
            audio_fingerprint=fingerprint,
            stems=[
                StemFile(
                    name=name,
                    filename=path.relative_to(project.root).as_posix(),
                    bytes=path.stat().st_size,
                )
                for name, path in result.files.items()
            ],
        )
    )

    megabytes = sum(path.stat().st_size for path in result.files.values()) / (1 << 20)
    context.report(
        f"{len(result.files)} stems from {result.model} on {result.device} "
        f"({result.format}, {megabytes:.0f} MB).",
        100.0,
    )
