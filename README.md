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
  │ 2. Lyrics     detect language (small model, 30s) │
  │               transcribe once (large-v3-turbo,   │──▶ 02_lyrics.json
  │                 or the Hebrew-tuned model)       │
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

### When the web does not have them

Neither provider knows every song — older and local repertoire is where they
run out — so the words can always be supplied by hand. Stage 2 stops and offers
the choice: keep the transcript, or paste the real lyrics and have them timed
against the recording like any other source.

That offer is not a moment that can be missed. It is recorded on the song
rather than on the run, so closing the app and coming back to it later asks
again. **Paste lyrics** in the review toolbar makes the same box available at
any time, which is what fixes the other half of the problem: lyrics that *were*
found, but belong to a different recording of the song. Pasting rebuilds the
song from stage 2, so it asks first when there is work to lose.

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

The engines are **not** weighted equally. madmom's pretrained CRF is worth far
more than the other two, and the weights say so — 0.73 against 0.05 each. They
still matter: they break madmom's ties, and librosa is the only source of the
bass notes behind slash chords. Those numbers are measured, not guessed; see
"Tuning chord extraction" below.

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

ffmpeg is downloaded automatically on first use. So are the Whisper models, from
Hugging Face into `~/.cache/huggingface`: roughly 490 MB for the language
detector and 1.6 GB for the transcription model, once, on the first song.

Node.js on `PATH` matters more than it sounds. A YouTube streaming URL is
signed, and answering the challenge means running the player's own JavaScript
in a real engine — yt-dlp no longer has an interpreter of its own. It enables
only Deno by default, so VidiChord names every engine yt-dlp supports and uses
whichever it finds; Node is the one most machines already have. The
`yt-dlp-ejs` package in `requirements.txt` supplies the script Node needs,
which yt-dlp itself ships only for Deno and Bun.

### Configuration

`backend/config.json` holds two paths, both editable in the app's settings:

- `library_dir` — where songs are stored, one folder each
- `sheets_dir` — where "Export to songbook" writes

plus two optional keys for identifying YouTube requests, described below:

- `cookies_file` — a Netscape-format cookie jar to send with them
- `cookies_browser` — a browser to read those cookies from instead

The file is gitignored. Copy `config.example.json` to start. It holds no
secrets, and VidiChord needs no API keys at all — lyrics come from LRClib and
Genius, both open, and transcription runs locally.

### When YouTube says "Sign in to confirm you're not a bot"

YouTube no longer serves audio to unidentified requests. When it decides a
request is one, it withholds every audio format and stage 1 fails with that
message — the app reports what to do, and this is the longer version.

Give the app your own YouTube session, which is what yt-dlp documents:

1. **A cookie jar beside the app.** Export `cookies.txt` with any "Get
   cookies.txt" browser extension while signed in to YouTube, and save it next
   to `VidiChord.exe` (or in `backend/` when running from source). It is found
   with no configuration at all.
2. **Or name a browser** in `cookies_browser` — `firefox` is the one that
   works on Windows. Chrome and Edge encrypt their cookie store so yt-dlp
   cannot read it, and Chrome also locks the file while it is running.

`VIDICHORD_COOKIES` and `VIDICHORD_COOKIES_BROWSER` override both.

A second, separate limit is per-network rather than per-request: fetch a lot in
a short time, or share an office connection, and YouTube answers `429 Too Many
Requests` for a while. No cookie fixes that one — only waiting does.

A third refusal looks alarming and means nothing: a bare `403 Forbidden` on the
media itself, which Google's servers hand out to a large share of perfectly
ordinary requests — measured at roughly half of them on one video, with the
next attempt on a freshly signed URL succeeding. yt-dlp treats a 403 as final
and stops, so stage 1 asks again up to eight times before believing it. This is
why a download sometimes pauses and reports that it is retrying.

Local audio files are unaffected. "Add from file" needs none of this, and is
the reliable path when YouTube is being difficult.

Environment variables:

| Variable | Effect |
|---|---|
| `VIDICHORD_WHISPER_MODEL` | Force the transcription model, e.g. `tiny` on a slow machine. Language detection still runs, and still uses its own model |
| `VIDICHORD_WHISPER_DEVICE` | `cpu` (default) or `cuda` |
| `VIDICHORD_WHISPER_THREADS` | CPU threads for transcription (default: all cores minus two) |
| `VIDICHORD_WHISPER_BEAM` | Beam size (default `1`; the transcript is only a timing reference) |
| `VIDICHORD_WHISPER_BATCH` | Windows decoded per batch (default `1` = sequential; try `8` on CUDA) |
| `VIDICHORD_BEAT_THREADS` | Worker processes for madmom's downbeat ensemble (default: cores/3, max 4; 1 in the single-file exe, where each worker re-extracts the bundle) |
| `VIDICHORD_CHORD_WORKERS` | Worker processes for madmom chord recognition (default: cores/4, max 4; 1 in the single-file exe) |
| `VIDICHORD_WHISPER_VAD` | `0` to transcribe instrumental passages too (default `1`: skip them) |
| `VIDICHORD_COOKIES` | Path to a `cookies.txt` for YouTube requests |
| `VIDICHORD_COOKIES_BROWSER` | Read YouTube cookies from this browser, e.g. `firefox` |
| `VIDICHORD_NO_BROWSER=1` | Do not open a browser on start |
| `VIDICHORD_PORT` | Serve on another port when 8001 is taken (default `8001`) |

---

## Releasing

```bat
release_windows.bat
```

One command, from the repository root, to the file you hand someone:
`release/VidiChord-<version>-win64.exe`. It needs a `.venv` from
`backend/setup.bat` and npm on `PATH`; everything else it arranges itself.

Eight steps, and it stops at the first one that fails:

| | | |
|---|---|---|
| 1 | preflight | the venv, Node.js, npm, and whether madmom is there to bundle |
| 2 | version | read from `vidichord/__init__.py`, which names the exe and stamps its file properties |
| 3 | build tools | PyInstaller 6.x, plus the exe icon converted from the app logo |
| 4 | tests | the whole suite; a red test stops the release (`--skip-tests`) |
| 5 | ffmpeg | fetched now so it can be bundled |
| 6 | frontend | Angular, production configuration, from a cleaned `dist/` |
| 7 | executable | PyInstaller, single file |
| 8 | package | name, size, SHA256, and starting the exe to see that it serves `/api/config` (`--skip-smoke`) |

Step 8 earns its place: a missing hidden import in a PyInstaller bundle is
invisible to the test suite and to the build itself, and only surfaces when the
exe runs. Step 5 does too — ffmpeg is normally downloaded on first use, but in a
single-file build the download target is inside the temporary extraction
directory, which is deleted on exit, so an unbundled ffmpeg would be fetched
again on every launch.

### What the single file costs

Everything is in one exe, so the target machine needs no Python and no installs.
The price is that the bootloader unpacks the whole bundle — most of a
gigabyte — into a temporary folder on *every* launch, before any of the app
runs. Expect to wait. The console window stays open for that reason: it makes
the wait legible, and it carries the pipeline's progress output afterwards.

One thing the exe cannot carry is a JavaScript engine, because it is a separate
program rather than a Python dependency. YouTube signs its download links and
unscrambling them means running the player's own code, so a machine with no
Node.js (or Deno, Bun or QuickJS) can open local audio files but not download
from YouTube. Installing Node.js is the usual answer; dropping `node.exe` beside
`VidiChord.exe` also works, since that folder is searched.

`backend/VidiChord.spec` builds the same app as a folder instead. It starts in
seconds because nothing is unpacked, and it is the better choice for anything
but handing over a single file.

Settings and the song library are written next to the exe, not into the
extraction directory that would take them with it when the app exits — so keep
the exe somewhere writable rather than in `Program Files`.

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

### Tuning chord extraction

Noise metrics say how *tidy* the chords are, not how *right* they are — a config
that returns one chord for the whole song scores perfectly. Correctness needs
reference sheets to compare against, and `backend/tools/` does that:

```bat
cd backend
python -m tools.chordify_reference ..\songs -o tools\reference.json
python -m tools.tune_chords tools\reference.json --trials 500 --report
```

The first reads verified Chordify PDF exports into a beat-indexed reference
(each export names the YouTube video it came from, so it is tied to the exact
audio). The second searches the fusion and cleanup settings with Optuna and
reports duration-weighted agreement, holding out a third of the songs.

It is fast because it never re-runs the engines: every beat's per-engine
prediction is stored on `03_chords.json`, so a trial re-fuses stored numbers in
milliseconds. Only fusion and cleanup are reachable this way — the beat grid,
the detected key and the engines' own constants are baked into those labels.

The current defaults came from 15 verified songs: **72.0% → 77.7%** agreement at
majmin level, 74.1% → 78.1% on held-out songs, with every song improving. Most
of that is the engine weights alone. Note that the search will always try to
switch cleanup off, because the objective cannot see flicker — that buys about
1.7 points while pushing short runs from 0% to ~20%, which is why `--report`
prints the noise metrics next to the accuracy. Judge both.

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
