# VidiChord

Turns a YouTube link into a chord sheet with time-synced lyrics, for English
and Hebrew songs, and exports it to [songbook](https://github.com/ohadmanor)
format.

It runs locally: a Python backend does the audio work and serves an Angular
app, which opens in your browser.

---

## The four stages

Each stage reads the artifacts produced before it and writes exactly one of its
own. That means any stage can be re-run on its own — change the chord tuning
and re-run stage 3 without transcribing the song again.

```
  YouTube URL
      │
  ┌───▼──────────────────────────────────────────────┐
  │ 1. Audio      yt-dlp + ffmpeg                    │──▶ audio.wav
  │                                                  │    01_source.json
  ├──────────────────────────────────────────────────┤
  │ 2. Lyrics     detect language (tiny model, 30s)  │
  │               transcribe once (large-v3, or the  │──▶ 02_lyrics.json
  │                 Hebrew-tuned model)              │
  │               fetch real lyrics: LRClib → Genius │
  │               align them onto the transcript     │
  │               recover verse/chorus structure     │
  ├──────────────────────────────────────────────────┤
  │ 3. Chords     downbeat tracking → bars & BPM     │
  │               librosa + Essentia + madmom        │──▶ 03_chords.json
  │               HMM fusion, conditioned on key     │
  │               noise cleanup                      │
  ├──────────────────────────────────────────────────┤
  │ 4. Sheet      find passages with no singing      │
  │               place chords over syllables        │──▶ 04_sheet.json
  │               render bar charts for instrumentals│    sheet.txt
  └──────────────────────────────────────────────────┘
```

### Why lyrics come from the web

Whisper hears *when* words are sung accurately and *what* is sung poorly.
Published lyrics are the reverse. So the transcript is used purely as a timing
reference, and the words come from LRClib or Genius, aligned onto it with a
Needleman-Wunsch matcher that tolerates mis-heard, dropped and invented words.

Hebrew gets extra care: niqqud, final letter forms and optional vowels
(*ktiv haser* / *ktiv male*) are all normalised away before comparison, so the
same word spelled two ways still matches.

### Why three chord engines

None is reliable alone, and they fail differently. **librosa** has the richest
vocabulary (sevenths, sus, slash chords) but is confused by percussion, so it
runs on the harmonic half of an HPSS split. **Essentia** only predicts triads
but also reports the key. **madmom** is heavily smoothed, so it anchors the
others.

A hidden Markov model fuses them: emission probabilities say how likely each
engine's label is given a true chord, transition probabilities encode that
chords persist and move by small steps around the circle of fifths, and the
detected key weights chords that belong to it. Viterbi then decodes the whole
song at once, which lets one confident engine overrule two mistaken ones
without producing a progression that lurches between keys.

A cleanup pass then absorbs chord runs shorter than two beats — real music
rarely changes chord for a single beat, so those are almost always noise.

---

## Setup

**Use Python 3.12.** Everything else works on 3.13/3.14, but `madmom` — which
supplies downbeat tracking and one of the three chord engines — cannot be built
there. Without it VidiChord still runs, with bar lines estimated from onset
energy rather than tracked and chords fused from two engines instead of three.

```bat
cd backend
setup.bat            :: creates .venv and installs dependencies
start_backend.bat    :: launches the app and opens a browser
```

`setup.bat` handles madmom's three quirks: its `setup.py` imports Cython
without declaring it (so the build runs with `--no-build-isolation`), the PyPI
sdist ships C files including `longintrepr.h` which Python 3.12 removed (so it
installs from git and lets Cython regenerate them), and it needs NumPy 1.x at
build time. It also needs a C compiler:

```bat
winget install Microsoft.VisualStudio.2022.BuildTools ^
  --override "--quiet --wait --add Microsoft.VisualStudio.Workload.VCTools"
```

The frontend needs building once:

```bat
cd frontend
npm install
npm run build
```

For frontend development, `npm start` serves on `localhost:4200` and talks to
the backend on port 8001.

ffmpeg is downloaded automatically on first use. Node.js on `PATH` helps
yt-dlp with some YouTube downloads.

### Configuration

`backend/config.json` holds two paths, both editable in the app's settings:

- `library_dir` — where songs are stored, one folder each
- `sheets_dir` — where "Export to songbook" writes

The file is gitignored. Copy `config.example.json` to start. It holds no
secrets, and VidiChord needs no API keys at all — lyrics come from LRClib and
Genius, both open, and transcription runs locally.

Environment variables:

| Variable | Effect |
|---|---|
| `VIDICHORD_WHISPER_MODEL` | Force a Whisper model, e.g. `tiny` on a slow machine |
| `VIDICHORD_WHISPER_DEVICE` | `cpu` (default) or `cuda` |
| `VIDICHORD_NO_BROWSER=1` | Do not open a browser on start |

---

## Song library

```
VidiChord_Files/<Artist> - <Title> [hash]/
    audio.wav
    manifest.json      stage states and timings
    01_source.json     where the audio came from
    02_lyrics.json     language, sections, lines, word timings
    03_chords.json     bpm, key, bars → beats → chords
    04_sheet.json      the rendered sheet, block by block
    sheet.txt          songbook raw text
```

The hash makes the identifier unique, so two videos sharing a title do not
collide.

---

## Development

```bat
cd backend
python -m pytest tests/ -q
```

The suite covers the pure logic — alignment, structure recovery, chord
vocabulary, fusion, noise cleanup, chord layout, instrumental detection,
export and the HTTP API — and needs no audio.

Fixture WAVs live in `backend/tests/fixtures/audio/` and are gitignored;
`chords_baseline/` holds the chord output from before the pipeline rewrite, so
noise improvements can be measured rather than eyeballed:

| Song | Before | After |
|---|---|---|
| Always On My Mind | 2.20 changes/bar, 48.9% short runs, 23 chords | **1.10, 0%, 10 chords** |
| עוד לא תמו כל פלאייך | 1.96 changes/bar, 46.2% short runs, 26 chords | **1.03, 0%, 22 chords** |

Runs under two beats — the flicker that made the old output hard to read — are
gone entirely, and the chord vocabulary roughly halves as spurious chords are
absorbed into their neighbours.

Without madmom the same songs give 0.86 and 0.89 changes/bar, but that number
is flattered by stretches decoded as silence: with two engines and no downbeat
tracking, whole bars come back as no-chord. Fewer changes, less music.

madmom also costs time — roughly 280s per song against 75s without, because its
RNN beat tracker and CNN chord model both run over the full audio.

### Layout

```
backend/
  main.py                    entry point
  vidichord/
    config.py                settings and paths
    models.py                schemas for the four artifacts
    project.py               per-song folders and artifact I/O
    jobs.py                  background runs and progress
    server.py                FastAPI routes
    pipeline/stage{1..4}_*.py
    lyrics/                  whisper, providers, structure, alignment
    chords/                  beats, engines, fusion, cleanup, vocabulary
    sheet/                   layout, instrumentals, bar charts, export
    vendor/essentia/         bundled binaries
frontend/
  src/app/
    models/artifacts.ts      mirrors the backend schemas
    services/                api client, audio player
    components/sheet-view/   renders the sheet
    components/chord-grid/   edits the chord grid
```

### API

```
POST   /api/songs                          {url}  → starts a run
GET    /api/songs                          library listing
GET    /api/songs/{id}                     manifest and stage states
DELETE /api/songs/{id}
GET    /api/jobs/{id}/events               progress, server-sent events
POST   /api/songs/{id}/lyrics/choice       resume a paused run
POST   /api/songs/{id}/stages/{n}/rerun    re-run one stage onwards
GET    /api/songs/{id}/audio               range-capable stream
GET|PUT /api/songs/{id}/{lyrics|chords|sheet}
POST   /api/songs/{id}/export              write a songbook file
GET|PUT /api/config
```

Saving lyrics or chords rebuilds the sheet; saving a sheet stores it verbatim.
Interactive docs are at `/docs` while the server runs.
