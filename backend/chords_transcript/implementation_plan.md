# Implementation Plan: Clean SDK-Style `chords_extractor` Package

We will build a clean, self-contained SDK-style Python tool under the folder `C:\Develop\Github\Text_Proj\chords_extractor`. This package will run Librosa, Essentia, and Madmom models, run log-space HMM Viterbi fusion, and output a structured JSON file containing only the final fused beat-level chord predictions.

No HTML reports or visualizers will be created. The project will include its own copy of the Essentia extractor executable and DLLs for maximum portability.

---

## Directory Structure

```
chords_extractor/
├── essentia/
│   └── bin/
│       ├── streaming_extractor_music.exe
│       └── [DLL resources (Qt5Core, avcodec-62, avdevice-62, avfilter-11, avformat-62, avutil-60, pkgconf-7, samplerate, swresample-6, swscale-9, tag, tag_c, yaml, z)]
├── __init__.py
├── cli.py                     # CLI argument handling, runs extractor, and writes JSON
├── extractor.py               # Core DSP extraction, monkeypatching, and HMM Viterbi fusion logic
├── requirements.txt           # Python dependency file (Cython, wheel, numpy, madmom, librosa, soundfile)
├── README.md                  # Clean SDK API documentation and usage guide
└── run.py                     # Root CLI script runner (python run.py song.wav --output chords.json)
```

---

## Output JSON Schema

The output JSON file will contain the final combined results structured beat-by-beat:

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
      "start_time": 0.0,
      "end_time": 0.9174,
      "chord": "C/E"
    }
  ]
}
```

---

## Proposed Changes

We will perform the following actions:

### [COPY] Essentia Binaries
Copy the folder `C:\Develop\Github\Audio-Chords-Lyrics\essentia\bin` containing `streaming_extractor_music.exe` and its 14 dependent DLL files into `C:\Develop\Github\Text_Proj\chords_extractor\essentia\bin`.

### [NEW] [__init__.py](file:///C:/Develop/Github/Text_Proj/chords_extractor/__init__.py)
Initializes the `chords_extractor` module.

### [NEW] [extractor.py](file:///C:/Develop/Github/Text_Proj/chords_extractor/extractor.py)
Modular Python code doing the following:
1. Configures Python 3.11 / NumPy 2.x monkeypatches.
2. Locates the packaged Essentia binary relative to the module file.
3. Loads audio, tracks beat intervals, and runs Librosa, Essentia, and Madmom.
4. Executes the log-Viterbi fusion algorithm over the 109 state space.
5. Performs post-processing slash-chord restoration.
6. Returns a structured dictionary containing metadata and predictions.

### [NEW] [cli.py](file:///C:/Develop/Github/Text_Proj/chords_extractor/cli.py)
Command-line interface module using `argparse`:
- Input argument: path to the audio file.
- Option `--output` / `-o`: path to save the output JSON. Defaults to `<audio_basename>_chords.json`.
- Prints progress messages to stderr to keep stdout clean for piping JSON if needed.

### [NEW] [requirements.txt](file:///C:/Develop/Github/Text_Proj/chords_extractor/requirements.txt)
Specifies dependencies.

### [NEW] [README.md](file:///C:/Develop/Github/Text_Proj/chords_extractor/README.md)
SDK documentation.

### [NEW] [run.py](file:///C:/Develop/Github/Text_Proj/chords_extractor/run.py)
Top-level script runner:
```python
from chords_extractor.cli import main
if __name__ == "__main__":
    main()
```

---

## Verification Plan

### Automated Execution
We will verify that the tool executes correctly:
```powershell
python chords_extractor/run.py C:\Develop\Github\Text_Proj\test3.wav --output test3_chords.json
```
Verify:
- Clean execution without HTML template errors.
- Successful copy and invocation of the internal Essentia binary.
- Generation of `test3_chords.json` matching the schema containing only final fused predictions.
