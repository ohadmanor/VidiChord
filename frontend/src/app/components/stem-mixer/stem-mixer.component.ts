import { CommonModule } from '@angular/common';
import { Component, EventEmitter, Input, Output } from '@angular/core';
import { STEM_NAMES, StemName } from '../../models/artifacts';
import { AudioService } from '../../services/audio.service';

/**
 * Four faders over the separated parts of the song.
 *
 * The panel is also where separation explains itself: a song that has never
 * been separated offers to be, and one on a machine without Demucs says so
 * rather than silently offering nothing.
 */
@Component({
  selector: 'app-stem-mixer',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './stem-mixer.component.html',
  styleUrls: ['./stem-mixer.component.css'],
})
export class StemMixerComponent {
  /** Why there are no stems, when there are none worth mixing. */
  @Input() unavailable = '';
  /** True while a separation job is running for this song. */
  @Input() separating = false;
  @Output() separate = new EventEmitter<void>();

  readonly stems = STEM_NAMES;

  /** What each fader is called, since "other" says nothing on its own. */
  readonly labels: Record<StemName, string> = {
    vocals: 'Vocals',
    drums: 'Drums',
    bass: 'Bass',
    other: 'Other',
  };

  readonly hints: Record<StemName, string> = {
    vocals: 'The singing',
    drums: 'Kit and percussion',
    bass: 'Bass guitar, synth bass',
    other: 'Guitars, keys, strings - everything else',
  };

  constructor(public audio: AudioService) {}

  isMuted(name: StemName): boolean {
    return this.audio.muted().includes(name);
  }

  isSoloed(name: StemName): boolean {
    return this.audio.soloed().includes(name);
  }

  /** Dim a fader that is being silenced by somebody else's solo. */
  isSilenced(name: StemName): boolean {
    return this.audio.effectiveLevel(name) === 0;
  }

  percent(name: StemName): number {
    return Math.round(this.audio.levels()[name] * 100);
  }

  onLevel(name: StemName, event: Event): void {
    this.audio.setLevel(name, +(event.target as HTMLInputElement).value);
  }
}
