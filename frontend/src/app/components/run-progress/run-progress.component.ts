import { CommonModule } from '@angular/common';
import { Component, EventEmitter, Input, OnChanges, OnDestroy, Output } from '@angular/core';
import { ProgressStep, RunOutcome, StepState, formatClock } from './run-progress.model';

const STATE_WORDS: Record<StepState, string> = {
  pending: 'Not started',
  running: 'In progress',
  done: 'Done',
  skipped: 'Skipped',
  failed: 'Stopped',
  needs_input: 'Needs your answer',
};

/**
 * One run, step by step: what is done, what is running and how far it has
 * got, and what comes next.
 *
 * Used for adding a song, for every re-run, and for Sync. Its status marks
 * are inline SVG and CSS rather than the icon library: an icon that throws
 * while rendering once left this whole screen blank.
 */
@Component({
  selector: 'app-run-progress',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './run-progress.component.html',
  styleUrls: ['./run-progress.component.css'],
})
export class RunProgressComponent implements OnChanges, OnDestroy {
  @Input() steps: ProgressStep[] = [];
  @Input() heading = 'Working…';
  @Input() doneHeading = 'Done';
  @Input() songTitle = '';
  @Input() songArtist = '';
  @Input() outcome: RunOutcome = 'running';
  /** What happens once the run is done, shown under the steps. */
  @Input() nextText = '';
  @Input() retryLabel = 'Try again';
  @Input() dismissLabel = 'Close';
  @Output() retry = new EventEmitter<void>();
  @Output() dismiss = new EventEmitter<void>();

  /** The clock the timers read, ticked every second while running, so they
   *  move even while the backend has nothing new to say. */
  now = Date.now();
  private timer: ReturnType<typeof setInterval> | null = null;

  ngOnChanges(): void {
    this.now = Date.now();
    if (this.outcome === 'running') {
      if (!this.timer) this.timer = setInterval(() => (this.now = Date.now()), 1000);
    } else {
      this.stopTimer();
    }
  }

  ngOnDestroy(): void {
    this.stopTimer();
  }

  private stopTimer(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  /** Every snapshot builds new step objects; tracking by key keeps the rows,
   *  so bars animate rather than jump and spinners do not restart. */
  readonly trackByKey = (_: number, step: ProgressStep) => step.key;

  private get focus(): number {
    return this.steps.findIndex(
      (step) => step.state === 'running' || step.state === 'failed' || step.state === 'needs_input'
    );
  }

  get position(): number {
    if (this.outcome === 'done') return this.steps.length;
    const at = this.focus;
    if (at >= 0) return at + 1;
    const finished = this.steps.filter((s) => s.state === 'done' || s.state === 'skipped').length;
    return Math.max(1, Math.min(this.steps.length, finished + 1));
  }

  /** The finished rows' durations plus the running row's live time. */
  private get totalMs(): number {
    return this.steps.reduce(
      (total, step) =>
        total +
        (step.state === 'running'
          ? step.startedAt === null ? 0 : Math.max(0, this.now - step.startedAt)
          : step.durationMs ?? 0),
      0
    );
  }

  get headline(): string {
    if (this.outcome === 'done') return this.doneHeading;
    if (this.outcome === 'needs_input') return 'Waiting for your answer';
    if (this.outcome === 'failed') {
      const failed = this.steps.find((step) => step.state === 'failed');
      return failed ? `Stopped at “${failed.title}”` : 'This run stopped';
    }
    return this.heading;
  }

  get metaLine(): string {
    const count = this.steps.length;
    const total = formatClock(this.totalMs);
    switch (this.outcome) {
      case 'done':
        // "done in 0:00" reads as nothing having happened.
        return this.totalMs < 1000 ? `All ${count} steps done` : `All ${count} steps done in ${total}`;
      case 'failed':
        return `Stopped at step ${this.position} of ${count} · ${total}`;
      case 'needs_input':
        return `Paused at step ${this.position} of ${count}: your answer is needed`;
      default:
        return this.totalMs > 0 ? `Step ${this.position} of ${count} · ${total} elapsed` : 'Getting ready…';
    }
  }

  /** For screen readers: step changes only, not every download tick. */
  get announcement(): string {
    const running = this.steps.find((step) => step.state === 'running');
    return running && this.outcome === 'running'
      ? `Step ${this.position} of ${this.steps.length}: ${running.title}`
      : this.headline;
  }

  sideText(step: ProgressStep, index: number): string {
    switch (step.state) {
      case 'running':
        return step.startedAt === null ? '' : formatClock(this.now - step.startedAt);
      case 'done':
        return step.durationMs !== null && step.durationMs >= 1000
          ? `Done · ${formatClock(step.durationMs)}`
          : 'Done';
      case 'skipped':
        return 'Skipped';
      case 'failed':
        return 'Stopped';
      case 'needs_input':
        return 'Needs your answer';
      default: {
        const upNext =
          this.outcome === 'running' && index === this.steps.findIndex((s) => s.state === 'pending');
        if (!upNext) return step.typicalShort;
        return step.typicalShort ? `Up next · ${step.typicalShort}` : 'Up next';
      }
    }
  }

  stateWord(step: ProgressStep): string {
    return STATE_WORDS[step.state];
  }

  /**
   * The bar's value, or null for "unknown" - a sliding bar and no number.
   * Zero counts as unknown too, because an empty bar reads as stuck; and the
   * bar stops at 99 until the step is actually done.
   */
  barValue(step: ProgressStep): number | null {
    if (step.percent === null || step.percent <= 0) return null;
    return Math.min(99, Math.max(1, Math.round(step.percent)));
  }

  isSlow(step: ProgressStep): boolean {
    return (
      step.state === 'running' &&
      step.startedAt !== null &&
      (this.now - step.startedAt) / 1000 > step.slowAfter
    );
  }
}
