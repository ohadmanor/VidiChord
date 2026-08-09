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
#: else uses stock large-v3.
HEBREW_MODEL = "ivrit-ai/whisper-large-v3-turbo-ct2"
DEFAULT_MODEL = "large-v3"

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

        model = WhisperModel(name, device=_DEVICE, compute_type=_COMPUTE_TYPE)
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

        options: dict[str, Any] = {"word_timestamps": True}
        if language:
            options["language"] = language
        if initial_prompt:
            # Whisper's prompt is a short conditioning hint, not a transcript.
            options["initial_prompt"] = initial_prompt[:1000]

        if on_progress:
            on_progress("Transcribing audio...")
        raw_segments, info = model.transcribe(audio_path, **options)

        segments: list[Segment] = []
        for raw in raw_segments:
            words = [
                {"text": word.word.strip(), "start": word.start, "end": word.end}
                for word in (getattr(raw, "words", None) or [])
                if word.word.strip()
            ]
            segments.append(
                Segment(
                    start=raw.start,
                    end=raw.end,
                    text=raw.text.strip(),
                    words=words,
                )
            )

        return Transcript(language=language or info.language, segments=segments)
