import { Injectable, signal, OnDestroy } from '@angular/core';
import { STEM_NAMES, StemName } from '../models/artifacts';

/**
 * Playback, in one of two modes.
 *
 * Without stems it is a plain `<audio>` element streaming the mix, which is
 * what it has always been. With them it is a Web Audio graph - one decoded
 * buffer per stem, each through its own gain - so the four parts can be
 * balanced against each other while the song plays.
 *
 * Four `<audio>` elements would have been far less work, and they drift:
 * nothing keeps their clocks together, and a few tens of milliseconds between
 * the bass and the drums is exactly what a musician hears first. Buffers
 * started against one audio clock cannot drift at all.
 *
 * The public surface is the same in both modes, so everything that plays,
 * seeks or reads the time is unaware of which one is running.
 */

/** Decode at this rate rather than 44.1 kHz: half the memory, no audible cost. */
const MIXER_SAMPLE_RATE = 32000;

/** How the faders sit for each named preset. */
const PRESETS: Record<string, Partial<Record<StemName, number>>> = {
  'Full mix': { vocals: 1, drums: 1, bass: 1, other: 1 },
  'No vocals': { vocals: 0, drums: 1, bass: 1, other: 1 },
  'Vocals only': { vocals: 1, drums: 0, bass: 0, other: 0 },
  'Rhythm section': { vocals: 0, drums: 1, bass: 1, other: 0 },
};

export type MixerLevels = Record<StemName, number>;

function fullMix(): MixerLevels {
  return { vocals: 1, drums: 1, bass: 1, other: 1 };
}

@Injectable({
  providedIn: 'root'
})
export class AudioService implements OnDestroy {
  private audio = new Audio();

  // Expose state as Signals
  public isPlaying = signal<boolean>(false);
  public currentTime = signal<number>(0);
  public duration = signal<number>(0);
  public volume = signal<number>(0.8);
  public currentTrackPath = signal<string>('');

  /** True once the stems are decoded and the faders are live. */
  public mixing = signal<boolean>(false);
  /** True while the stems are being fetched and decoded. */
  public loadingStems = signal<boolean>(false);
  public stemError = signal<string>('');
  public levels = signal<MixerLevels>(fullMix());
  public muted = signal<StemName[]>([]);
  public soloed = signal<StemName[]>([]);

  private listeners: { name: string; handler: any }[] = [];

  // --- the mixer graph -----------------------------------------------------
  private context: AudioContext | null = null;
  private master: GainNode | null = null;
  private buffers = new Map<StemName, AudioBuffer>();
  private gains = new Map<StemName, GainNode>();
  private sources = new Map<StemName, AudioBufferSourceNode>();
  /** Context time playback started at, and the track position it started from. */
  private startedAt = 0;
  private startOffset = 0;
  private frame = 0;
  /** Bumped on every load, so a slow decode cannot land on a later song. */
  private loadToken = 0;

  constructor() {
    this.audio.volume = this.volume();

    // Register audio event listeners
    this.addListener('timeupdate', () => {
      if (!this.mixing()) this.currentTime.set(this.audio.currentTime);
    });

    this.addListener('loadedmetadata', () => {
      if (!this.mixing()) this.duration.set(this.audio.duration);
    });

    this.addListener('ended', () => {
      this.isPlaying.set(false);
      this.currentTime.set(0);
    });

    this.addListener('pause', () => {
      if (!this.mixing()) this.isPlaying.set(false);
    });

    this.addListener('play', () => {
      if (!this.mixing()) this.isPlaying.set(true);
    });
  }

  private addListener(event: string, handler: any) {
    this.audio.addEventListener(event, handler);
    this.listeners.push({ name: event, handler });
  }

  /**
   * Point the player at a fully-formed audio URL.
   *
   * `force` loads it again even when it is the URL already set: one asked
   * for before the audio existed got a 404, and asking again once it does is
   * the only way the player recovers.
   */
  public loadTrack(url: string, force = false) {
    if (!url || (!force && url === this.currentTrackPath())) return;
    this.teardownMixer();
    this.currentTrackPath.set(url);
    this.audio.src = url;
    this.audio.load();
    this.isPlaying.set(false);
    this.currentTime.set(0);
    this.duration.set(0);
  }

  public play() {
    if (this.mixing()) {
      this.startSources(this.currentTime());
      return;
    }
    if (!this.audio.src) return;
    this.audio.play().then(() => {
      this.isPlaying.set(true);
    }).catch(err => {
      console.error('Audio playback error:', err);
    });
  }

  public pause() {
    if (this.mixing()) {
      const at = this.positionNow();
      this.stopSources();
      this.currentTime.set(at);
      this.isPlaying.set(false);
      return;
    }
    this.audio.pause();
    this.isPlaying.set(false);
  }

  public togglePlay() {
    if (this.isPlaying()) {
      this.pause();
    } else {
      this.play();
    }
  }

  public seek(time: number) {
    if (isNaN(time) || time < 0) return;
    if (this.mixing()) {
      const target = Math.min(time, this.duration());
      const wasPlaying = this.isPlaying();
      this.stopSources();
      this.currentTime.set(target);
      if (wasPlaying) this.startSources(target);
      return;
    }
    this.audio.currentTime = time;
    this.currentTime.set(time);
  }

  public seekRelative(offset: number) {
    this.seek((this.mixing() ? this.currentTime() : this.audio.currentTime) + offset);
  }

  public setVolume(vol: number) {
    const safeVol = Math.max(0, Math.min(1, vol));
    this.audio.volume = safeVol;
    if (this.master) this.master.gain.value = safeVol;
    this.volume.set(safeVol);
  }

  // --- the mixer -----------------------------------------------------------

  /**
   * Fetch and decode a song's stems, then play them instead of the mix.
   *
   * Everything is decoded up front because that is what makes the four parts
   * inseparable in time. It costs a few hundred megabytes for a five-minute
   * song, which is why the buffers are dropped the moment another song is
   * opened.
   */
  public async loadStems(urls: Partial<Record<StemName, string>>): Promise<void> {
    const wanted = STEM_NAMES.filter((name) => !!urls[name]);
    if (!wanted.length) return;

    const token = ++this.loadToken;
    this.loadingStems.set(true);
    this.stemError.set('');

    try {
      const context = this.ensureContext();
      const decoded = await Promise.all(
        wanted.map(async (name) => {
          const response = await fetch(urls[name] as string);
          if (!response.ok) {
            throw new Error(`${name}: ${response.status} ${response.statusText}`);
          }
          return [name, await context.decodeAudioData(await response.arrayBuffer())] as const;
        })
      );

      // Another song was opened while this one was decoding.
      if (token !== this.loadToken) return;

      // Carry the position over, so turning the mixer on mid-song does not
      // send the listener back to the start.
      const wasPlaying = this.isPlaying();
      const resumeFrom = this.mixing() ? this.currentTime() : this.audio.currentTime;

      // Whatever was playing has to stop before its nodes are replaced -
      // re-opening a song that is already mixing would otherwise leave the
      // old sources running and play it twice.
      this.stopSources();
      for (const gain of this.gains.values()) gain.disconnect();
      this.buffers.clear();
      this.gains.clear();
      let longest = 0;
      for (const [name, buffer] of decoded) {
        this.buffers.set(name, buffer);
        const gain = context.createGain();
        gain.connect(this.master as GainNode);
        this.gains.set(name, gain);
        longest = Math.max(longest, buffer.duration);
      }

      const resumeAt = Math.min(resumeFrom || 0, longest);
      this.audio.pause();

      this.mixing.set(true);
      this.duration.set(longest);
      this.currentTime.set(resumeAt);
      this.applyGains();
      if (wasPlaying) this.startSources(resumeAt);
    } catch (error) {
      if (token === this.loadToken) {
        this.stemError.set(error instanceof Error ? error.message : String(error));
        this.mixing.set(false);
      }
    } finally {
      if (token === this.loadToken) this.loadingStems.set(false);
    }
  }

  public setLevel(name: StemName, value: number): void {
    const level = Math.max(0, Math.min(1, value));
    this.levels.set({ ...this.levels(), [name]: level });
    this.applyGains();
  }

  public toggleMute(name: StemName): void {
    const current = this.muted();
    this.muted.set(
      current.includes(name) ? current.filter((n) => n !== name) : [...current, name]
    );
    this.applyGains();
  }

  public toggleSolo(name: StemName): void {
    const current = this.soloed();
    this.soloed.set(
      current.includes(name) ? current.filter((n) => n !== name) : [...current, name]
    );
    this.applyGains();
  }

  public presetNames(): string[] {
    return Object.keys(PRESETS);
  }

  /** Apply a named balance, clearing whatever mutes and solos were set. */
  public applyPreset(name: string): void {
    const preset = PRESETS[name];
    if (!preset) return;
    this.levels.set({ ...fullMix(), ...preset });
    this.muted.set([]);
    this.soloed.set([]);
    this.applyGains();
  }

  /** The faders as they stand, for storing against a song. */
  public mixerState(): { levels: MixerLevels; muted: StemName[]; soloed: StemName[] } {
    return { levels: this.levels(), muted: this.muted(), soloed: this.soloed() };
  }

  public restoreMixerState(state: {
    levels?: Partial<MixerLevels>;
    muted?: StemName[];
    soloed?: StemName[];
  }): void {
    this.levels.set({ ...fullMix(), ...(state.levels ?? {}) });
    this.muted.set(state.muted ?? []);
    this.soloed.set(state.soloed ?? []);
    this.applyGains();
  }

  /** What a stem is actually contributing, once mutes and solos are applied. */
  public effectiveLevel(name: StemName): number {
    const soloed = this.soloed();
    if (soloed.length) return soloed.includes(name) ? this.levels()[name] : 0;
    return this.muted().includes(name) ? 0 : this.levels()[name];
  }

  private applyGains(): void {
    const context = this.context;
    for (const [name, gain] of this.gains) {
      const target = this.effectiveLevel(name);
      // A short ramp rather than a jump: an instant gain change on a playing
      // buffer is an audible click.
      if (context) {
        gain.gain.setTargetAtTime(target, context.currentTime, 0.01);
      } else {
        gain.gain.value = target;
      }
    }
  }

  private ensureContext(): AudioContext {
    if (!this.context) {
      try {
        this.context = new AudioContext({ sampleRate: MIXER_SAMPLE_RATE });
      } catch {
        // Some devices refuse a rate; the default one works just as well, it
        // only costs more memory.
        this.context = new AudioContext();
      }
      this.master = this.context.createGain();
      this.master.gain.value = this.volume();
      this.master.connect(this.context.destination);
    }
    return this.context;
  }

  /** Where the playhead is right now, from the audio clock rather than a timer. */
  private positionNow(): number {
    if (!this.context || !this.isPlaying()) return this.currentTime();
    // Sources are started a moment in the future, so until that moment
    // arrives the elapsed time is negative. Clamped, rather than shown.
    return Math.max(
      0,
      Math.min(
        this.duration(),
        this.startOffset + (this.context.currentTime - this.startedAt)
      )
    );
  }

  private startSources(from: number): void {
    const context = this.ensureContext();
    if (!this.buffers.size) return;
    this.stopSources();

    const offset = Math.max(0, Math.min(from, this.duration()));
    // Created on every start because a buffer source is single-use: once it
    // has been started it can never be started again.
    for (const [name, buffer] of this.buffers) {
      const source = context.createBufferSource();
      source.buffer = buffer;
      source.connect(this.gains.get(name) as GainNode);
      this.sources.set(name, source);
    }

    void context.resume();
    const when = context.currentTime + 0.02;
    for (const source of this.sources.values()) {
      source.start(when, offset);
    }
    this.startedAt = when;
    this.startOffset = offset;
    this.isPlaying.set(true);
    this.tick();
  }

  private stopSources(): void {
    cancelAnimationFrame(this.frame);
    this.frame = 0;
    for (const source of this.sources.values()) {
      try {
        source.stop();
      } catch {
        // Already finished; nothing to stop.
      }
      source.disconnect();
    }
    this.sources.clear();
  }

  /**
   * Follow the audio clock on every frame.
   *
   * The `<audio>` element reports its position about four times a second,
   * which is what made the chord highlighting lurch. This is as smooth as the
   * display.
   */
  private tick = (): void => {
    if (!this.isPlaying() || !this.context) return;
    const at = this.positionNow();
    if (at >= this.duration() - 0.001) {
      this.stopSources();
      this.isPlaying.set(false);
      this.currentTime.set(0);
      return;
    }
    this.currentTime.set(at);
    this.frame = requestAnimationFrame(this.tick);
  };

  private teardownMixer(): void {
    this.loadToken++;
    this.stopSources();
    for (const gain of this.gains.values()) gain.disconnect();
    this.gains.clear();
    // Hundreds of megabytes of decoded audio; letting go of it is the point.
    this.buffers.clear();
    this.mixing.set(false);
    this.loadingStems.set(false);
    this.stemError.set('');
  }

  ngOnDestroy() {
    this.audio.pause();
    this.teardownMixer();
    void this.context?.close();
    this.context = null;
    this.listeners.forEach(l => {
      this.audio.removeEventListener(l.name, l.handler);
    });
  }
}
