# Chords Extractor SDK

A clean, self-contained SDK-style Python tool to extract and fuse beat-aligned chord predictions from multiple models (**Librosa**, **Essentia**, and **Madmom**) using an **HMM Viterbi Decoder**.

This package estimates the song BPM, aligns predictions to a 4-beats-per-bar grid, performs statistical fusion to resolve engine disagreements, and outputs a structured JSON file containing the final prediction sequence.

---

## 📦 Installation & Dependencies

Make sure you have the required Python dependencies installed:
```bash
pip install -r requirements.txt
```

### Packaged Resources
The package contains its own portable copy of the **Essentia** command-line music extractor executable and DLLs under `essentia/bin/`. No separate installation of Essentia is required.

---

## 🚀 Usage

### 1. Command-Line Interface (CLI)

You can run the extractor directly using the root `run.py` script:

```bash
# Output defaults to test_chords.json in the same folder
python run.py path/to/audio/test.wav

# Or specify a custom output path
python run.py path/to/audio/test.wav -o output.json
```

### 2. Python SDK API

Import the extractor directly in your Python code:

```python
from chords_extractor.extractor import extract_chords

# Run extraction (returns a Python dictionary)
chords_data = extract_chords("path/to/song.wav")

# Access metadata and beats
print(f"BPM: {chords_data['metadata']['bpm']}")
for beat in chords_data['beats'][:8]:
    print(f"Bar {beat['bar']}.{beat['beat_in_bar']} ({beat['start_time']}s): {beat['chord']}")
```

---

## 📊 Output JSON Schema

The output JSON file maps chord predictions beat-by-beat:

```json
{
  "metadata": {
    "audio_file": "test3.wav",
    "bpm": 130.81,
    "estimated_key": "C major",
    "duration": 219.01,
    "total_beats": 480,
    "total_bars": 120
  },
  "beats": [
    {
      "beat_index": 1,
      "bar": 1,
      "beat_in_bar": 1,
      "start_time": 0.0,
      "end_time": 0.4587,
      "chord": "C"
    },
    {
      "beat_index": 2,
      "bar": 1,
      "beat_in_bar": 2,
      "start_time": 0.4587,
      "end_time": 0.9174,
      "chord": "C/E"
    }
  ]
}
```
