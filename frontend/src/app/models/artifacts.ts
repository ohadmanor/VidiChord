/**
 * Mirrors the pipeline artifacts defined in `backend/vidichord/models.py`.
 *
 * These are the wire format for every API call, so the two files must be kept
 * in step. The sheet is rendered directly from `SheetDoc` - the frontend no
 * longer assembles it.
 */

export type SectionKind =
  | 'intro'
  | 'verse'
  | 'pre_chorus'
  | 'chorus'
  | 'bridge'
  | 'solo'
  | 'interlude'
  | 'outro'
  | 'other';

export type LyricsSourceName =
  | 'lrclib'
  | 'genius'
  | 'manual'
  | 'cache'
  | 'raw'
  /** The song has no lyrics; the sheet is chords-only bar charts. */
  | 'instrumental';

export type StageState = 'pending' | 'running' | 'done' | 'failed' | 'needs_input';

/** Label used by the backend for "no chord sounding". */
export const NO_CHORD = 'N';

// --- stage 1 ---------------------------------------------------------------

export interface SourceDoc {
  kind: 'youtube' | 'local';
  url: string;
  video_id: string;
  title: string;
  artist: string;
  uploader: string;
  duration: number;
  audio_filename: string;
  downloaded_at: string;
}

// --- stage 5, which runs between stages 1 and 2 ----------------------------

/** The four parts the separator splits a mix into. */
export type StemName = 'vocals' | 'drums' | 'bass' | 'other';

export const STEM_NAMES: StemName[] = ['vocals', 'drums', 'bass', 'other'];

export interface StemFile {
  name: StemName;
  /** Path relative to the song folder, e.g. `stems/vocals.ogg`. */
  filename: string;
  bytes: number;
}

export interface StemsDoc {
  model: string;
  device: string;
  format: string;
  duration: number;
  /** Loudness of the isolated vocal; near silence means an instrumental. */
  vocals_rms_db: number;
  audio_fingerprint: string;
  separated_at: string;
  stems: StemFile[];
  /** Empty when separation ran; otherwise why it did not, in words to show. */
  unavailable: string;
}

// --- stage 2 ---------------------------------------------------------------

export interface Word {
  text: string;
  start: number;
  end: number;
  /** Character column of the word within its line; -1 when unknown. */
  offset: number;
}

export interface LyricLine {
  index: number;
  section_index: number | null;
  time: number;
  end: number;
  text: string;
  words: Word[];
}

export interface Section {
  index: number;
  name: string;
  kind: SectionKind;
  line_start: number;
  line_end: number;
}

export interface LyricsDoc {
  language: string;
  source: LyricsSourceName;
  title: string;
  artist: string;
  sections: Section[];
  lines: LyricLine[];
}

// --- stage 3 ---------------------------------------------------------------

export interface Beat {
  index: number;
  beat_in_bar: number;
  start: number;
  end: number;
  chord: string;
  /** Per-engine predictions, kept for debugging and weight tuning. */
  sources: Record<string, string>;
}

export interface Bar {
  index: number;
  start: number;
  end: number;
  beats: Beat[];
}

export interface ChordsDoc {
  bpm: number;
  time_signature: number;
  key: string;
  pickup_beats: number;
  /** False when the bar grid was estimated rather than tracked. */
  downbeats_tracked: boolean;
  bars: Bar[];
}

// --- stage 4 ---------------------------------------------------------------

export interface InstrumentalBlock {
  type: 'instrumental';
  kind: SectionKind;
  /** Bar-chart notation, e.g. `// C / G / Am F //`. */
  text: string;
  start: number;
  end: number;
  bar_indices: number[];
  /**
   * First block of a new section. Sections are not named on the sheet, so this
   * is what puts a paragraph break where a heading would otherwise go.
   */
  starts_section: boolean;
}

export interface LyricBlock {
  type: 'lyric';
  /** Monospace chord row aligned to `text`. */
  chord_line: string;
  text: string;
  start: number;
  end: number;
  line_index: number;
  /** See `InstrumentalBlock.starts_section`. */
  starts_section: boolean;
}

export type SheetBlock = InstrumentalBlock | LyricBlock;

export interface SheetDoc {
  title: string;
  artist: string;
  key: string;
  bpm: number;
  is_rtl: boolean;
  blocks: SheetBlock[];
}

// --- jobs and library ------------------------------------------------------

/** A stage's state within one run: the manifest's states, plus skipped. */
export type JobStepState = StageState | 'skipped';

/** One stage a run works through, as the run sees it. */
export interface JobStep {
  stage: number;
  label: string;
  state: JobStepState;
  /** Progress within this stage, 0-100; null until it reports one. Never goes backwards. */
  percent: number | null;
  /** The latest message while running; afterwards its result, skip reason, error or question. */
  message: string;
  /** Seconds so far while running, or in total once finished; null while pending. */
  elapsed: number | null;
}

export interface Job {
  job_id: string;
  song_id: string;
  state: 'queued' | 'running' | 'done' | 'failed' | 'needs_input';
  stage: number;
  stage_label: string;
  message: string;
  percent: number;
  error: string;
  /** Choices offered when `state` is `needs_input`. */
  options: string[];
  updated_at: string;
  version: number;
  /**
   * The stages this run works through, in running order, and each one's own
   * record. Optional so that the app still renders - one row, from `stage` -
   * against a backend from before they existed.
   */
  stages?: number[];
  stage_percent?: number | null;
  steps?: JobStep[];
}

export interface SongSummary {
  song_id: string;
  title: string;
  artist: string;
  language: string;
  updated_at: string;
  has_audio: boolean;
  /** Whether this song has stems the player can mix. */
  has_stems: boolean;
  stages: Record<string, StageState>;
}

/** One stage's state in the manifest, with the note the pipeline left on it. */
export interface StageStatus {
  state: StageState;
  updated_at: string;
  message: string;
  error: string;
}

/** The stages, by the names the manifest and the summary use. */
export type StageName = 'audio' | 'stems' | 'lyrics' | 'chords' | 'sheet';

/**
 * The order the stages run in, which is not the order they are numbered in.
 * Separation was added last and took stage number 5, but it runs second - see
 * `DEFAULT_ORDER` in `backend/vidichord/pipeline/__init__.py`.
 */
export const STAGE_ORDER: StageName[] = ['audio', 'stems', 'lyrics', 'chords', 'sheet'];

/** One-letter pills, because two of the five stages start with an S. */
export const STAGE_INITIALS: Record<StageName, string> = {
  audio: 'A',
  stems: 'M',
  lyrics: 'L',
  chords: 'C',
  sheet: 'S',
};

export interface Manifest {
  song_id: string;
  title: string;
  artist: string;
  language: string;
  created_at: string;
  updated_at: string;
  // Partial because a manifest written by an older build may not carry every
  // stage - and because saying so is what makes reading one a checked access
  // rather than a promise the compiler cannot keep.
  stages: Partial<Record<StageName, StageStatus>>;
}

/**
 * One song, as `GET /api/songs/{id}` returns it.
 *
 * The manifest is the durable record: jobs live in memory and are forgotten
 * when the server restarts, so a run that stopped to ask the user something is
 * only still visible here.
 */
export interface SongDetail extends SongSummary {
  manifest: Manifest;
  job: Job | null;
}

export interface AppConfig {
  library_dir: string;
  sheets_dir: string;
  /** Cookie jar sent with YouTube requests, so they are not seen as a bot. */
  cookies_file: string;
  /** A browser to read those cookies from instead. */
  cookies_browser: string;
  /** Whether to separate every song into stems. Costs minutes per song. */
  stems_enabled: boolean;
  /** Which Demucs model to separate with; empty means the default. */
  stems_model: string;
}

// --- tuning ----------------------------------------------------------------

export interface FusionConfig {
  emission_weights: {
    essentia_match: number;
    librosa_match: number;
    madmom_match: number;
    none_state_bias: number;
  };
  transition_probabilities: {
    self_transition: number;
    same_root_diff_quality: number;
    circle_of_fifths_dist_1: number;
    circle_of_fifths_dist_2: number;
    unrelated_chord: number;
  };
  key_prior: {
    enabled: boolean;
    diatonic: number;
    same_root: number;
    foreign: number;
  };
}

export interface CleanupConfig {
  min_chord_beats: number;
  bar_snap_threshold: number;
  fill_isolated_silence: boolean;
}

export const DEFAULT_FUSION: FusionConfig = {
  // Keep in step with EmissionWeights/TransitionWeights in
  // backend/vidichord/chords/fusion.py - these are fitted values.
  emission_weights: {
    essentia_match: 0.05,
    librosa_match: 0.05,
    madmom_match: 0.73,
    none_state_bias: 0.47,
  },
  transition_probabilities: {
    self_transition: 0.7,
    same_root_diff_quality: 0.0013,
    circle_of_fifths_dist_1: 0.0012,
    circle_of_fifths_dist_2: 0.096,
    unrelated_chord: 0.000053,
  },
  key_prior: {
    enabled: true,
    diatonic: 1.0,
    same_root: 0.45,
    foreign: 0.12,
  },
};

export const DEFAULT_CLEANUP: CleanupConfig = {
  min_chord_beats: 2,
  bar_snap_threshold: 0.75,
  fill_isolated_silence: true,
};
