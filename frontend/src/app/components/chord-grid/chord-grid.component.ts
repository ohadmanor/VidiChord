import { CommonModule } from '@angular/common';
import { Component, EventEmitter, Input, Output, computed, signal } from '@angular/core';
import { Bar, Beat, ChordsDoc, NO_CHORD } from '../../models/artifacts';

/**
 * Editable bar-by-bar view of the chords from stage 3.
 *
 * Bars come from the backend's beat grid, so this component never re-groups or
 * shifts them - it only displays what stage 3 decided and lets the user
 * correct individual chords.
 */
@Component({
  selector: 'app-chord-grid',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './chord-grid.component.html',
  styleUrls: ['./chord-grid.component.css'],
})
export class ChordGridComponent {
  @Input({ required: true }) set chords(value: ChordsDoc | null) {
    this._chords.set(value);
  }
  get chords(): ChordsDoc | null {
    return this._chords();
  }

  @Input() set currentTime(value: number) {
    this._currentTime.set(value);
  }

  /** Emitted with the whole document whenever a chord is edited. */
  @Output() chordsChanged = new EventEmitter<ChordsDoc>();
  @Output() seek = new EventEmitter<number>();

  private readonly _chords = signal<ChordsDoc | null>(null);
  private readonly _currentTime = signal(0);

  readonly bars = computed(() => this._chords()?.bars ?? []);

  readonly activeBarIndex = computed(() => {
    const time = this._currentTime();
    const bars = this.bars();
    for (let i = bars.length - 1; i >= 0; i--) {
      if (bars[i].start <= time) return i;
    }
    return -1;
  });

  /** True when the beat repeats the chord before it, so it can be dimmed. */
  isRepeat(bar: Bar, position: number): boolean {
    const beat = bar.beats[position];
    if (beat.chord === NO_CHORD) return false;
    if (position > 0) return bar.beats[position - 1].chord === beat.chord;

    const bars = this.bars();
    const barPosition = bars.indexOf(bar);
    const previous = bars[barPosition - 1];
    const last = previous?.beats[previous.beats.length - 1];
    return last?.chord === beat.chord;
  }

  display(beat: Beat, repeated: boolean): string {
    if (beat.chord === NO_CHORD) return '-';
    return repeated ? `(${beat.chord})` : beat.chord;
  }

  timestamp(seconds: number): string {
    const minutes = Math.floor(seconds / 60);
    const rest = Math.floor(seconds % 60);
    const tenths = Math.floor((seconds % 1) * 10);
    return `${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}.${tenths}`;
  }

  onChordEdit(bar: Bar, beat: Beat, event: Event): void {
    const element = event.target as HTMLElement;
    let text = (element.innerText ?? '').replace(/[\n\r]/g, '').trim();
    // Strip the parentheses the repeat display adds.
    text = text.replace(/^\(|\)$/g, '').trim();
    if (text === '-' || text === '') text = NO_CHORD;

    const document = this._chords();
    if (!document || text === beat.chord) {
      element.innerText = this.display(beat, this.isRepeat(bar, bar.beats.indexOf(beat)));
      return;
    }

    beat.chord = text;
    this.chordsChanged.emit(document);
  }

  /**
   * Halve or double the beat resolution.
   *
   * Beat trackers routinely lock onto twice or half the intended tempo. Rather
   * than re-running the whole stage, the existing beats are split or merged and
   * bars renumbered, which is instant and keeps the chords already corrected.
   */
  changeResolution(factor: 2 | 0.5): void {
    const document = this._chords();
    if (!document) return;

    const flat = document.bars.flatMap((bar) => bar.beats);
    if (flat.length < 2) return;

    let rebuilt: Beat[];
    if (factor === 2) {
      rebuilt = flat.flatMap((beat) => {
        const middle = (beat.start + beat.end) / 2;
        return [
          { ...beat, end: middle },
          { ...beat, start: middle },
        ];
      });
    } else {
      rebuilt = [];
      for (let i = 0; i < flat.length; i += 2) {
        const first = flat[i];
        const second = flat[i + 1];
        rebuilt.push({
          ...first,
          end: second ? second.end : first.end,
          chord: first.chord !== NO_CHORD ? first.chord : second?.chord ?? NO_CHORD,
        });
      }
    }

    const meter = document.time_signature || 4;
    document.bpm = Math.round(document.bpm * factor * 100) / 100;
    document.bars = [];

    rebuilt.forEach((beat, index) => {
      beat.index = index;
      beat.beat_in_bar = (index % meter) + 1;
      if (beat.beat_in_bar === 1) {
        document.bars.push({
          index: document.bars.length + 1,
          start: beat.start,
          end: beat.end,
          beats: [],
        });
      }
      const bar = document.bars[document.bars.length - 1];
      if (bar) {
        bar.beats.push(beat);
        bar.end = beat.end;
      }
    });

    this._chords.set({ ...document });
    this.chordsChanged.emit(document);
  }

  trackByBar(_index: number, bar: Bar): number {
    return bar.index;
  }
}
