# VidiChord

Turns a YouTube link into a chord sheet with time-synced lyrics, for English
and Hebrew songs, and exports it to [songbook](https://github.com/ohadmanor)
format.

It runs locally: a Python backend does the audio work and serves an Angular
app, which opens in your browser.

---

## The stages

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
  │ 5. Stems      Demucs splits the mix four ways    │──▶ stems/*.ogg
  │               (optional; see below)              │    05_stems.json
  ├──────────────────────────────────────────────────┤
  │ 2. Lyrics     detect language (small model, 30s) │
  │               transcribe the vocals stem once    │──▶ 02_lyrics.json
  │                 (large-v3-turbo, or the          │
  │                  Hebrew-tuned model)             │
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

Stage numbers are identifiers, not the running order. Separation was added
once the artifact files `01`–`04` were already on disk in every song folder,
so it took the next free number rather than renaming them all; it runs second,
which is where it has to run to hand the transcription a clean vocal.

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

### Why the song is taken apart first

[Demucs](https://github.com/adefossez/demucs) splits the recording into four
parts — vocals, drums, bass, and everything else — and both halves of the app
want that.

The player turns them into faders, so a song can be practised against its own
backing track, or the bass line soloed to hear what it actually does. And
stage 2 transcribes the isolated vocal rather than the whole band, which is
where Whisper is at its best: it was trained on speech, and its word timings —
the only thing this app keeps from a transcript — are much better for it.

On the Hebrew test song the difference is not subtle. Transcribing the mix,
Whisper's voice-activity pass finds one region in the whole track and returns
8 words, of which the aligner can pin **6 of the song's 121 lyric words** to a
time. Transcribing the separated vocal, the same model returns 29 segments and
125 words, and times **101 of 121**. Every lyric word that finds no match is a
word whose time is interpolated rather than heard, so this is the difference
between a sheet whose chords sit where they are sung and one that drifts.

The same isolation gives a much better answer
to *does this song have singing at all*: a track with a vocal produces a loud
vocals stem and one without produces near-silence, which beats guessing at a
voice through a full mix. It is still only a hint. Whether a song has words is
always the user's call.

Chord recognition deliberately stays on the full mix. The three engines are
weighted by numbers measured against 15 hand-verified songs, and every song's
per-engine predictions are stored so a re-run can re-fuse them in
milliseconds; feeding the engines different audio would quietly invalidate
both. That is an experiment worth running one day, with the reference sheets
to score it. It is not a thing to assume.

**Demucs is optional**, in the same way madmom is, and for a blunter reason:
it brings PyTorch, about 400 MB installed, which is more than the rest of
VidiChord weighs. So it is not in `requirements.txt` and it is never bundled
into the released executable. Install it when you want it:

```bat
backend\.venv\Scripts\pip install demucs
```

or pass `--with-stems` to `run_local.bat` once. Everything installs from
wheels — unlike madmom, no compiler is involved.

Without it, nothing breaks: stage 5 records why it separated nothing, the
player keeps playing the mix with no mixer to offer, and the lyrics are timed
against the full recording exactly as they were before. With it, expect a
couple of extra minutes per song — measured at **158 s for a 3:39 track**, or
0.72× its length, on a Core Ultra 7 265U with no GPU — and about 13 MB of Opus
stems beside the 37 MB `audio.wav`. A CUDA GPU, if there is one, is used
automatically and turns that into seconds.

The first separation also downloads the model, roughly 80 MB, into
`~/.cache/torch`.

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
devops\scripts\run_local.bat
```

That one command sets up `backend/.venv`, builds the Angular app and starts
the server. It finds the repository itself, so it works from any directory,
and it skips the first two steps once they are done — an ordinary launch goes
straight to the app. Pass `--reinstall` to rebuild the environment or
`--rebuild` to rebuild the interface.

It handles madmom's three quirks: its `setup.py` imports Cython
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

and two for separation, both editable in settings as well:

- `stems_enabled` — whether to split every song into stems (default: yes)
- `stems_model` — which Demucs model to do it with (default: `htdemucs`)

The file is gitignored and optional: every key defaults, and the app writes
the file itself the first time settings are saved. It holds no secrets, and
VidiChord needs no API keys at all — lyrics come from LRClib and Genius, both
open, and transcription runs locally.

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
| `VIDICHORD_DEMUCS=0` | Do not separate stems, whatever the settings say |
| `VIDICHORD_DEMUCS_MODEL` | Separation model (default `htdemucs`; `htdemucs_ft` is better and 4x slower) |
| `VIDICHORD_DEMUCS_DEVICE` | `cpu` or `cuda`; by default CUDA when torch finds a GPU |
| `VIDICHORD_DEMUCS_SEGMENT` | Seconds per chunk, to cut peak memory (htdemucs caps at 7.8) |
| `VIDICHORD_DEMUCS_JOBS` | Parallel separation jobs |
| `VIDICHORD_DEMUCS_THREADS` | Torch CPU threads (default: all cores minus one) |
| `VIDICHORD_COOKIES` | Path to a `cookies.txt` for YouTube requests |
| `VIDICHORD_COOKIES_BROWSER` | Read YouTube cookies from this browser, e.g. `firefox` |
| `VIDICHORD_NO_BROWSER=1` | Do not open a browser on start |
| `VIDICHORD_PORT` | Serve on another port when 8001 is taken (default `8001`) |

---

## Releasing

```bat
devops\scripts\build_release.bat
```

One command, run from anywhere, to the file you hand someone:
`release/VidiChord-<version>-win64.exe`. It sets up the environment itself
and needs only npm on `PATH`; everything else it arranges.

Nine steps, and it stops at the first one that fails:

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

Two things the exe does not carry. Stem separation is one, left out on
purpose: Demucs and PyTorch together outweigh everything else in the bundle,
on a file that already unpacks most of a gigabyte on every launch. The app
says so plainly when the mixer is asked for, and every other feature works.

The other is a JavaScript engine, because it is a separate
program rather than a Python dependency. YouTube signs its download links and
unscrambling them means running the player's own code, so a machine with no
Node.js (or Deno, Bun or QuickJS) can open local audio files but not download
from YouTube. Installing Node.js is the usual answer; dropping `node.exe` beside
`VidiChord.exe` also works, since that folder is searched.

Settings and the song library are written next to the exe, not into the
extraction directory that would take them with it when the app exits — so keep
the exe somewhere writable rather than in `Program Files`.

---

## Song library

```
VidiChord_Files/<Artist> - <Title> [hash]/
    audio.wav
    stems/             vocals.ogg, drums.ogg, bass.ogg, other.ogg
    manifest.json      stage states and timings
    01_source.json     where the audio came from
    05_stems.json      separation model, files, vocal loudness
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

The noise numbers below were measured once, against chord output saved from
before the pipeline rewrite, so the improvement could be counted rather than
eyeballed. They are a record, not something the suite re-runs:

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
reference sheets to compare against, and `backend/tools/` holds them.

`tools/reference.json` is the ground truth: 15 songs, 7,476 beats, each keyed to
the YouTube video it was taken from. **It cannot be regenerated.** It was parsed
from hand-verified Chordify PDF exports that were never committed and no longer
exist, so treat the file as source, not as output. Nothing else can stand in for
it: the pipeline's own `03_chords.json` is the estimate being scored, so using
that would make the objective circular.

Running the tuner needs two things that a fresh checkout does not have:

```bat
cd backend
.venv\Scripts\pip install optuna
python -m tools.tune_chords tools\reference.json --trials 500 --report
```

and a song library holding the 15 reference songs. The tuner matches them by
the `video_id` in each entry, so re-importing those videos and running stages 1
and 3 restores its input; songs it cannot find are listed as skipped, and it
refuses to run a search on too few.

It is fast because it never re-runs the engines: every beat's per-engine
prediction is stored on `03_chords.json`, so a trial re-fuses stored numbers in
milliseconds. Only fusion and cleanup are reachable this way — the beat grid,
the detected key and the engines' own constants are baked into those labels. The
search itself takes minutes; re-importing the songs to feed it takes hours.

The current defaults came from 15 verified songs: **72.0% → 77.7%** agreement at
majmin level, 74.1% → 78.1% on held-out songs, with every song improving. Most
of that is the engine weights alone, and only those were adopted — the search's
key-prior and cleanup values were judged by hand and rejected, so `cleanup.py`'s
defaults are hand-chosen rather than searched. The search will always try to
switch cleanup off, because the objective cannot see flicker — that buys about
1.7 points while pushing short runs from 0% to ~20%, which is why `--report`
prints the noise metrics next to the accuracy. Judge both.

A third script, `tools/chordify_reference.py`, parsed those PDF exports into
`reference.json`. It was removed once its input was gone, because it wrote its
output unconditionally and so would overwrite the ground truth with an empty
file. If you ever export Chordify sheets again, recover it with
`git show 0769afd:backend/tools/chordify_reference.py` — and commit the PDFs
this time, since the `*.pdf` rule in `.gitignore` is what lost them.

### Layout

Every file in the repository, and what it is for.

```
backend/
  main.py                           entry point
  requirements.txt                  Python dependencies and their version floors
  VidiChord_onefile.spec            PyInstaller single-file release build, version-stamped
  vidichord/
    __init__.py                     package root and version number
    config.py                       settings and paths
    models.py                       schemas for the four artifacts
    project.py                      per-song folders and artifact I/O
    jobs.py                         background runs and progress
    server.py                       FastAPI routes
    pipeline/
      __init__.py                   stage runner, shared context and progress
      stage1_audio.py               YouTube or local audio into WAV
      stage2_lyrics.py              transcript, official lyrics, word timings and sections
      stage3_chords.py              three chord engines fused onto a beat grid
      stage4_sheet.py               interleaves lyrics and chords into sheet blocks
      stage5_stems.py               Demucs separation, which never fails a run
    lyrics/
      __init__.py                   public exports for the lyrics package
      whisper_engine.py             Whisper transcription and language detection
      providers.py                  official lyrics from LRClib and Genius
      structure.py                  verse and chorus section detection
      align.py                      timing official lyrics against the transcript
      normalize.py                  Hebrew-aware comparison keys for word matching
    chords/
      __init__.py                   package exports for chord recognition
      beats.py                      beat and downbeat tracking
      engines.py                    the three engines, reduced to beat labels
      fusion.py                     HMM fusion of the engines' predictions
      cleanup.py                    chord noise removal and its metrics
      vocabulary.py                 chord spelling, parsing and key theory
      _madmom_compat.py             compatibility shims for importing madmom
    stems/
      __init__.py                   public exports for source separation
      demucs_engine.py              Demucs, and why it is not always there
    sheet/
      __init__.py                   re-exports for sheet assembly
      layout.py                     aligning chords above lyrics by column
      instrumental.py               detecting intros, solos, interludes and outros
      bars.py                       bar-chart notation for instrumental passages
      export.py                     songbook text and JSON output
    vendor/
      __init__.py                   package marker for bundled binaries
      essentia/                     bundled Essentia extractor and its DLLs
  tools/
    reference.json                  beat-level chord ground truth, 15 songs
    tune_chords.py                  Optuna search for fusion and cleanup settings
  tests/
    conftest.py                     puts backend/ on the test import path
    test_align.py                   lyric-to-transcript alignment and line timing
    test_audio.py                   YouTube cookie, retry and JavaScript engine tests
    test_chords.py                  chord vocabulary, key, fusion and cleanup tests
    test_instrumental.py            lyrics-not-found prompt and instrumental sheets
    test_language.py                sung language detection and transcript cache
    test_lookup.py                  YouTube title to search query variants
    test_project.py                 song id, artifact I/O and manifest tests
    test_server.py                  FastAPI route, audio range and review tests
    test_sheet.py                   chord placement, instrumentals and export
    test_stems.py                   separation, its absence, and what stage 2 reads
    test_structure.py               verse and chorus structure recovery
frontend/
  package.json                      npm dependencies and build scripts
  angular.json                      Angular CLI build and serve configuration
  tsconfig.json                     TypeScript and Angular compiler options
  tsconfig.app.json                 TypeScript settings for the application build
  .editorconfig                     editor whitespace and quote conventions
  .gitignore                        Angular's own ignores: dist, cache, editors
  README.md                         stock Angular CLI usage notes
  public/VidiChord.png              app logo, used as favicon and header mark
  src/
    main.ts                         browser entry point, bootstraps the Angular app
    index.html                      host page holding app-root, title and favicon
    styles.css                      design tokens, fonts and four colour themes
    app/
      app.component.ts              shell: starts runs, follows jobs, saves edits
      app.component.html            toolbar, library, review panes, sheet, settings
      app.component.css             shell layout: header, player, library, modals
      app.config.ts                 bootstrap providers and change-detection setup
      models/artifacts.ts           mirrors the backend schemas, plus tuning defaults
      services/api.service.ts       backend REST client and job progress stream
      services/audio.service.ts     playback: the mix, or a four-stem Web Audio mixer
      components/stem-mixer/
        stem-mixer.component.ts     faders, mutes, solos and presets over the stems
        stem-mixer.component.html   the mixer panel, and what it says without stems
        stem-mixer.component.css    fader and preset styling
      components/sheet-view/
        sheet-view.component.ts     renders and edits the finished song sheet
        sheet-view.component.html   title block, chord rows over lyric rows
        sheet-view.component.css    songbook page styling, right-to-left included
      components/chord-grid/
        chord-grid.component.ts     editable beat grid; halve or double the tempo
        chord-grid.component.html   bar cards and editable beat cells
        chord-grid.component.css    styling for bar cards, repeats and silent beats
      components/waveform/
        waveform.component.ts       decorative canvas bars behind the scrubber
devops/
  scripts/
    run_local.bat                   sets up, builds, and runs the app locally
    build_release.bat               sets up, builds, and packages the exe
.gitignore                          keeps build output, venvs and local config out of git
.gitattributes                      keeps CRLF line endings on batch files
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
GET    /api/songs/{id}/stems               separation record, or why there is none
GET    /api/songs/{id}/stems/{name}        one stem: vocals, drums, bass or other
GET|PUT /api/songs/{id}/{lyrics|chords|sheet}
POST   /api/songs/{id}/export              write a songbook file
GET|PUT /api/config
```

Saving lyrics or chords rebuilds the sheet; saving a sheet stores it verbatim.
Interactive docs are at `/docs` while the server runs.
