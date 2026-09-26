import { CommonModule } from '@angular/common';
import { Component, OnDestroy, OnInit, effect } from '@angular/core';
import { FormsModule } from '@angular/forms';
import {
  LucideAlertCircle,
  LucideFileText,
  LucideLayers,
  LucideMusic,
  LucidePause,
  LucidePlay,
  LucideSettings,
  LucideSliders,
  LucideVolume2,
  LucideX,
} from '@lucide/angular';
import { ChordGridComponent } from './components/chord-grid/chord-grid.component';
import { RunProgressComponent } from './components/run-progress/run-progress.component';
import {
  ADD_SONG_STAGES,
  LyricsMode,
  ProgressStep,
  RunOutcome,
  blankStep,
  formatClock,
  settleStep,
  sheetSummary,
  startStep,
  stepsFromJob,
  syncSteps,
} from './components/run-progress/run-progress.model';
import { SheetViewComponent } from './components/sheet-view/sheet-view.component';
import { StemMixerComponent } from './components/stem-mixer/stem-mixer.component';
import { WaveformComponent } from './components/waveform/waveform.component';
import {
  AppConfig,
  ChordsDoc,
  CleanupConfig,
  DEFAULT_CLEANUP,
  DEFAULT_FUSION,
  FusionConfig,
  Job,
  LyricBlock,
  LyricsDoc,
  SheetBlock,
  SheetDoc,
  SongDetail,
  SongSummary,
  STAGE_INITIALS,
  STAGE_ORDER,
  StageName,
  StemName,
  StemsDoc,
  STEM_NAMES,
} from './models/artifacts';
import { ApiService } from './services/api.service';
import { AudioService } from './services/audio.service';

const MIXER_STORAGE_PREFIX = 'vidiChordMixer:';
const FUSION_STORAGE_KEY = 'vidiChordFusionConfig';
const CLEANUP_STORAGE_KEY = 'vidiChordCleanupConfig';
const TUNING_VERSION_KEY = 'vidiChordTuningVersion';

/**
 * Bump whenever the shipped chord-detection defaults change.
 *
 * The stored tuning is sent to the backend on every run, so it wins over the
 * backend's own defaults. Without this stamp anyone who had opened the app
 * before an upgrade would keep the old values forever - seeing stale numbers
 * in Settings and, worse, silently still getting the old behaviour.
 */
const TUNING_VERSION = '1.0.4';

/**
 * Overlay stored tuning on the defaults, one level into the nested groups.
 *
 * A plain spread would replace a whole group (`emission_weights` and friends)
 * with the stored copy, so any field added to that group since would come back
 * undefined and reach the backend as a missing weight.
 */
function mergeTuning<T extends object>(defaults: T, stored: any): T {
  if (!stored || typeof stored !== 'object') return defaults;
  const merged: any = { ...defaults };
  for (const [key, value] of Object.entries(stored)) {
    const base = (defaults as any)[key];
    merged[key] =
      base && typeof base === 'object' && value && typeof value === 'object'
        ? { ...base, ...value }
        : value;
  }
  return merged;
}

/**
 * What to say when the paused run's own words are not to be had.
 *
 * The pipeline writes its reason onto the manifest, but a run interrupted
 * mid-flight may leave none, so the panel still needs something to explain
 * itself with.
 */
const DEFAULT_CHOICE_REASON =
  'No lyrics were found for this song. Use the transcript, paste the lyrics, ' +
  'or mark the song instrumental.';

/** How long a finished or paused run stays on screen before the next one. */
const RUN_DONE_BEAT_MS = 900;
/** Sync never just flashes: it shows for at least this long... */
const SYNC_MIN_MS = 1000;
/** ...and holds its finished state for at least this long. */
const SYNC_DONE_BEAT_MS = 600;
const delay = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

const NEXT_TEXT =
  'Next, the lyrics and chords open side by side for you to check. Sync then builds the song sheet.';

type LyricsChoice = 'ai' | 'manual' | 'instrumental';
type RunKind = 'add' | 'rerun' | 'stems' | 'choice' | 'resume';

const RERUN_HEADINGS: Record<number, string> = {
  1: 'Adding your song',
  5: 'Separating the voice from the band',
  2: 'Redoing the lyrics',
  3: 'Redoing the chords',
};
const CHOICE_HEADINGS: Record<LyricsChoice, string> = {
  manual: 'Lining up your lyrics',
  ai: 'Using the transcript',
  instrumental: 'Making a chords-only sheet',
};

/** A run on screen: the steps it works through, and how it ended. */
interface RunView {
  kind: RunKind;
  heading: string;
  songTitle: string;
  songArtist: string;
  /** The client-side "Find the video" row of an add, before any job exists. */
  lookup: ProgressStep | null;
  /** The lookup, if there is one, then the job's own rows. */
  steps: ProgressStep[];
  outcome: RunOutcome;
  lyricsMode: LyricsMode;
  /** The lyrics answer a choice run was started with, for Try again. */
  choice: LyricsChoice | null;
}

/**
 * Application shell.
 *
 * Chord and lyric alignment now happens in the backend pipeline, so this
 * component's job is limited to driving it: start a run, follow its progress,
 * load the resulting artifacts, and write edits back.
 */
@Component({
  selector: 'app-root',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    WaveformComponent,
    SheetViewComponent,
    ChordGridComponent,
    StemMixerComponent,
    RunProgressComponent,
    LucideMusic,
    LucideLayers,
    LucideFileText,
    LucidePlay,
    LucidePause,
    LucideVolume2,
    LucideAlertCircle,
    LucideSettings,
    LucideSliders,
    LucideX,
  ],
  templateUrl: './app.component.html',
  styleUrls: ['./app.component.css'],
})
export class AppComponent implements OnInit, OnDestroy {
  // --- current song --------------------------------------------------------
  songId = '';
  lyrics: LyricsDoc | null = null;
  chords: ChordsDoc | null = null;
  sheet: SheetDoc | null = null;
  /** What separation produced, or why it produced nothing. Null before stage 5. */
  stems: StemsDoc | null = null;
  showMixer = false;

  /** The stage pills, in the order the stages actually run. */
  readonly stageOrder = STAGE_ORDER;
  readonly stageInitials = STAGE_INITIALS;

  // --- run state -----------------------------------------------------------
  youtubeUrl = '';
  job: Job | null = null;
  error = '';
  success = '';
  busy = false;
  /**
   * Set while the request that adds a song is in flight - before there is a
   * job to show. It reads the video's details first, and just after start-up
   * it may also wait for yt-dlp to finish updating, so it can take a while.
   */
  starting = false;
  /**
   * The run on screen, step by step. Set from the click that starts it until
   * the next screen shows, or until it is closed after failing - so it
   * outlives the job, and the screen is decided by it rather than by
   * isRunning. Buttons are still disabled by isRunning and `starting`.
   */
  run: RunView | null = null;
  /** Sync's own steps, shown over the review while it saves and lays out. */
  syncRun: { steps: ProgressStep[]; outcome: RunOutcome } | null = null;
  /** Bumped whenever a run view begins or ends, so a stale pause does nothing. */
  private runToken = 0;
  /** The song's names before any lyrics exist, from adding or opening it. */
  private knownTitle = '';
  private knownArtist = '';
  /** The song whose audio the player has been given, once it existed. */
  private audioLoadedFor = '';

  // --- library -------------------------------------------------------------
  library: SongSummary[] = [];
  showLibrary = false;

  // --- lyrics choice modal -------------------------------------------------
  showChoiceModal = false;
  manualLyrics = '';
  selectedLanguage = 'auto';
  /** Why the panel is open, in the pipeline's own words when it has any. */
  choiceReason = '';
  /**
   * True when the panel is a free choice rather than a stalled run waiting to
   * be answered. It decides both what the panel offers - a stalled run can
   * still fall back to the transcript, a finished song has nothing to fall
   * back to - and what going through with it costs.
   *
   * Derived from the song, not from how the panel was opened: dismissing a
   * stalled run's panel and reaching for it again must not turn the transcript
   * option off.
   */
  get choiceIsVoluntary(): boolean {
    return !this.lyricsNeedsInput;
  }
  /** Whether stage 2 is parked waiting to be told what the lyrics are. */
  lyricsNeedsInput = false;
  /** Set while a choice is being submitted, so it cannot be submitted twice. */
  choiceSubmitting = false;

  // --- editors -------------------------------------------------------------
  /**
   * Which half of the workflow is on screen.
   *
   * A run stops once the chords are in and lands on `review`, where the lyrics
   * and the chords sit side by side and can be corrected. Sync lays out the
   * sheet from them and moves to `sheet`.
   */
  view: 'review' | 'sheet' = 'review';
  sheetMode: 'view' | 'edit' = 'view';
  /** Plain-text mirror of the lyrics document, for bulk editing. */
  lyricsText = '';
  /** Edits made in the review panes but not yet written to disk. */
  dirty = false;
  syncing = false;
  isExporting = false;

  // --- settings ------------------------------------------------------------
  showSettingsModal = false;
  showAdvanced = false;
  config: AppConfig = {
    library_dir: '',
    sheets_dir: '',
    cookies_file: '',
    cookies_browser: '',
    stems_enabled: true,
    stems_model: '',
  };
  fusion: FusionConfig = structuredClone(DEFAULT_FUSION);
  cleanup: CleanupConfig = structuredClone(DEFAULT_CLEANUP);
  settingsMessage = '';

  // --- playbar hover -------------------------------------------------------
  showHoverTooltip = false;
  hoverLeft = 0;
  hoverTimeText = '';

  private stopWatching: (() => void) | null = null;

  constructor(public audioService: AudioService, private api: ApiService) {
    effect(() => {
      this.audioService.setVolume(this.audioService.volume());
    }, { allowSignalWrites: true });

    // Remember where the faders were left, per song. Reading the three
    // signals is what subscribes this to every move of them.
    effect(() => {
      const state = {
        levels: this.audioService.levels(),
        muted: this.audioService.muted(),
        soloed: this.audioService.soloed(),
      };
      if (!this.songId || !this.audioService.mixing()) return;
      try {
        localStorage.setItem(MIXER_STORAGE_PREFIX + this.songId, JSON.stringify(state));
      } catch {
        // A full or disabled store is no reason to stop playing.
      }
    });
  }

  async ngOnInit(): Promise<void> {
    this.restoreTuning();
    try {
      this.config = await this.api.getConfig();
      await this.refreshLibrary();
    } catch (err) {
      this.error = this.describe(err);
    }
  }

  ngOnDestroy(): void {
    this.stopWatching?.();
  }

  // --- helpers -------------------------------------------------------------

  private describe(err: unknown): string {
    return err instanceof Error ? err.message : String(err);
  }

  private flash(message: string): void {
    this.success = message;
    setTimeout(() => (this.success = ''), 3500);
  }

  private restoreTuning(): void {
    // Tuning saved before the current defaults were measured is discarded
    // rather than merged: keeping it would quietly override the new defaults
    // on every run. Deliberate edits are worth preserving, a stale copy of a
    // superseded default is not.
    if (localStorage.getItem(TUNING_VERSION_KEY) !== TUNING_VERSION) {
      localStorage.removeItem(FUSION_STORAGE_KEY);
      localStorage.removeItem(CLEANUP_STORAGE_KEY);
      localStorage.setItem(TUNING_VERSION_KEY, TUNING_VERSION);
      return;
    }

    for (const [key, target] of [
      [FUSION_STORAGE_KEY, 'fusion'],
      [CLEANUP_STORAGE_KEY, 'cleanup'],
    ] as const) {
      const stored = localStorage.getItem(key);
      if (!stored) continue;
      try {
        (this as any)[target] = mergeTuning((this as any)[target], JSON.parse(stored));
      } catch {
        localStorage.removeItem(key);
      }
    }
  }

  private persistTuning(): void {
    localStorage.setItem(FUSION_STORAGE_KEY, JSON.stringify(this.fusion));
    localStorage.setItem(CLEANUP_STORAGE_KEY, JSON.stringify(this.cleanup));
    localStorage.setItem(TUNING_VERSION_KEY, TUNING_VERSION);
  }

  get isRunning(): boolean {
    return this.job?.state === 'queued' || this.job?.state === 'running';
  }

  /**
   * Title and artist for the header.
   *
   * The sheet does not exist until the first sync, so during review these come
   * from the lyrics. Kept out of the template: chained `?.` reads in one
   * expression hit an Angular 18 codegen bug that throws while rendering.
   */
  get displayTitle(): string {
    return this.sheet?.title || this.lyrics?.title || this.songId;
  }

  get displayArtist(): string {
    return this.sheet?.artist || this.lyrics?.artist || '';
  }

  /** Text direction for the lyrics editor. A getter for the same reason. */
  get lyricsDir(): 'rtl' | 'ltr' {
    return this.lyrics?.language === 'he' ? 'rtl' : 'ltr';
  }

  // --- the run on screen ---------------------------------------------------

  private beginRun(
    kind: RunKind,
    heading: string,
    options: { lookup?: ProgressStep; plan?: number[]; lyricsMode?: LyricsMode; choice?: LyricsChoice } = {}
  ): number {
    this.runToken += 1;
    const lookup = options.lookup ?? null;
    const plan = (options.plan ?? []).map((n) => blankStep(`stage-${n}`, options.lyricsMode));
    this.run = {
      kind,
      heading,
      songTitle: this.sheet?.title || this.lyrics?.title || this.knownTitle,
      songArtist: this.sheet?.artist || this.lyrics?.artist || this.knownArtist,
      lookup,
      steps: lookup ? [lookup, ...plan] : plan,
      outcome: 'running',
      lyricsMode: options.lyricsMode ?? 'auto',
      choice: options.choice ?? null,
    };
    return this.runToken;
  }

  private endRun(): void {
    this.runToken += 1;
    this.run = null;
  }

  private showJob(job: Job): void {
    const run = this.run;
    if (!run) return;
    const jobSteps = stepsFromJob(job, run.steps, run.lyricsMode);
    this.run = { ...run, steps: run.lookup ? [run.lookup, ...jobSteps] : jobSteps };
  }

  get runNextText(): string {
    const run = this.run;
    const audio = run ? run.steps.find((step) => step.key === 'stage-1') : undefined;
    const playable = !!this.songId && (!audio || audio.state === 'done');
    return playable ? `${NEXT_TEXT} Meanwhile, you can play the song from the player above.` : NEXT_TEXT;
  }

  // --- starting and following a run ---------------------------------------

  async startFromYoutube(): Promise<void> {
    const url = this.youtubeUrl.trim();
    if (!url || this.isRunning || this.starting) return;

    this.error = '';
    this.success = '';
    this.resetSong();
    this.persistTuning();

    // The whole road shows from the click: reading the video's details is
    // step one, and the stages the job will run are laid out behind it.
    this.starting = true;
    const token = this.beginRun('add', 'Adding your song', {
      lookup: startStep(blankStep('lookup'), 'Asking YouTube about this video…'),
      plan: ADD_SONG_STAGES,
    });
    try {
      const created = await this.api.createFromYoutube(url, {
        language: this.selectedLanguage === 'auto' ? null : this.selectedLanguage,
        fusion: this.fusion,
        cleanup: this.cleanup,
        review: true,
      });
      if (token !== this.runToken || !this.run || !this.run.lookup) return;
      this.songId = created.song_id;
      this.knownTitle = created.title || '';
      this.knownArtist = created.artist || '';
      const found = created.title ? `Found “${created.title}”` : 'Found it';
      const lookup = settleStep(
        this.run.lookup,
        'done',
        created.duration ? `${found}, ${formatClock(created.duration * 1000)} long` : found
      );
      this.run = {
        ...this.run,
        lookup,
        songTitle: this.knownTitle,
        songArtist: this.knownArtist,
        steps: [lookup, ...this.run.steps.slice(1)],
      };
      this.watch(created.job);
    } catch (err) {
      if (token !== this.runToken || !this.run || !this.run.lookup) return;
      // Shown in full on the step itself, so not in the alert as well.
      const lookup = settleStep(this.run.lookup, 'failed', '', this.describe(err));
      this.run = { ...this.run, lookup, steps: [lookup, ...this.run.steps.slice(1)], outcome: 'failed' };
    } finally {
      this.starting = false;
    }
  }

  private watch(job: Job): void {
    if (!this.run) this.beginRun('resume', 'Still working on this song');
    this.job = job;
    this.showJob(job);
    this.stopWatching?.();
    this.stopWatching = this.api.watchJob(
      job.job_id,
      (update) => {
        if (update.song_id && update.song_id !== this.songId) return;
        this.job = update;
        this.showJob(update);
        // Load the audio as soon as stage 1 has produced it - forcibly, since
        // the player may have been pointed at it before it existed and got a
        // 404 it would otherwise never retry.
        if (update.stage >= 2 && this.audioLoadedFor !== this.songId) {
          this.audioLoadedFor = this.songId;
          this.audioService.loadTrack(this.api.audioUrl(this.songId), true);
        }
      },
      (final) => void this.onJobFinished(final)
    );
  }

  private async onJobFinished(job: Job): Promise<void> {
    // A run for a song no longer open - deleted, say - has nothing to show.
    if (job.song_id && job.song_id !== this.songId) return;
    if (!this.run) this.beginRun('resume', 'Working on this song');
    this.showJob(job);
    const run = this.run!;

    if (job.state === 'failed') {
      // The card stays, the failed step showing why, until Try again or
      // Close. Whatever was pasted is kept: the run failing is exactly when
      // the user needs another go at it, and retyping the lyrics is the one
      // part of that they cannot get back. The reload below is what drops
      // it, and that only happens once the run has worked.
      this.run = { ...run, outcome: 'failed' };
      return;
    }

    const token = this.runToken;
    if (job.state === 'needs_input') {
      this.run = { ...run, outcome: 'needs_input' };
      await delay(RUN_DONE_BEAT_MS);
      if (token !== this.runToken) return;
      this.endRun();
      this.lyricsNeedsInput = true;
      this.choiceReason = job.message || DEFAULT_CHOICE_REASON;
      this.showChoiceModal = true;
      return;
    }

    // A moment on the finished card - which also covers loading the song -
    // then on to the review by itself.
    this.run = { ...run, outcome: 'done' };
    await Promise.all([this.loadSong(this.songId, { keepRun: true }), delay(RUN_DONE_BEAT_MS)]);
    // Another song opened, or another run started, meanwhile.
    if (token !== this.runToken) return;
    this.endRun();
    // The run deliberately stopped before the sheet, so show the editor even
    // when an earlier sheet is still on disk from a previous sync.
    this.view = 'review';
    this.flash('Lyrics and chords are ready to review.');
    await this.refreshLibrary();
  }

  /** Try a failed run again, the way it was first asked for. */
  retryRun(): void {
    const run = this.run;
    const failed = run ? run.steps.find((step) => step.state === 'failed') : undefined;
    if (!run || !failed) return;
    // The URL is still in the box.
    if (failed.key === 'lookup') {
      void this.startFromYoutube();
      return;
    }
    // The pasted text is kept until a run succeeds.
    if (run.kind === 'choice' && run.choice) {
      void this.submitChoice(run.choice);
      return;
    }
    // From the stage that failed, reusing everything before it.
    void this.rerun(Number(failed.key.slice('stage-'.length)));
  }

  dismissRun(): void {
    this.endRun();
  }

  async submitChoice(choice: 'ai' | 'manual' | 'instrumental'): Promise<void> {
    // Not while another run is going: its own stage 2 would be the one that
    // ran, and this answer would be silently lost.
    if (!this.songId || this.choiceSubmitting || this.isRunning) return;
    if (choice === 'manual' && !this.manualLyrics.trim()) return;
    // Nothing about the run that follows is idempotent - it rewrites the
    // lyrics and re-fuses every chord - and the server starts one per request
    // without checking whether another is already going.
    this.choiceSubmitting = true;
    try {
      const job = await this.api.submitLyricsChoice(this.songId, choice, {
        lyrics: this.manualLyrics,
        language: this.selectedLanguage === 'auto' ? null : this.selectedLanguage,
        fusion: this.fusion,
        cleanup: this.cleanup,
        review: true,
      });
      // Closed only once the run is accepted. Closing first loses the pasted
      // text on a rejection, which is the moment it is least replaceable - and
      // the text is kept until the run succeeds, so a failure can be retried.
      this.showChoiceModal = false;
      this.beginRun('choice', CHOICE_HEADINGS[choice], { lyricsMode: choice, choice });
      this.watch(job);
    } catch (err) {
      this.error = this.describe(err);
    } finally {
      this.choiceSubmitting = false;
    }
  }

  /** Re-run a stage and everything after it. */
  async rerun(stage: number): Promise<void> {
    if (!this.songId || this.isRunning || this.syncing) return;
    this.persistTuning();
    try {
      const job = await this.api.rerunStage(this.songId, stage, {
        fusion: this.fusion,
        cleanup: this.cleanup,
        language: this.selectedLanguage === 'auto' ? null : this.selectedLanguage,
        review: true,
      });
      // The lyrics panel, if open, would hide the run's card; the run asks
      // again itself if it pauses for lyrics, and the pasted text is kept.
      this.showChoiceModal = false;
      this.beginRun(stage === 5 ? 'stems' : 'rerun', RERUN_HEADINGS[stage] ?? 'Working on this song');
      this.watch(job);
    } catch (err) {
      this.error = this.describe(err);
    }
  }

  // --- loading -------------------------------------------------------------

  private resetSong(): void {
    // Stop following the old song's run: it carries on on the server - a
    // delete does not cancel it - and its events would otherwise keep
    // re-disabling Add and end in a card for a song that is gone.
    this.stopWatching?.();
    this.stopWatching = null;
    this.audioLoadedFor = '';
    this.songId = '';
    this.lyrics = null;
    this.chords = null;
    this.sheet = null;
    this.stems = null;
    this.showMixer = false;
    this.lyricsText = '';
    this.job = null;
    this.view = 'review';
    this.dirty = false;
    this.endRun();
    this.syncRun = null;
    this.knownTitle = '';
    this.knownArtist = '';
    this.forgetChoice();
  }

  /**
   * Drop everything the lyrics panel was holding.
   *
   * Pasted words belong to the song they were pasted for. Left behind, they
   * are offered to the next song opened - already typed, already valid - and
   * one click sends one song's lyrics to be aligned against another's audio.
   */
  private forgetChoice(): void {
    this.showChoiceModal = false;
    this.choiceReason = '';
    this.lyricsNeedsInput = false;
    this.manualLyrics = '';
  }

  /**
   * Open a song. `keepRun` leaves the finished run's card on screen while the
   * song loads behind it, so the review appears whole rather than filling in.
   */
  async loadSong(songId: string, options: { keepRun?: boolean } = {}): Promise<void> {
    // The song already open, still being worked on - picked from the library
    // to get back to it. Its run is on screen already, with what only it
    // knows: which lyrics answer started it, and the pasted text Try again
    // would need. Starting over would drop both.
    if (songId === this.songId && this.isRunning && !options.keepRun) {
      this.showLibrary = false;
      return;
    }
    if (!options.keepRun) this.endRun();
    this.syncRun = null;
    this.busy = true;
    this.error = '';
    // Another song's run must not be left on screen: `job` drives isRunning,
    // and a stale one hides the workspace behind a progress bar for work that
    // has nothing to do with the song being opened. The run itself carries on
    // in the background either way - only this view of it is dropped, and it
    // is picked up again below if it belongs to the song being opened.
    this.stopWatching?.();
    this.stopWatching = null;
    this.job = null;
    this.forgetChoice();
    try {
      this.songId = songId;
      // Artifacts appear as their stages complete, so a missing one is normal.
      const [detail, lyrics, chords, sheet] = await Promise.all([
        this.api.getSong(songId).catch(() => null),
        this.api.getLyrics(songId).catch(() => null),
        this.api.getChords(songId).catch(() => null),
        this.api.getSheet(songId).catch(() => null),
      ]);
      this.lyrics = lyrics;
      this.chords = chords;
      this.sheet = sheet;
      this.knownTitle = detail ? detail.title : '';
      this.knownArtist = detail ? detail.artist : '';
      this.lyricsText = lyrics ? this.renderLyricsText(lyrics) : '';
      this.dirty = false;
      // A song that has already been synced opens on its sheet; one that has
      // not opens where the work is left to do. A sheet older than the lyrics
      // or chords it was built from is neither: re-running an earlier stage
      // leaves it on disk untouched and still marked done, so opening onto it
      // would show words the song no longer has. Those go back to review,
      // where the sync that would fix them is.
      const stale = this.sheetIsBehind(detail);
      this.view = sheet && !stale ? 'sheet' : 'review';
      this.dirty = stale && !!sheet;
      // Only once there is audio to load: a song still downloading would get
      // a 404 here, and the run's progress loads it when stage 1 is done.
      if (!detail || detail.stages?.['audio'] === 'done') {
        this.audioService.loadTrack(this.api.audioUrl(songId), this.audioLoadedFor !== songId);
        this.audioLoadedFor = songId;
      }
      // The mixer replaces the mix once its four parts are decoded; until
      // then, and on a song without stems, the element carries on playing.
      void this.loadStems(songId);
      this.showLibrary = false;
      // Back to automatic, because the dropdown is about to describe a
      // different song. Carried over, it is sent as an override, and asking
      // for a language the transcript was not made in throws that transcript
      // away and spends minutes making another one.
      this.selectedLanguage = 'auto';

      // This song may still be being worked on. Following that run again is
      // what keeps the buttons that would start a second one disabled, and it
      // is the only way back to its progress and its result.
      if (detail?.job && (detail.job.state === 'queued' || detail.job.state === 'running')) {
        this.beginRun('resume', 'Still working on this song');
        this.watch(detail.job);
        return;
      }

      // A run that stopped to ask for lyrics asks again here. The question
      // outlives the job that raised it - jobs are in memory and forgotten on
      // restart - so it is read back off the manifest instead.
      if (detail?.stages?.['lyrics'] === 'needs_input') {
        this.lyricsNeedsInput = true;
        this.choiceReason =
          detail.manifest?.stages?.['lyrics']?.message || DEFAULT_CHOICE_REASON;
        this.showChoiceModal = true;
      }
    } catch (err) {
      this.error = this.describe(err);
    } finally {
      this.busy = false;
    }
  }

  /**
   * True when the sheet on disk was built before the lyrics or chords it is
   * made of.
   *
   * Stages 2 and 3 can be re-run without stage 4 - that is what the review
   * step is - and nothing marks the sheet stale when they are. It keeps its
   * "done" state and its old words until the next sync.
   */
  private sheetIsBehind(detail: SongDetail | null): boolean {
    const stages = detail?.manifest?.stages;
    const builtAt = stages?.['sheet']?.updated_at;
    if (!builtAt) return false;
    const sources: StageName[] = ['lyrics', 'chords'];
    return sources.some((name) => {
      const source = stages[name]?.updated_at;
      return !!source && source > builtAt;
    });
  }

  /**
   * Open the paste box on purpose, rather than because the run stopped.
   *
   * Lyrics that were found but belong to another recording are no better than
   * lyrics that were never found, and until now the only way to replace them
   * was to re-run the lookup and hope it chose differently.
   */
  openLyricsPaste(): void {
    if (!this.songId || this.isRunning || this.syncing) return;
    // A run already waiting to be told what the lyrics are has nothing to
    // lose, so it just reopens its own question. Only a song that has been
    // through stage 2 successfully has work that pasting would rebuild.
    if (!this.lyricsNeedsInput && (this.lyrics || this.chords)) {
      const edits = this.dirty ? 'Unsaved edits, and corrections' : 'Corrections';
      const warning =
        'Replacing the lyrics rebuilds this song from stage 2.\n\n' +
        `${edits} you have made to the lyrics and to the chords will be ` +
        'replaced, and the sheet will need syncing again.\n\nContinue?';
      if (!window.confirm(warning)) return;
    }
    this.showChoiceModal = true;
  }

  /** Close the paste box without changing anything. */
  dismissChoice(): void {
    this.showChoiceModal = false;
  }

  async refreshLibrary(): Promise<void> {
    try {
      this.library = await this.api.listSongs();
    } catch (err) {
      this.error = this.describe(err);
    }
  }

  async deleteSong(songId: string, event: Event): Promise<void> {
    event.stopPropagation();
    if (!confirm(`Delete "${songId}" and all its files?`)) return;
    try {
      await this.api.deleteSong(songId);
      if (this.songId === songId) this.resetSong();
      await this.refreshLibrary();
    } catch (err) {
      this.error = this.describe(err);
    }
  }

  // --- lyrics editing ------------------------------------------------------

  /** Render the lyrics document as tagged plain text for the editor. */
  private renderLyricsText(document: LyricsDoc): string {
    const headings = new Map(document.sections.map((s) => [s.line_start, s.name]));
    const out: string[] = [];
    for (const line of document.lines) {
      const heading = headings.get(line.index);
      if (heading) {
        if (out.length) out.push('');
        out.push(`[${heading}]`);
      }
      out.push(line.text);
    }
    return out.join('\n');
  }

  /**
   * Write both corrected artifacts, then lay the sheet out from them.
   *
   * This is the end of the review step. Lyrics and chords are saved with the
   * rebuild suppressed so the sheet is generated once, from both, rather than
   * twice - once off corrected lyrics and stale chords, then again.
   */
  async sync(): Promise<void> {
    if (!this.songId || this.syncing || !this.lyrics || !this.chords) return;
    // Words typed into a song that has no timed lines yet - an instrumental
    // the user is correcting - have no original timings to inherit, and
    // parsing them here would stack every line into the opening seconds.
    // Stage 2 is the only thing that can align them to the audio, so the
    // typed text goes through the same door as pasted lyrics.
    if (!this.lyrics.lines.length && this.lyricsText.trim()) {
      const warning =
        'This song has no timed lyrics yet, so the words you typed will be ' +
        'aligned to the audio from stage 2. Chord corrections will be ' +
        're-extracted.\n\nContinue?';
      if (!window.confirm(warning)) return;
      this.manualLyrics = this.lyricsText;
      await this.submitChoice('manual');
      return;
    }
    this.syncing = true;
    this.error = '';
    // Three steps, each ticked off only when its own request returns, so the
    // dialog says truthfully where it is. It also keeps the review from being
    // typed into while the corrections are saved: a word typed then used to
    // miss the sheet and still be marked clean.
    const started = Date.now();
    let steps = syncSteps();
    const show = (outcome: RunOutcome = 'running') => (this.syncRun = { steps, outcome });
    const doing: Record<string, string> = {
      'save-lyrics': 'Saving the lyrics…',
      'save-chords': 'Saving the chords…',
      'stage-4': 'Placing the chords over the words…',
    };
    const begin = (key: string) => {
      steps = steps.map((step) => (step.key === key ? startStep(step, doing[key]) : step));
      show();
    };
    const finish = (key: string, detail: string) => {
      steps = steps.map((step) => (step.key === key ? settleStep(step, 'done', detail) : step));
      show();
    };
    let current = 'save-lyrics';
    show();
    try {
      const lyrics = this.parseLyricsText(this.lyricsText, this.lyrics);
      begin(current);
      await this.api.putLyrics(this.songId, lyrics, false);
      finish(current, `${lyrics.lines.length} lines saved`);

      current = 'save-chords';
      begin(current);
      await this.api.putChords(this.songId, this.chords, false);
      finish(current, `${this.chords.bars.length} bars saved`);
      this.lyrics = lyrics;

      current = 'stage-4';
      begin(current);
      const sheet = await this.api.syncSheet(this.songId);
      finish(current, sheetSummary(sheet));
      this.sheet = sheet;
      this.dirty = false;

      show('done');
      await delay(Math.max(SYNC_DONE_BEAT_MS, SYNC_MIN_MS - (Date.now() - started)));
      if (!this.syncRun) return; // closed, or another song opened, meanwhile
      this.syncRun = null;
      this.view = 'sheet';
      this.flash('Song sheet generated.');
      await this.refreshLibrary();
    } catch (err) {
      // Shown on the step that failed, so not in the alert as well.
      steps = steps.map((step) =>
        step.key === current ? settleStep(step, 'failed', '', this.describe(err)) : step
      );
      show('failed');
    } finally {
      this.syncing = false;
    }
  }

  /** Close a failed Sync; the review and every edit in it are untouched. */
  closeSync(): void {
    this.syncRun = null;
  }

  /** Go back to the editor without touching what is already on disk. */
  backToReview(): void {
    this.view = 'review';
  }

  private parseLyricsText(text: string, base: LyricsDoc): LyricsDoc {
    const sectionTag = /^\[([^\]]+)\]$/;
    const lines: LyricsDoc['lines'] = [];
    const sections: LyricsDoc['sections'] = [];
    let pendingName: string | null = null;

    for (const raw of text.split('\n')) {
      const trimmed = raw.trim();
      if (!trimmed) continue;

      const tag = trimmed.match(sectionTag);
      if (tag) {
        pendingName = tag[1].trim();
        continue;
      }

      const index = lines.length;
      // Reuse the original line's timings where the position still exists.
      const original = base.lines[index];
      lines.push({
        index,
        section_index: sections.length ? sections.length - 1 : null,
        time: original?.time ?? (lines[index - 1]?.end ?? 0),
        end: original?.end ?? (lines[index - 1]?.end ?? 0) + 3,
        text: trimmed,
        // Word timings belong to the old text; drop them when it changed.
        words: original && original.text === trimmed ? original.words : [],
      });

      if (pendingName !== null) {
        sections.push({
          index: sections.length,
          name: pendingName,
          kind: base.sections.find((s) => s.name === pendingName)?.kind ?? 'other',
          line_start: index,
          line_end: index,
        });
        lines[index].section_index = sections.length - 1;
        pendingName = null;
      } else if (sections.length) {
        sections[sections.length - 1].line_end = index;
      }
    }

    return { ...base, sections, lines };
  }

  // --- chord editing -------------------------------------------------------

  /**
   * Hold a chord edit in memory.
   *
   * Corrections are not written as they are typed: the point of the review
   * step is to fix everything first and generate the sheet once, on sync.
   */
  onChordsChanged(document: ChordsDoc): void {
    this.chords = document;
    this.dirty = true;
  }

  // --- sheet editing -------------------------------------------------------

  async onSheetBlockChanged(change: { index: number; block: SheetBlock }): Promise<void> {
    if (!this.sheet || !this.songId) return;
    const blocks = [...this.sheet.blocks];
    blocks[change.index] = change.block;
    this.sheet = { ...this.sheet, blocks };
    try {
      await this.api.putSheet(this.songId, this.sheet);
    } catch (err) {
      this.error = this.describe(err);
    }
  }

  async exportToSongbook(): Promise<void> {
    if (!this.songId) return;
    this.isExporting = true;
    try {
      const result = await this.api.exportToSongbook(this.songId);
      this.flash(`Exported to ${result.filename}`);
    } catch (err) {
      this.error = this.describe(err);
    } finally {
      this.isExporting = false;
    }
  }

  // --- settings ------------------------------------------------------------

  async saveSettings(): Promise<void> {
    this.settingsMessage = 'Saving...';
    this.persistTuning();
    try {
      this.config = await this.api.saveConfig(this.config);
      this.settingsMessage = 'Saved.';
      setTimeout(() => {
        this.showSettingsModal = false;
        this.settingsMessage = '';
      }, 1200);
    } catch (err) {
      this.settingsMessage = this.describe(err);
    }
  }

  resetTuning(): void {
    this.fusion = structuredClone(DEFAULT_FUSION);
    this.cleanup = structuredClone(DEFAULT_CLEANUP);
    this.persistTuning();
  }

  // --- stems ---------------------------------------------------------------

  /**
   * Load a song's stems into the player, when it has usable ones.
   *
   * A missing document means separation has not run for this song; one with
   * `unavailable` set means it cannot, and says why - both leave the mix
   * playing, which is what the player did before any of this existed.
   */
  private async loadStems(songId: string): Promise<void> {
    this.stems = await this.api.getStems(songId).catch(() => null);
    if (!this.stems || this.stems.unavailable || !this.stems.stems.length) return;

    const available = new Set(this.stems.stems.map((stem) => stem.name));
    const urls: Partial<Record<StemName, string>> = {};
    for (const name of STEM_NAMES) {
      if (available.has(name)) urls[name] = this.api.stemUrl(songId, name);
    }

    this.restoreMixerState(songId);
    await this.audioService.loadStems(urls);
  }

  private restoreMixerState(songId: string): void {
    try {
      const stored = localStorage.getItem(MIXER_STORAGE_PREFIX + songId);
      this.audioService.restoreMixerState(stored ? JSON.parse(stored) : {});
    } catch {
      this.audioService.restoreMixerState({});
    }
  }

  /** Whether a run is separating this song right now. */
  get isSeparating(): boolean {
    return !!this.job && this.job.stage === 5 && !this.jobIsFinished;
  }

  private get jobIsFinished(): boolean {
    return (
      !this.job ||
      this.job.state === 'done' ||
      this.job.state === 'failed' ||
      this.job.state === 'needs_input'
    );
  }

  /**
   * Separate this song, then re-time the lyrics against the isolated vocal.
   *
   * Cascading is the backend's own doing: stage 5 runs before stage 2 in the
   * running order, so asking for it carries on through the transcription that
   * reads what it produced.
   */
  async separateStems(): Promise<void> {
    if (!this.songId || this.isSeparating || this.isRunning) return;
    this.error = '';
    try {
      const job = await this.api.rerunStage(this.songId, 5, {
        fusion: this.fusion,
        cleanup: this.cleanup,
        review: true,
      });
      this.showMixer = false;
      this.showChoiceModal = false; // as in rerun()
      this.beginRun('stems', RERUN_HEADINGS[5]);
      this.watch(job);
    } catch (err) {
      this.error = this.describe(err);
    }
  }

  // --- playback ------------------------------------------------------------

  togglePlayPause(): void {
    this.audioService.togglePlay();
  }

  handleSeek(time: number): void {
    this.audioService.seek(time);
  }

  formatTime(seconds: number): string {
    if (!seconds || isNaN(seconds)) return '00:00.0';
    const minutes = Math.floor(seconds / 60);
    const rest = Math.floor(seconds % 60);
    const tenths = Math.floor((seconds % 1) * 10);
    return `${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}.${tenths}`;
  }

  handlePlayBarMouseMove(event: MouseEvent): void {
    const container = event.currentTarget as HTMLElement;
    if (!container) return;
    const rect = container.getBoundingClientRect();
    this.hoverLeft = event.clientX - rect.left;
    const ratio = Math.max(0, Math.min(1, this.hoverLeft / rect.width));
    this.hoverTimeText = this.formatTime(ratio * (this.audioService.duration() || 0));
  }
}
