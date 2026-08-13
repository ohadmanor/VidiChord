"""faster-whisper wrapper with one-shot language detection.

The old implementation transcribed a Hebrew song twice: once end-to-end with
``large-v3``, then again with the Hebrew-tuned model once the first pass
revealed the language. Transcription is the slowest thing the pipeline does, so
here a tiny model identifies the language first and only the right large model
is ever run over the full audio.
"""

from __future__ import annotations

import os
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

#: Cheap model used purely to identify the language.
DETECTION_MODEL = "tiny"

#: Hebrew is served by a community model tuned on Hebrew speech; everything
#: else uses large-v3-turbo. Turbo keeps large-v3's encoder but has 4 decoder
#: layers instead of 32, which makes it several times faster on CPU for a
#: barely measurable accuracy cost - and the transcript is only a timing
#: reference here, so that trade is free.
HEBREW_MODEL = "ivrit-ai/whisper-large-v3-turbo-ct2"
DEFAULT_MODEL = "large-v3-turbo"

#: Seconds of audio to inspect when detecting the language.
DETECTION_WINDOW = 30.0

#: Where those windows start, in seconds. Songs open with an instrumental
#: intro often enough that reading only the first window is a coin flip: on a
#: Hebrew test track it scored Hebrew at 0.17, barely ahead of Greek at 0.16,
#: and the loser won often enough to send the whole song to the wrong model.
#: Sampling across the track finds the singing wherever it starts.
DETECTION_OFFSETS = (0.0, 30.0, 60.0, 90.0, 120.0)

#: Whisper's fixed input rate.
_SAMPLE_RATE = 16000

_DEVICE = os.environ.get("VIDICHORD_WHISPER_DEVICE", "cpu")
_COMPUTE_TYPE = os.environ.get("VIDICHORD_WHISPER_COMPUTE", "int8")

#: Overrides model selection entirely. Useful on modest hardware, and for
#: tests, where "tiny" avoids a multi-gigabyte download.
_MODEL_ENV = os.environ.get("VIDICHORD_WHISPER_MODEL") or None


from ..config import int_env

#: ctranslate2 runs on 4 CPU threads unless told otherwise, which leaves most
#: of a modern laptop idle during the slowest step of the whole pipeline.
_CPU_THREADS = int_env("VIDICHORD_WHISPER_THREADS", max(4, (os.cpu_count() or 8) - 2))

#: Greedy decoding. Whisper's default beam of 5 buys a slightly better
#: transcript for ~3x the decode time, but the words are replaced by official
#: lyrics anyway - only the timings survive, and those don't need a beam.
_BEAM_SIZE = int_env("VIDICHORD_WHISPER_BEAM", 1)

#: 30-second windows decoded per batch; 1 means sequential decoding.
#: Off by default: ctranslate2 4.8's batched encoder overflows its worker
#: threads' stack on Windows CPU builds (a hard crash, not an exception).
#: Worth enabling on CUDA, where batching is a large win and the crash was
#: not observed.
_BATCH_SIZE = int_env("VIDICHORD_WHISPER_BATCH", 1)

#: Escape hatch: VIDICHORD_WHISPER_VAD=0 transcribes everything, for the
#: rare mix whose vocals Silero misses even at the gentle threshold below.
_USE_VAD = int_env("VIDICHORD_WHISPER_VAD", 1, minimum=0) > 0

ProgressFn = Callable[[str], None]


@dataclass
class Segment:
    """One transcribed phrase."""

    start: float
    end: float
    text: str
    words: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "words": self.words,
        }


@dataclass
class Transcript:
    language: str
    segments: list[Segment]

    @property
    def text(self) -> str:
        return "\n".join(segment.text for segment in self.segments if segment.text)

    def as_dicts(self) -> list[dict[str, Any]]:
        return [segment.as_dict() for segment in self.segments]


def model_for_language(language: str | None) -> str:
    return HEBREW_MODEL if language == "he" else DEFAULT_MODEL


def _collect_segments(raw_segments, on_progress: ProgressFn | None) -> list[Segment]:
    """Drain the decoder's lazy generator - this is where the minutes go."""
    segments: list[Segment] = []
    for raw in raw_segments:
        words = [
            {"text": word.word.strip(), "start": word.start, "end": word.end}
            for word in (getattr(raw, "words", None) or [])
            if word.word.strip()
        ]
        segments.append(
            Segment(start=raw.start, end=raw.end, text=raw.text.strip(), words=words)
        )
        if on_progress and len(segments) % 10 == 0:
            minutes, seconds = divmod(int(raw.end), 60)
            on_progress(f"Transcribing audio... ({minutes}:{seconds:02d} done)")
    return segments


class WhisperEngine:
    """Loads faster-whisper models on demand and keeps the last one warm."""

    def __init__(self, model_override: str | None = None) -> None:
        #: Forces a specific model regardless of detected language.
        self.model_override = model_override or _MODEL_ENV
        self._models: dict[str, Any] = {}

    def _load(self, name: str):
        model = self._models.get(name)
        if model is not None:
            return model

        from faster_whisper import WhisperModel

        model = WhisperModel(
            name, device=_DEVICE, compute_type=_COMPUTE_TYPE, cpu_threads=_CPU_THREADS
        )
        # Keep the detector plus at most one large model resident.
        for cached in list(self._models):
            if cached != DETECTION_MODEL:
                del self._models[cached]
        self._models[name] = model
        return model

    def detect_language(self, audio_path: str) -> str:
        """Identify the sung language by sampling across the track.

        Runs on a small model over short windows, so it costs seconds rather
        than the minutes a full transcription takes.

        Each window votes with its own confidence and the votes are summed, so
        a language that scores moderately wherever there is singing beats one
        that scores highly on a single unrepresentative window. On the Hebrew
        test track that is the difference between Hebrew (0.17 + 0.68 + 0.50)
        and the Arabic its instrumental passage reads as (0.76).
        """
        model = self._load(DETECTION_MODEL)

        try:
            from faster_whisper.audio import decode_audio

            waveform = decode_audio(audio_path, sampling_rate=_SAMPLE_RATE)
            span = int(DETECTION_WINDOW * _SAMPLE_RATE)

            scores: dict[str, float] = {}
            for offset in DETECTION_OFFSETS:
                window = waveform[int(offset * _SAMPLE_RATE):][:span]
                # A part-window at the end of a short track still carries a
                # usable reading; a sliver does not.
                if len(window) < span // 2:
                    break
                language, probability, *_ = model.detect_language(window)
                scores[language] = scores.get(language, 0.0) + probability

            if scores:
                return max(scores, key=lambda name: scores[name])
        except Exception:
            # Signatures differ across faster-whisper releases. Say so rather
            # than failing over in silence: the fallback reads the language off
            # a whole-file pass by the detection model, which is a weaker
            # signal, and a wrong answer here costs a full transcription by the
            # wrong model.
            traceback.print_exc()

        _segments, info = model.transcribe(audio_path, without_timestamps=True)
        return info.language

    def transcribe(
        self,
        audio_path: str,
        language: str | None = None,
        initial_prompt: str | None = None,
        on_progress: ProgressFn | None = None,
    ) -> Transcript:
        """Transcribe the audio, detecting the language first if not given.

        Detection runs even when the model is forced: the language is recorded
        on the artifact and decides text direction downstream, not just which
        model to load.
        """
        if language is None:
            if on_progress:
                on_progress("Detecting language...")
            language = self.detect_language(audio_path)
            if on_progress:
                on_progress(f"Detected language: {language}")

        model_name = self.model_override or model_for_language(language)
        if on_progress:
            on_progress(f"Loading model '{model_name}'...")
        model = self._load(model_name)

        options: dict[str, Any] = {
            "word_timestamps": True,
            "beam_size": _BEAM_SIZE,
            # Feeding each window the previous window's text is how Whisper
            # falls into repetition loops on music, and every looping window
            # decodes to the 448-token cap before giving up.
            "condition_on_previous_text": False,
        }
        if _USE_VAD:
            # Skip instrumental passages instead of decoding them. There are
            # no words there to time, only hallucinations to clean up.
            options["vad_filter"] = True
            # Silero's defaults are tuned for speech; sung vocals sitting low
            # in a mix score under its 0.5 threshold and whole outros vanish
            # from the transcript. Detect gently, pad generously.
            options["vad_parameters"] = {
                "threshold": 0.3,
                "min_silence_duration_ms": 1000,
                "speech_pad_ms": 600,
            }
        if language:
            options["language"] = language
        if initial_prompt:
            # Whisper's prompt is a short conditioning hint, not a transcript.
            options["initial_prompt"] = initial_prompt[:1000]

        if on_progress:
            on_progress("Transcribing audio...")
        raw_segments, info = self._start_transcription(model, audio_path, options)
        segments = _collect_segments(raw_segments, on_progress)

        if not segments and _USE_VAD:
            # Silero heard no vocals at all - quiet singing it cannot follow,
            # or a genuine instrumental. Decode everything, as the pre-VAD
            # behaviour did, so downstream still gets whatever there is.
            if on_progress:
                on_progress("No vocals detected; transcribing without the filter...")
            options["vad_filter"] = False
            options.pop("vad_parameters", None)
            raw_segments, info = self._start_transcription(model, audio_path, options)
            segments = _collect_segments(raw_segments, on_progress)

        return Transcript(language=language or info.language, segments=segments)

    @staticmethod
    def _start_transcription(model, audio_path: str, options: dict[str, Any]):
        """Kick off decoding, batched when the runtime supports it.

        Batched decoding feeds several 30-second windows through the model at
        once, which is what actually saturates a multi-core CPU; sequential
        decoding leaves it mostly idle. Falls back to the sequential path on
        older faster-whisper releases.
        """
        if _BATCH_SIZE > 1:
            try:
                from faster_whisper import BatchedInferencePipeline

                pipeline = BatchedInferencePipeline(model)
                return pipeline.transcribe(
                    audio_path, batch_size=_BATCH_SIZE, **options
                )
            except (ImportError, TypeError, ValueError) as exc:
                # Releases differ in what the batched pipeline accepts;
                # decode sequentially rather than fail the stage.
                print(f"Batched transcription unavailable: {exc}", file=sys.stderr)

        # Sequential path. Cap the temperature ladder: on repetitive lyrics
        # the compression-ratio check trips constantly, and the stock ladder
        # re-decodes such windows up to six times.
        return model.transcribe(audio_path, temperature=[0.0, 0.4], **options)
