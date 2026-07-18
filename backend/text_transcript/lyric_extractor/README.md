# Lyric Extractor SDK

A lightweight Python SDK and command-line utility for extracting timestamped song lyrics from audio WAV files.

## Features
- **Zero-Config Web Search (Black Box)**: Automatically identifies the song based on the audio filename or transcription snippet, retrieves official lyrics from the open-source LRClib database, and aligns them with timestamps.
- **Robust Alignment**: Programmatically aligns clean lyrics sheets with Whisper audio segment timestamps using sequence matching.
- **Local Audio Resampling**: Decimates and resamples audio to 16kHz mono internally using pure `numpy` and `soundfile` (no heavy libraries like `librosa` or external `ffmpeg` binary required).
- **Whisper Speech-to-Text**: Utilizes local Whisper models for speech recognition.
- **Structured Timestamps**: Formats segments into standard `[MM:SS.xx]` timestamped lines.

## Directory Structure
```text
lyric_extractor/
  ├── lyric_extractor/
  │    ├── __init__.py
  │    └── core.py       # Main LyricExtractor class with align & search logic
  ├── run_extractor.py   # SDK test runner script
  ├── requirements.txt   # Package dependencies
  ├── setup.py           # Pip installation configuration
  └── README.md          # Documentation
```

## Installation

Install the package in editable mode using `pip`:

```bash
# Navigate to the SDK folder
cd lyric_extractor

# Install the package and dependencies
pip install -e .
```

## SDK Usage

```python
from lyric_extractor import LyricExtractor

# Initialize the extractor (uses the local Whisper "base" model by default)
extractor = LyricExtractor(model_name="base")

# 1. Fully Automated Black-Box Extraction
# Auto-detects language, searches online for lyrics, and returns aligned results
result = extractor.extract_black_box("path/to/song.wav")
print(result["text"])

# 2. Manual Alignment (if you already have the official lyrics text)
raw_res = extractor.extract_lyrics("path/to/song.wav")
aligned_text = extractor.align_lyrics(raw_res["segments"], "Official lyrics sheet content...")
print(aligned_text)
```
