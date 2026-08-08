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

export type LyricsSourceName = 'lrclib' | 'genius' | 'manual' | 'cache' | 'raw';

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

export interface SectionBlock {
  type: 'section';
  name: string;
  kind: SectionKind;
  start: number;
}

export interface InstrumentalBlock {
  type: 'instrumental';
  kind: SectionKind;
  /** Bar-chart notation, e.g. `// C / G / Am F //`. */
  text: string;
  start: number;
  end: number;
  bar_indices: number[];
}

export interface LyricBlock {
  type: 'lyric';
  /** Monospace chord row aligned to `text`. */
  chord_line: string;
  text: string;
  start: number;
  end: number;
  line_index: number;
}

export type SheetBlock = SectionBlock | InstrumentalBlock | LyricBlock;

export interface SheetDoc {
  title: string;
  artist: string;
  key: string;
  bpm: number;
  is_rtl: boolean;
  blocks: SheetBlock[];
}

// --- jobs and library ------------------------------------------------------

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
}

export interface SongSummary {
  song_id: string;
  title: string;
  artist: string;
  language: string;
  updated_at: string;
  has_audio: boolean;
  stages: Record<string, StageState>;
}

export interface AppConfig {
  library_dir: string;
  sheets_dir: string;
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
  emission_weights: {
    essentia_match: 0.5,
    librosa_match: 0.9,
    madmom_match: 0.5,
    none_state_bias: 0.8,
  },
  transition_probabilities: {
    self_transition: 0.75,
    same_root_diff_quality: 0.05,
    circle_of_fifths_dist_1: 0.7,
    circle_of_fifths_dist_2: 0.4,
    unrelated_chord: 0.001,
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
