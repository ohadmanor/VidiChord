/**
 * What the run-progress card shows, worked out from a job - no Angular here.
 *
 * The backend reports each stage's state and its own messages, in its own
 * words: yt-dlp's download lines, Whisper's "Transcribing audio... (1:23
 * done)", a chord summary with tuning metrics in it. This file turns those
 * into rows a person can follow - a title and a plain description per step,
 * a live line while it runs, a result line when it is done - through rule
 * tables. A message no rule knows is shown tidied, never dropped, so a change
 * of wording on the backend degrades to readable raw text rather than a blank
 * row. The raw text is kept too, for the tooltip and the failure box.
 *
 * The backend stages point back here: keep the rules in step with them.
 */
import { Job, JobStep, JobStepState, SheetDoc } from '../../models/artifacts';

export type StepState = JobStepState;
export type RunOutcome = 'running' | 'done' | 'failed' | 'needs_input';
/** How stage 2 was asked to find the lyrics; each reads differently. */
export type LyricsMode = 'auto' | 'manual' | 'ai' | 'instrumental';

export interface StepCopy {
  title: string;
  description: string;
  /** Under the running bar: "Usually 1–4 min, the longest step". */
  typical: string;
  /** Beside a step still to come: "1–4 min". */
  typicalShort: string;
  /** Seconds after which a running step shows `slowHint`. */
  slowAfter: number;
  slowHint: string;
}

export interface ProgressStep extends StepCopy {
  /** 'lookup' | 'stage-<n>' | 'save-lyrics' | 'save-chords' */
  key: string;
  state: StepState;
  /** Progress within the step, 0-100, while it runs; null when unknown. */
  percent: number | null;
  /** One cleaned line: live while running, the result or reason after. */
  detail: string;
  /** The raw text behind it: the tooltip, and the failure box in full. */
  fullText: string;
  /** When it started, as Date.now() milliseconds; running rows only. */
  startedAt: number | null;
  /** How long it took; finished rows only. */
  durationMs: number | null;
}

/**
 * The stages adding a song runs, as stages_from(1, review=True) orders them.
 * Only draws the road before the job exists - the job's own list replaces it
 * under the same keys, so the rows carry on rather than being swapped.
 */
export const ADD_SONG_STAGES = [1, 5, 2, 3];

const STILL = 'Taking longer than usual, but it is still working.';

export const STEP_COPY: Record<string, StepCopy> = {
  lookup: {
    title: 'Find the video',
    description: "Reading the song's title, artist and length from YouTube.",
    typical: 'Usually a few seconds',
    typicalShort: 'a few sec',
    slowAfter: 8,
    slowHint:
      'Just after the app starts this can take up to a minute, while it updates its YouTube downloader.',
  },
  'stage-1': {
    title: 'Download the audio',
    description: "Fetching the song's sound from YouTube and converting it for the analysis.",
    typical: 'Usually under a minute',
    typicalShort: 'under 1 min',
    slowAfter: 90,
    slowHint: 'Taking longer than usual. YouTube can be slow at times, and the app retries by itself.',
  },
  'stage-5': {
    title: 'Separate the voice from the band',
    description:
      'Splitting the recording into vocals, drums, bass and the rest, so the singing can be heard on its own.',
    // Measured at 0.72x the song's length on a CPU (README): a 4-minute song
    // takes about 3, a 6-minute one about 4 and a half.
    typical: "Usually about ¾ of the song's length; seconds with a graphics card",
    typicalShort: '2–5 min',
    slowAfter: 360,
    slowHint:
      "Still working. Without a graphics card this takes about three-quarters of the song's length, " +
      'and the first time also downloads the model (about 80 MB).',
  },
  'stage-2': {
    title: 'Write down the lyrics',
    description:
      'Listening to the singing, finding the published lyrics online, and timing every line to the recording.',
    typical: 'Usually 1–4 min',
    typicalShort: '1–4 min',
    slowAfter: 300,
    slowHint: 'Still working. The first song on a computer also downloads the speech models (about 2 GB), once.',
  },
  'stage-3': {
    title: 'Work out the chords',
    description: 'Finding the beat and the bar lines, then letting three chord detectors vote on every beat.',
    typical: 'Usually 1–5 min; seconds when only the settings changed',
    typicalShort: '1–5 min',
    slowAfter: 420,
    slowHint: 'Still working. Long or busy songs keep the chord detectors busy for a while.',
  },
  'stage-4': {
    title: 'Lay out the song sheet',
    description: 'Placing each chord above the word where it changes.',
    typical: 'A second or two',
    typicalShort: '1–2 sec',
    slowAfter: 15,
    slowHint: STILL,
  },
  'save-lyrics': {
    title: 'Save your lyrics',
    description: 'Keeping the words and sections exactly as you corrected them.',
    typical: 'A moment',
    typicalShort: 'a moment',
    slowAfter: 10,
    slowHint: STILL,
  },
  'save-chords': {
    title: 'Save your chords',
    description: 'Keeping every chord you changed.',
    typical: 'A moment',
    typicalShort: 'a moment',
    slowAfter: 10,
    slowHint: STILL,
  },
};

/** Stage 2 after the user answered "no lyrics found" - it does something else then. */
const LYRICS_VARIANTS: Record<Exclude<LyricsMode, 'auto'>, Partial<StepCopy>> = {
  manual: {
    title: 'Line up your lyrics',
    description: 'Listening to the recording and timing each line you pasted to the singing.',
    typical: 'Usually under a minute',
    typicalShort: 'under 1 min',
    slowAfter: 120,
  },
  ai: {
    title: 'Use the transcript',
    description: 'Turning what was heard into lyric lines and timing them to the music.',
    typical: 'Usually under a minute',
    typicalShort: 'under 1 min',
    slowAfter: 120,
  },
  instrumental: {
    title: 'Skip the lyrics',
    description: 'No words to look for; the sheet will show chords only.',
    typical: 'A moment',
    typicalShort: 'a moment',
    slowAfter: 30,
  },
};

export function copyFor(key: string, mode: LyricsMode = 'auto', label = ''): StepCopy {
  const base: StepCopy = STEP_COPY[key] ?? {
    title: label || 'Working',
    description: '',
    typical: '',
    typicalShort: '',
    slowAfter: 600,
    slowHint: STILL,
  };
  return key === 'stage-2' && mode !== 'auto' ? { ...base, ...LYRICS_VARIANTS[mode] } : base;
}

export function blankStep(key: string, mode: LyricsMode = 'auto', label = ''): ProgressStep {
  return {
    key, ...copyFor(key, mode, label),
    state: 'pending', percent: null, detail: '', fullText: '', startedAt: null, durationMs: null,
  };
}

export function startStep(step: ProgressStep, detail = ''): ProgressStep {
  return { ...step, state: 'running', detail, startedAt: Date.now(), durationMs: null };
}

export function settleStep(
  step: ProgressStep, state: StepState, detail = '', fullText = detail
): ProgressStep {
  return {
    ...step, state, percent: null, detail, fullText, startedAt: null,
    durationMs: step.startedAt === null ? 0 : Date.now() - step.startedAt,
  };
}

/** The three steps of Sync, each ticked off as its own request returns. */
export function syncSteps(): ProgressStep[] {
  return ['save-lyrics', 'save-chords', 'stage-4'].map((key) => blankStep(key));
}

export function sheetSummary(sheet: SheetDoc): string {
  const lines = sheet.blocks.filter((block) => block.type === 'lyric').length;
  const passages = sheet.blocks.length - lines;
  if (!lines) return `Chords only, ${passages} passages`;
  return passages
    ? `${lines} lines with chords, ${passages} instrumental passages`
    : `${lines} lines with chords`;
}

/**
 * A job snapshot as rows.
 *
 * `previous` is what was on screen: a running row keeps its start time
 * rather than re-deriving it from every snapshot, which would jitter its
 * clock by the delivery delay, and keeps its detail when a message is one
 * not worth showing.
 */
export function stepsFromJob(
  job: Job, previous: ProgressStep[] = [], mode: LyricsMode = 'auto'
): ProgressStep[] {
  const now = Date.now();
  const before = new Map(previous.map((step) => [step.key, step] as const));
  const records = job.steps && job.steps.length ? job.steps : legacySteps(job);

  const steps = records.map((record): ProgressStep => {
    const key = `stage-${record.stage}`;
    const step = blankStep(key, mode, record.label);
    const ms = record.elapsed === null ? null : record.elapsed * 1000;
    switch (record.state) {
      case 'running': {
        const prev = before.get(key);
        const held = prev && prev.state === 'running' ? prev : null;
        const cleaned = liveDetail(record.stage, record.message);
        const fresh = ms === null ? null : now - ms;
        const startedAt =
          held && held.startedAt !== null && fresh !== null && Math.abs(held.startedAt - fresh) < 2000
            ? held.startedAt
            : fresh;
        return {
          ...step, state: 'running', percent: record.percent, startedAt,
          detail: cleaned !== null ? cleaned : held ? held.detail : '',
          fullText: cleaned !== null ? record.message : held ? held.fullText : '',
        };
      }
      case 'done':
        return { ...step, state: 'done', detail: summaryDetail(record.stage, record.message),
                 fullText: record.message, durationMs: ms };
      case 'skipped':
        return { ...step, state: 'skipped', detail: skippedDetail(record.message),
                 fullText: record.message, durationMs: ms };
      case 'failed':
        return { ...step, state: 'failed', fullText: record.message || job.error, durationMs: ms };
      case 'needs_input':
        return { ...step, state: 'needs_input', detail: record.message,
                 fullText: record.message, durationMs: ms };
      default:
        return step;
    }
  });

  // Failed before any stage ran, or contact was lost: pin the error on the
  // first unfinished row, so it is shown where the run stopped.
  if (job.state === 'failed' && !steps.some((step) => step.state === 'failed')) {
    const index = steps.findIndex((step) => step.state === 'pending' || step.state === 'running');
    const at = index >= 0 ? index : steps.length - 1;
    if (at >= 0) {
      steps[at] = { ...steps[at], state: 'failed', percent: null, startedAt: null,
                    fullText: job.error || 'The run stopped.' };
    }
  }
  // Queued, or the instant between two stages: the next row is starting.
  if ((job.state === 'queued' || job.state === 'running') && !steps.some((s) => s.state === 'running')) {
    const index = steps.findIndex((step) => step.state === 'pending');
    if (index >= 0) steps[index] = { ...steps[index], state: 'running', detail: 'Starting…' };
  }
  return steps;
}

/** A backend without `steps`: one row, for the stage it names. */
function legacySteps(job: Job): JobStep[] {
  if (!job.stage) return [];
  const state: JobStepState = job.state === 'queued' ? 'running' : job.state;
  return [{
    stage: job.stage, label: job.stage_label, state, percent: null,
    message: job.state === 'failed' ? job.error : job.message, elapsed: null,
  }];
}

/** 0:07, 2:31, 1:02:03 */
export function formatClock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = String(total % 60).padStart(2, '0');
  return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${seconds}` : `${minutes}:${seconds}`;
}

// --- messages --------------------------------------------------------------

type Rule = [RegExp, (match: RegExpMatchArray) => string | null];

/** yt-dlp can colour its progress lines. */
const ANSI = /\u001b\[[0-9;]*m/g;
const FULL_MIX = 'The lyrics are read from the full mix.';
const LYRICS_FROM: Record<string, string> = {
  lrclib: 'from LRClib',
  genius: 'from Genius',
  manual: 'from your pasted lyrics',
  raw: 'from the transcript',
  cache: 'from the saved copy',
};

const clean = (raw: string | null | undefined) => (raw || '').replace(ANSI, '').trim();
const clip = (text: string, max: number) =>
  text.length > max ? text.slice(0, max - 1).trimEnd() + '…' : text;

/** First line only, no leading "[tag] ", "..." as "…", no full stop, 140 characters at most. */
export function tidy(raw: string): string {
  let text = clean(raw).split('\n')[0].replace(/^\[[\w-]+\]\s*/, '');
  if (text.endsWith('...')) text = text.slice(0, -3) + '…';
  else if (text.endsWith('.')) text = text.slice(0, -1);
  return clip(text, 140);
}

function languageName(code: string): string {
  try {
    return new Intl.DisplayNames(['en'], { type: 'language' }).of(code) || code.toUpperCase();
  } catch {
    return code.toUpperCase();
  }
}

/**
 * yt-dlp's "[download]  45.2% of    6.06MiB at    2.34MiB/s ETA 00:02".
 * The percent is not repeated: it sits beside the bar already.
 */
function downloadDetail(text: string): string {
  const match = text.match(/([\d.]+)%\s+of\s+~?\s*([\d.]+)\s*([KMG])iB/);
  if (match) {
    const size = `${Number(match[2]).toFixed(1)} ${match[3]}B`;
    if (Number(match[1]) >= 100) return `Downloaded ${size}`;
    const eta = text.match(/ETA\s+(\d+:\d\d(?::\d\d)?)/); // "ETA Unknown" does not match
    return eta
      ? `Downloading ${size} · about ${eta[1].replace(/^0(\d:)/, '$1')} left`
      : `Downloading ${size}`;
  }
  if (/Destination:/.test(text)) return 'Starting the download…';
  if (/has already been downloaded/.test(text)) return 'Already downloaded';
  if (/Got error|Retrying/i.test(text)) return 'Connection hiccup, trying again…';
  return 'Downloading…';
}

const LIVE: Record<number, Rule[]> = {
  1: [
    [/^Converting to WAV/, () => 'Converting the audio for the analysis…'],
    [/^Downloading FFmpeg/, () => 'Downloading the audio converter (first run only)…'],
    [/^Extracting FFmpeg/, () => 'Setting up the audio converter (first run only)…'],
    [/^Updating yt-dlp/, () => 'Updating the YouTube downloader first…'],
    [/^Updated yt-dlp to .+, asking YouTube again/, () => 'Downloader updated, asking YouTube again…'],
    [/^YouTube refused the download, retrying \((\d+)\/(\d+)\)/,
      (m) => `YouTube refused the download, trying again (${m[1]} of ${m[2]})…`],
    [/^YouTube needs a JavaScript engine/,
      () => 'YouTube needs a JavaScript engine: install Node.js, or put node.exe beside VidiChord.exe'],
    [/^Copying (.+?)\.\.\.$/, (m) => `Copying ${m[1]}…`],
    [/^Audio (already present|ready)/, () => null],
  ],
  5: [
    [/^Separating the mix with /, () => 'Starting the separator…'],
    [/^Loading the .+ model/, () => 'Loading the separation model (downloaded once, the first time)…'],
    [/^Separating stems/, () => 'Separating vocals, drums, bass and the rest…'],
    [/^Wrote the (\w+) stem/, (m) => `Saved the ${m[1]} track`],
    [/^(\d+) stems from |^Stems already separated/, () => null],
  ],
  2: [
    [/^Transcribing audio\.\.\. \((\d+:\d\d) done\)$/,
      (m) => `Listening to the singing: ${m[1]} of the song so far`],
    [/^Transcribing audio/, () => 'Listening to the singing…'],
    [/^Transcribing the separated vocal/, () => 'Listening to the separated vocals…'],
    [/^Reusing cached transcript/, () => 'Using what was heard last time'],
    [/^Cached transcript is /, () => 'Listening again in the language you picked…'],
    [/^Detecting language/, () => 'Working out the language (about 500 MB to download the first time)…'],
    [/^Detected language: (\S+)/, (m) => `Sung in ${languageName(m[1])}`],
    [/^Loading model /, () => 'Loading the speech model (about 1.6 GB to download the first time)…'],
    [/^No vocals detected/, () => "Couldn't pick out a voice, listening to the whole track…"],
    [/^Looking up official lyrics/, () => 'Looking up the published lyrics online…'],
    [/^Searching LRClib for '(.+)'\.\.\.$/,
      (m) => `Searching the lyrics database for “${clip(m[1], 48)}”…`],
    [/^Rejected weak match/, () => "That match didn't fit what was sung, still looking…"],
    [/^Searching the web for lyrics/, () => 'Searching the web for the lyrics…'],
    [/^Found '(.+)' on Genius/, (m) => `Found “${clip(m[1], 60)}” on Genius`],
    [/^Found '(.+)' by (.+?)\.?$/, (m) => `Found “${clip(m[1], 60)}” by ${m[2]}`],
    [/^Aligning pasted lyrics/, () => 'Lining your lyrics up with the singing…'],
    [/^Using the transcript as the lyrics/, () => 'Using what was heard as the lyrics'],
    [/^Timing (\d+) lines/, (m) => `Timing ${m[1]} lines to the music…`],
    [/^Marked instrumental/, () => 'No lyrics to look for'],
    [/^No lyric lines could be produced/, () => 'No lyric lines found, finishing as chords only'],
    [/^\d+ lines across |^Instrumental - /, () => null],
  ],
  3: [
    [/^Loading audio/, () => 'Loading the audio…'],
    [/^Tracking beats and downbeats/, () => 'Finding the beat and the bar lines…'],
    [/^\d+ beats at ([\d.]+) BPM, (\d+)\/4( \(estimated bar lines\))?/,
      (m) => `${Math.round(Number(m[1]))} beats per minute in ${m[2]}/4 time${m[3] ? ', bar lines estimated' : ''}`],
    [/^Collecting chord engines/, () => 'Three chord detectors are listening…'],
    [/^Fusing /, () => "Combining the detectors' votes…"],
    [/^Re-fusing stored engine predictions/, () => 'Re-combining the earlier votes with your settings…'],
    // One engine missing; a real failure arrives as the step failing.
    [/ unavailable: /, () => null],
    [/^\d+ bars/, () => null],
  ],
  4: [
    [/^Laying out the sheet/, () => 'Placing the chords over the words…'],
    [/^Rendering text/, () => 'Writing the sheet…'],
    [/^No chords yet/, () => 'No chords yet, building a lyrics-only sheet…'],
  ],
};

const SUMMARY: Record<number, Rule[]> = {
  1: [
    [/^Audio already present/, () => 'Already downloaded'],
    [/^Audio ready/, () => 'Downloaded'],
  ],
  5: [
    // Not "the mixer is ready": the player picks the stems up when the song
    // opens, after the run - until then the mixer still says there are none.
    [/^(\d+) stems from /, (m) => `Split into ${m[1]} tracks: vocals, drums, bass and the rest`],
    [/^Stems already separated/, () => 'Already separated earlier'],
  ],
  2: [
    [/^(\d+) lines across (\d+) sections \((\w+)\)/,
      (m) => `${m[1]} lines in ${m[2]} sections, ${LYRICS_FROM[m[3]] ?? 'from ' + m[3]}`],
    [/^Instrumental - /, () => 'Instrumental: the sheet will show chords only'],
  ],
  3: [
    [/^(\d+) bars( re-fused from stored predictions)?, key ([^,]+)/,
      (m) => `${m[1]} bars${m[3] === 'unknown' ? '' : ', key of ' + m[3]}${m[2] ? ' (re-used the earlier analysis)' : ''}`],
  ],
  4: [
    [/^(\d+) lyric lines, (\d+) instrumental passages/,
      (m) => `${m[1]} lines with chords, ${m[2]} instrumental passages`],
  ],
};

/** The running row's line. null means "not worth showing": keep the previous one. */
export function liveDetail(stage: number, raw: string): string | null {
  const text = clean(raw);
  // The stage's own banner, and yt-dlp's command-line advice.
  if (!text || /^Stage \d+: /.test(text) || /^(Warning|Error): /.test(text)) return null;
  if (text.startsWith('[download]')) return downloadDetail(text);
  for (const [pattern, write] of LIVE[stage] || []) {
    const match = text.match(pattern);
    if (match) return write(match);
  }
  return tidy(text);
}

/** A finished row's result, from the stage's last message. */
export function summaryDetail(stage: number, raw: string): string {
  const text = clean(raw);
  if (!text || /^Stage \d+: /.test(text)) return '';
  for (const [pattern, write] of SUMMARY[stage] || []) {
    const match = text.match(pattern);
    if (match) return write(match) || '';
  }
  return tidy(text);
}

/** Why a step was skipped, and what that means for the song. */
export function skippedDetail(raw: string): string {
  const text = clean(raw);
  if (/switched off in settings/i.test(text)) return `Turned off in Settings. ${FULL_MIX}`;
  if (/switched off/i.test(text)) return `Turned off. ${FULL_MIX}`;
  if (/build of VidiChord was made without/i.test(text)) {
    return `Not included in this build of the app. ${FULL_MIX}`;
  }
  // The pip command stays in the tooltip.
  if (/not installed/i.test(text)) return `Demucs isn't installed. ${FULL_MIX}`;
  if (/^Stem separation failed/i.test(text)) return `Separation didn't work this time. ${FULL_MIX}`;
  return text ? `Skipped: ${tidy(text)}` : 'Skipped';
}
