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
  LucideRefreshCw,
  LucideSettings,
  LucideVolume2,
  LucideX,
} from '@lucide/angular';
import { ChordGridComponent } from './components/chord-grid/chord-grid.component';
import { SheetViewComponent } from './components/sheet-view/sheet-view.component';
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
  StageName,
} from './models/artifacts';
import { ApiService } from './services/api.service';
import { AudioService } from './services/audio.service';

const FUSION_STORAGE_KEY = 'vidiChordFusionConfig';
const CLEANUP_STORAGE_KEY = 'vidiChordCleanupConfig';

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
    LucideMusic,
    LucideLayers,
    LucideFileText,
    LucidePlay,
    LucidePause,
    LucideVolume2,
    LucideAlertCircle,
    LucideRefreshCw,
    LucideSettings,
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

  // --- run state -----------------------------------------------------------
  youtubeUrl = '';
  job: Job | null = null;
  error = '';
  success = '';
  busy = false;

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
    for (const [key, target] of [
      [FUSION_STORAGE_KEY, 'fusion'],
      [CLEANUP_STORAGE_KEY, 'cleanup'],
    ] as const) {
      const stored = localStorage.getItem(key);
      if (!stored) continue;
      try {
        // Merge over the defaults so a stored config from an older version
        // does not leave new fields undefined.
        (this as any)[target] = { ...(this as any)[target], ...JSON.parse(stored) };
      } catch {
        localStorage.removeItem(key);
      }
    }
  }

  private persistTuning(): void {
    localStorage.setItem(FUSION_STORAGE_KEY, JSON.stringify(this.fusion));
    localStorage.setItem(CLEANUP_STORAGE_KEY, JSON.stringify(this.cleanup));
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

  get progressPercent(): number {
    return this.job?.percent ?? 0;
  }

  // --- starting and following a run ---------------------------------------

  async startFromYoutube(): Promise<void> {
    const url = this.youtubeUrl.trim();
    if (!url || this.isRunning) return;

    this.error = '';
    this.success = '';
    this.resetSong();
    this.persistTuning();

    try {
      const created = await this.api.createFromYoutube(url, {
        language: this.selectedLanguage === 'auto' ? null : this.selectedLanguage,
        fusion: this.fusion,
        cleanup: this.cleanup,
        review: true,
      });
      this.songId = created.song_id;
      this.watch(created.job);
    } catch (err) {
      this.error = this.describe(err);
    }
  }

  private watch(job: Job): void {
    this.job = job;
    this.stopWatching?.();
    this.stopWatching = this.api.watchJob(
      job.job_id,
      (update) => {
        this.job = update;
        // Load the audio as soon as stage 1 has produced it.
        if (update.stage >= 2 && !this.audioService.currentTrackPath()) {
          this.audioService.loadTrack(this.api.audioUrl(this.songId));
        }
      },
      (final) => this.onJobFinished(final)
    );
  }

  private async onJobFinished(job: Job): Promise<void> {
    if (job.state === 'needs_input') {
      this.lyricsNeedsInput = true;
      this.choiceReason = job.message || DEFAULT_CHOICE_REASON;
      this.showChoiceModal = true;
      return;
    }
    if (job.state === 'failed') {
      this.error = job.error || 'The run failed.';
      // Whatever was pasted is kept: the run failing is exactly when the user
      // needs another go at it, and retyping the lyrics is the one part of
      // that they cannot get back. The reload below is what drops it, and
      // that only happens once the run has worked.
      return;
    }
    await this.loadSong(this.songId);
    // The run deliberately stopped before the sheet, so show the editor even
    // when an earlier sheet is still on disk from a previous sync.
    this.view = 'review';
    this.flash('Lyrics and chords are ready to review.');
    await this.refreshLibrary();
  }

  async submitChoice(choice: 'ai' | 'manual' | 'instrumental'): Promise<void> {
    if (!this.songId || this.choiceSubmitting) return;
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
      this.watch(job);
    } catch (err) {
      this.error = this.describe(err);
    }
  }

  // --- loading -------------------------------------------------------------

  private resetSong(): void {
    this.songId = '';
    this.lyrics = null;
    this.chords = null;
    this.sheet = null;
    this.lyricsText = '';
    this.job = null;
    this.view = 'review';
    this.dirty = false;
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

  async loadSong(songId: string): Promise<void> {
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
      this.audioService.loadTrack(this.api.audioUrl(songId));
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
    try {
      const lyrics = this.parseLyricsText(this.lyricsText, this.lyrics);
      await this.api.putLyrics(this.songId, lyrics, false);
      await this.api.putChords(this.songId, this.chords, false);
      this.lyrics = lyrics;

      this.sheet = await this.api.syncSheet(this.songId);
      this.dirty = false;
      this.view = 'sheet';
      this.flash('Song sheet generated.');
      await this.refreshLibrary();
    } catch (err) {
      this.error = this.describe(err);
    } finally {
      this.syncing = false;
    }
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
