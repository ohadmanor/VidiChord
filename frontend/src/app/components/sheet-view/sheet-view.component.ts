import { CommonModule } from '@angular/common';
import {
  Component,
  EventEmitter,
  Input,
  Output,
  computed,
  signal,
} from '@angular/core';
import {
  InstrumentalBlock,
  LyricBlock,
  SectionBlock,
  SheetBlock,
  SheetDoc,
} from '../../models/artifacts';

/**
 * Renders a `SheetDoc` produced by stage 4.
 *
 * The component only displays and edits blocks - it never computes chord
 * positions. Alignment is carried in the spacing of `chord_line`, which the
 * backend built, so the chord row must stay `dir="ltr"` and `white-space: pre`
 * even for a right-to-left song.
 */
@Component({
  selector: 'app-sheet-view',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './sheet-view.component.html',
  styleUrls: ['./sheet-view.component.css'],
})
export class SheetViewComponent {
  @Input({ required: true }) set sheet(value: SheetDoc | null) {
    this._sheet.set(value);
  }
  get sheet(): SheetDoc | null {
    return this._sheet();
  }

  /** Playback position, used to highlight the line currently sounding. */
  @Input() set currentTime(value: number) {
    this._currentTime.set(value);
  }

  @Input() editable = false;

  /** Emitted when a lyric line or chord row is edited. */
  @Output() blockChanged = new EventEmitter<{ index: number; block: SheetBlock }>();

  /** Emitted when the user clicks a block, to seek playback to it. */
  @Output() seek = new EventEmitter<number>();

  private readonly _sheet = signal<SheetDoc | null>(null);
  private readonly _currentTime = signal(0);

  readonly blocks = computed(() => this._sheet()?.blocks ?? []);
  readonly isRtl = computed(() => this._sheet()?.is_rtl ?? false);

  // The heading songbook prints above every sheet: title, artist, and the key
  // it is in. Computed rather than read through `?.` chains in the template,
  // which Angular 18 miscompiles into an undeclared temporary.
  readonly title = computed(() => this._sheet()?.title ?? '');
  readonly artist = computed(() => this._sheet()?.artist ?? '');
  readonly key = computed(() => this._sheet()?.key ?? '');
  readonly hasHeading = computed(() => !!(this.title() || this.artist()));

  /** Index of the block currently sounding, or -1. */
  readonly activeIndex = computed(() => {
    const time = this._currentTime();
    const blocks = this.blocks();
    let active = -1;
    for (let i = 0; i < blocks.length; i++) {
      const start = (blocks[i] as any).start ?? 0;
      if (start <= time) {
        active = i;
      } else {
        break;
      }
    }
    return active;
  });

  isSection(block: SheetBlock): block is SectionBlock {
    return block.type === 'section';
  }

  isInstrumental(block: SheetBlock): block is InstrumentalBlock {
    return block.type === 'instrumental';
  }

  isLyric(block: SheetBlock): block is LyricBlock {
    return block.type === 'lyric';
  }

  onLyricEdit(index: number, block: LyricBlock, event: Event): void {
    const text = ((event.target as HTMLElement).innerText ?? '')
      .replace(/\n/g, '')
      .trim();
    if (text === block.text) return;
    this.blockChanged.emit({ index, block: { ...block, text } });
  }

  onChordEdit(index: number, block: LyricBlock, event: Event): void {
    // Trailing spaces carry the alignment, so only the line break is stripped.
    const chordLine = ((event.target as HTMLElement).innerText ?? '').replace(/\n/g, '');
    if (chordLine === block.chord_line) return;
    this.blockChanged.emit({ index, block: { ...block, chord_line: chordLine } });
  }

  onInstrumentalEdit(index: number, block: InstrumentalBlock, event: Event): void {
    const text = ((event.target as HTMLElement).innerText ?? '').trim();
    if (text === block.text) return;
    this.blockChanged.emit({ index, block: { ...block, text } });
  }

  blockStart(block: SheetBlock): number {
    return (block as any).start ?? 0;
  }

  trackByIndex(index: number): number {
    return index;
  }
}
