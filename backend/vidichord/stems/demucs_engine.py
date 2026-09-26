"""Source separation with Demucs.

Splits a mix into vocals, drums, bass and everything else. Two things want
that: the player, which turns the four parts into faders, and stage 2, which
transcribes a clean vocal far better than it transcribes a whole band.

Demucs is part of the app: ``requirements.txt`` installs it, PyTorch with it
(about 650 MB on Windows, more than the rest of the application weighs), and
the release executable bundles both. It is still never required to *run*: an
install where it will not load - or a user who switches separation off - gets
the app as it always was, stage 5 recording why it separated nothing and every
later stage working from the full mix.

Everything here imports demucs lazily. Importing torch costs seconds and a few
hundred megabytes of address space, and a song that is not being separated
should pay neither.
"""

from __future__ import annotations

import importlib.util
import math
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from ..config import int_env
from ..models import STEM_NAMES

#: Progress reporter: ``(message, percent within this step)``.
ReportFn = Callable[[str, float | None], None]

#: The separation model. htdemucs is the hybrid transformer default: one pass,
#: four stems, about 80 MB fetched from Hugging Face on first use. htdemucs_ft
#: is four models in a bag - a little better, four times slower.
DEFAULT_MODEL = "htdemucs"

#: Opus at this rate is transparent enough to practise against and turns a
#: 50 MB WAV into roughly 5 MB per stem. Ogg is the container: browsers handle
#: its pre-skip consistently, which is what keeps four separately decoded
#: stems sample-aligned with each other and with the chord timeline. MP3 would
#: not - its encoder padding would shift the whole mix against the sheet.
_OPUS_BITRATE = "128k"

#: Encoders to try, best first. FLAC needs no external library, so it covers an
#: ffmpeg build without libopus rather than failing the stage.
_ENCODERS = (
    ("opus", ".ogg", ["-c:a", "libopus", "-b:a", _OPUS_BITRATE]),
    ("flac", ".flac", ["-c:a", "flac", "-compression_level", "5"]),
)

#: Frames per write when feeding the encoder, so a five-minute stem is not
#: copied into one 100 MB block of bytes.
_PIPE_FRAMES = 1 << 18


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def model_name(settings=None) -> str:
    """Which separation model to run: the environment, the settings, the default."""
    return (
        _env("VIDICHORD_DEMUCS_MODEL")
        or getattr(settings, "stems_model", "")
        or DEFAULT_MODEL
    )


def device_name() -> str:
    """Where to run it: whatever was asked for, else CUDA if it is there.

    Transcription defaults to CPU because ctranslate2's CUDA build is a
    separate install. torch carries its own, so when a GPU is present using it
    costs nothing and turns minutes into seconds.
    """
    requested = _env("VIDICHORD_DEMUCS_DEVICE")
    if requested:
        return requested
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def demucs_installed() -> bool:
    """Whether demucs can be imported, without paying to import it."""
    try:
        return importlib.util.find_spec("demucs") is not None
    except (ImportError, ValueError):
        return False


def unavailable_reason(settings=None) -> str:
    """Why separation cannot run, in words worth showing the user.

    Empty when it can. Worked out afresh on every run and never read back off
    a stored document: installing demucs afterwards has to be enough to make
    the next run separate.
    """
    if _env("VIDICHORD_DEMUCS", "1") == "0":
        return "Stem separation is switched off (VIDICHORD_DEMUCS=0)."
    if settings is not None and not getattr(settings, "stems_enabled", True):
        return "Stem separation is switched off in settings."
    if not demucs_installed():
        if getattr(sys, "frozen", False):
            # The release build refuses to build without it, so this is an exe
            # built some other way.
            return (
                "Stem separation needs Demucs and PyTorch, and this build of "
                "VidiChord was made without them. A build from "
                "devops\\scripts\\build_release.bat includes them."
            )
        return (
            "Demucs is not installed. It is part of requirements.txt, so "
            "devops\\scripts\\run_local.bat installs it, or: "
            "backend\\.venv\\Scripts\\pip install -r backend\\requirements.txt"
        )
    return ""


@dataclass
class Separation:
    """What one separation produced."""

    model: str
    device: str
    #: Codec actually used - "opus" normally, "flac" when libopus is missing.
    format: str
    duration: float
    vocals_rms_db: float
    #: ``name -> file written``, in :data:`STEM_NAMES` order.
    files: dict[str, Path] = field(default_factory=dict)


def _progress_callback(report: ReportFn | None):
    """Adapt the demucs callback onto the stage reporter.

    It fires per segment per model in the bag, which is the only thing that
    makes a stage measured in minutes look alive rather than hung.
    """
    if report is None:
        return None

    def callback(data: dict) -> None:
        if data.get("state") != "end":
            return
        models = max(1, int(data.get("models") or 1))
        index = int(data.get("model_idx_in_bag") or 0)
        length = float(data.get("audio_length") or 0) or 1.0
        offset = float(data.get("segment_offset") or 0)
        done = (index + min(1.0, offset / length)) / models
        report("Separating stems...", min(99.0, done * 100.0))

    return callback


def _rms_db(samples: np.ndarray) -> float:
    """Loudness of a waveform in dBFS, floored so digital silence is a number."""
    if samples.size == 0:
        return -120.0
    mean_square = float(np.mean(np.square(samples, dtype=np.float64)))
    if mean_square <= 0.0:
        return -120.0
    return round(10.0 * math.log10(mean_square), 2)


def _encode(
    samples: np.ndarray,
    destination: Path,
    sample_rate: int,
    ffmpeg: Path,
    arguments: list[str],
) -> bool:
    """Write ``(channels, frames)`` float audio through ffmpeg. True on success.

    The PCM goes down a pipe rather than into a temporary WAV: four stems of a
    five-minute song would be 170 MB of scratch files written only to be read
    straight back and deleted.
    """
    channels = int(samples.shape[0])
    interleaved = np.ascontiguousarray(samples.T, dtype=np.float32)

    command = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "f32le", "-ar", str(sample_rate), "-ac", str(channels),
        "-i", "pipe:0", *arguments, str(destination),
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        assert process.stdin is not None
        for start in range(0, len(interleaved), _PIPE_FRAMES):
            process.stdin.write(interleaved[start: start + _PIPE_FRAMES].tobytes())
        process.stdin.close()
    except (BrokenPipeError, OSError):
        # ffmpeg rejected the stream; communicate() below has the reason.
        pass

    _out, error = process.communicate()
    if process.returncode == 0 and destination.is_file() and destination.stat().st_size:
        return True

    destination.unlink(missing_ok=True)
    detail = (error or b"").decode("utf-8", "replace").strip().splitlines()
    print(
        f"Stem encoding with {arguments[1]} failed: "
        f"{detail[-1] if detail else 'no output'}",
        file=sys.stderr,
    )
    return False


def _ffmpeg_binary() -> Path:
    """The ffmpeg the app already uses, fetching it if this is the first run."""
    from ..pipeline.stage1_audio import ensure_ffmpeg

    return ensure_ffmpeg() / "ffmpeg.exe"


def separate(
    audio_path: Path,
    out_dir: Path,
    settings=None,
    report: ReportFn | None = None,
) -> Separation:
    """Split ``audio_path`` into four stems written under ``out_dir``.

    Raises whatever demucs raises. Stage 5 is what decides that a failure here
    degrades the run rather than ending it.
    """
    # torch and ctranslate2 - which transcription runs on - each ship their own
    # copy of the OpenMP runtime, and loading the second into a process that
    # already holds the first aborts the interpreter with "OMP: Error #15".
    # Both copies are the same implementation, so telling the loader to allow
    # the duplicate is the documented way through, and this process is exactly
    # the case the warning is not about: one application, two vendored copies.
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

    import demucs.api
    import torch

    name = model_name(settings)
    device = device_name()

    # Leave a core for the rest of the app; torch otherwise takes every one and
    # then fights the audio server for them.
    torch.set_num_threads(int_env("VIDICHORD_DEMUCS_THREADS", max(1, (os.cpu_count() or 4) - 1)))

    if report:
        report(f"Loading the {name} model (downloads it on first run)...", 1.0)

    arguments: dict = {
        "model": name,
        "device": device,
        "callback": _progress_callback(report),
    }
    segment = int_env("VIDICHORD_DEMUCS_SEGMENT", 0, minimum=0)
    if segment:
        # Shorter segments trade speed for peak memory; htdemucs caps at 7.8s.
        arguments["segment"] = segment
    jobs = int_env("VIDICHORD_DEMUCS_JOBS", 0, minimum=0)
    if jobs:
        arguments["jobs"] = jobs

    separator = demucs.api.Separator(**arguments)

    if report:
        report("Separating stems...", 2.0)
    _origin, parts = separator.separate_audio_file(Path(audio_path))

    sample_rate = int(getattr(separator, "samplerate", 44100))
    out_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = _ffmpeg_binary()

    files: dict[str, Path] = {}
    duration = 0.0
    vocals_db = -120.0
    chosen_format = ""

    for stem_name in STEM_NAMES:
        tensor = parts.get(stem_name)
        if tensor is None:
            continue

        samples = tensor.detach().cpu().numpy()
        if samples.ndim == 1:
            samples = samples[np.newaxis, :]
        duration = max(duration, samples.shape[1] / float(sample_rate))
        if stem_name == "vocals":
            vocals_db = _rms_db(samples)

        # Whichever encoder works for the first stem is used for all of them;
        # a mixed-format set would be a nuisance for everything downstream.
        for codec, suffix, encoder in _ENCODERS:
            if chosen_format and codec != chosen_format:
                continue
            destination = out_dir / f"{stem_name}{suffix}"
            if _encode(samples, destination, sample_rate, ffmpeg, encoder):
                chosen_format = codec
                files[stem_name] = destination
                break
        else:
            raise RuntimeError(f"Could not encode the {stem_name} stem")

        if report:
            report(f"Wrote the {stem_name} stem.", None)

    if not files:
        raise RuntimeError(f"{name} produced no stems")

    return Separation(
        model=name,
        device=device,
        format=chosen_format,
        duration=round(duration, 3),
        vocals_rms_db=vocals_db,
        files=files,
    )
