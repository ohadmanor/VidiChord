import { Injectable, NgZone, inject } from '@angular/core';
import {
  AppConfig,
  ChordsDoc,
  CleanupConfig,
  FusionConfig,
  Job,
  LyricsDoc,
  SheetDoc,
  SongDetail,
  SongSummary,
  SourceDoc,
  StemName,
  StemsDoc,
} from '../models/artifacts';

/** States after which a job reports nothing more. */
const TERMINAL_JOB_STATES: Job['state'][] = ['done', 'failed', 'needs_input'];

/** How often to ask after a job once its event stream has dropped. */
const POLL_MS = 1500;

/** Failed polls in a row before the run is given up on - about 7.5 s. */
const LOST_AFTER_MISSES = 5;

const LOST_RUN =
  'Lost contact with this run - the app may have been restarted. Open the song ' +
  'from the library to see how far it got, and run it again from there.';

/**
 * Client for the VidiChord API.
 *
 * Every call maps onto one resource: a song, one of its four artifacts, or a
 * pipeline job. Progress arrives over server-sent events rather than by
 * polling a global status object.
 */
@Injectable({ providedIn: 'root' })
export class ApiService {
  /** Same-origin when served by the backend; absolute for `ng serve`. */
  readonly baseUrl = this.resolveBaseUrl();
  private readonly zone = inject(NgZone);

  private resolveBaseUrl(): string {
    const devServerPorts = ['4200', '4300'];
    if (devServerPorts.includes(window.location.port)) {
      return 'http://127.0.0.1:8001';
    }
    return '';
  }

  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      headers: init?.body ? { 'Content-Type': 'application/json' } : undefined,
      ...init,
    });

    if (!response.ok) {
      throw new Error(await this.describeError(response));
    }
    if (response.status === 204) {
      return undefined as T;
    }
    return response.json() as Promise<T>;
  }

  /** Turn an error response into something worth showing the user. */
  private async describeError(response: Response): Promise<string> {
    try {
      const body = await response.json();
      if (typeof body?.detail === 'string') {
        return body.detail;
      }
      if (Array.isArray(body?.detail)) {
        return body.detail.map((d: any) => d.msg ?? JSON.stringify(d)).join('; ');
      }
    } catch {
      // Fall through to the status text.
    }
    return `${response.status} ${response.statusText}`;
  }

  // --- library -------------------------------------------------------------

  listSongs(): Promise<SongSummary[]> {
    return this.request<SongSummary[]>('/api/songs');
  }

  getSong(songId: string): Promise<SongDetail> {
    return this.request(`/api/songs/${encodeURIComponent(songId)}`);
  }

  deleteSong(songId: string): Promise<void> {
    return this.request(`/api/songs/${encodeURIComponent(songId)}`, {
      method: 'DELETE',
    });
  }

  /**
   * Start a run from a YouTube URL. Returns the new song and its job.
   *
   * With `review`, the run stops once the chords are in, leaving the sheet to
   * be built by {@link syncSheet} after the results have been corrected.
   */
  createFromYoutube(
    url: string,
    options: {
      language?: string | null;
      fusion?: FusionConfig;
      cleanup?: CleanupConfig;
      review?: boolean;
    } = {}
  ): Promise<{ song_id: string; title?: string; artist?: string; duration?: number; job: Job }> {
    return this.request('/api/songs', {
      method: 'POST',
      body: JSON.stringify({ url, ...options }),
    });
  }

  // --- artifacts -----------------------------------------------------------

  getSource(songId: string): Promise<SourceDoc> {
    return this.request(`/api/songs/${encodeURIComponent(songId)}/source`);
  }

  /**
   * What separation produced for this song.
   *
   * A document with `unavailable` set is the ordinary answer on a machine
   * without Demucs: it says why there are no stems rather than leaving the
   * player to guess. 404 means stage 5 has not run at all yet.
   */
  getStems(songId: string): Promise<StemsDoc> {
    return this.request(`/api/songs/${encodeURIComponent(songId)}/stems`);
  }

  getLyrics(songId: string): Promise<LyricsDoc> {
    return this.request(`/api/songs/${encodeURIComponent(songId)}/lyrics`);
  }

  /**
   * Save edited lyrics.
   *
   * The backend rebuilds the sheet from them unless `rebuild` is false, which
   * is what the review editor wants: it saves lyrics and chords together and
   * lays the sheet out once, on sync.
   */
  putLyrics(
    songId: string,
    document: LyricsDoc,
    rebuild = true
  ): Promise<{ status: string }> {
    return this.request(
      `/api/songs/${encodeURIComponent(songId)}/lyrics?rebuild=${rebuild}`,
      { method: 'PUT', body: JSON.stringify(document) }
    );
  }

  getChords(songId: string): Promise<ChordsDoc> {
    return this.request(`/api/songs/${encodeURIComponent(songId)}/chords`);
  }

  /** Save edited chords. See {@link putLyrics} for `rebuild`. */
  putChords(
    songId: string,
    document: ChordsDoc,
    rebuild = true
  ): Promise<{ status: string }> {
    return this.request(
      `/api/songs/${encodeURIComponent(songId)}/chords?rebuild=${rebuild}`,
      { method: 'PUT', body: JSON.stringify(document) }
    );
  }

  /** Lay the sheet out from the saved lyrics and chords, and return it. */
  syncSheet(songId: string): Promise<SheetDoc> {
    return this.request(`/api/songs/${encodeURIComponent(songId)}/sync`, {
      method: 'POST',
    });
  }

  getSheet(songId: string): Promise<SheetDoc> {
    return this.request(`/api/songs/${encodeURIComponent(songId)}/sheet`);
  }

  /** Save a hand-edited sheet verbatim, without rebuilding it. */
  putSheet(songId: string, document: SheetDoc): Promise<{ status: string }> {
    return this.request(`/api/songs/${encodeURIComponent(songId)}/sheet`, {
      method: 'PUT',
      body: JSON.stringify(document),
    });
  }

  // --- pipeline ------------------------------------------------------------

  /** Re-run one stage, optionally continuing through the ones after it. */
  rerunStage(
    songId: string,
    stage: number,
    options: {
      language?: string | null;
      fusion?: FusionConfig;
      cleanup?: CleanupConfig;
      retranscribe?: boolean;
      force?: boolean;
      cascade?: boolean;
      review?: boolean;
    } = {}
  ): Promise<Job> {
    return this.request(
      `/api/songs/${encodeURIComponent(songId)}/stages/${stage}/rerun`,
      { method: 'POST', body: JSON.stringify({ cascade: true, ...options }) }
    );
  }

  /** Resume a run that paused because no lyrics could be found. */
  submitLyricsChoice(
    songId: string,
    choice: 'ai' | 'manual' | 'instrumental',
    options: {
      lyrics?: string;
      language?: string | null;
      fusion?: FusionConfig;
      // Sent for the same reason the re-run calls send it: resuming re-fuses
      // the chords, and without the user's tuning they come back different
      // from the run they were looking at.
      cleanup?: CleanupConfig;
      review?: boolean;
    } = {}
  ): Promise<Job> {
    return this.request(`/api/songs/${encodeURIComponent(songId)}/lyrics/choice`, {
      method: 'POST',
      body: JSON.stringify({ choice, lyrics: '', ...options }),
    });
  }

  getJob(jobId: string): Promise<Job> {
    return this.request(`/api/jobs/${encodeURIComponent(jobId)}`);
  }

  /**
   * Follow a job's progress over server-sent events.
   *
   * Returns a function that stops following it. `onDone` fires once, and only
   * with a job in a terminal state - whether it succeeded or not.
   *
   * A stream that drops mid-run hands over to polling. It used to hand the
   * job's last state straight to `onDone`, still running, and the app then
   * announced a finished run in the middle of one. Polling that fails several
   * times in a row - the backend restarted, say - ends the watch with a
   * failed job that says so, rather than leaving the screen waiting forever.
   */
  watchJob(
    jobId: string,
    onProgress: (job: Job) => void,
    onDone?: (job: Job) => void
  ): () => void {
    let stopped = false;
    let finished = false;
    let misses = 0;
    let last: Job | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const source = new EventSource(
      `${this.baseUrl}/api/jobs/${encodeURIComponent(jobId)}/events`
    );

    // Inside Angular's zone, explicitly. zone.js patches EventSource only in
    // its legacy bundle, which this app does not load, so the stream's
    // messages arrive outside the zone and nothing re-renders for them. That
    // went unnoticed while something else happened to trigger a render - a
    // ticking timer, the audio element loading - and showed as a screen that
    // stayed on "Done" and never moved on to the review.
    const deliver = (job: Job) =>
      this.zone.run(() => {
        if (stopped || finished) return;
        // A slower answer overtaken by a newer one.
        if (last && job.version < last.version) return;
        last = job;
        onProgress(job);
        if (TERMINAL_JOB_STATES.includes(job.state)) {
          finished = true;
          source.close();
          clearTimeout(timer);
          onDone?.(job);
        }
      });

    const poll = () => {
      if (stopped || finished) return;
      this.getJob(jobId)
        .then((job) => {
          misses = 0;
          deliver(job);
        })
        .catch(() => {
          misses += 1;
          if (misses < LOST_AFTER_MISSES) return;
          const base: Job = last ?? {
            job_id: jobId, song_id: '', state: 'running', stage: 0, stage_label: '',
            message: '', percent: 0, error: '', options: [], updated_at: '', version: 0,
          };
          deliver({ ...base, state: 'failed', error: LOST_RUN, version: base.version + 1 });
        })
        .finally(() => {
          if (!stopped && !finished) timer = setTimeout(poll, POLL_MS);
        });
    };

    source.onmessage = (event) => deliver(JSON.parse(event.data) as Job);
    // The server closes the stream after the final event, which deliver() has
    // already handled. Any other error - a dropped connection, a restarted
    // backend - hands over to polling.
    source.onerror = () => {
      source.close();
      if (!finished) poll();
    };

    return () => {
      stopped = true;
      source.close();
      clearTimeout(timer);
    };
  }

  // --- audio, export, settings ---------------------------------------------

  audioUrl(songId: string): string {
    return `${this.baseUrl}/api/songs/${encodeURIComponent(songId)}/audio`;
  }

  /** One separated part. The server decides the file, so the format is its business. */
  stemUrl(songId: string, name: StemName): string {
    return `${this.baseUrl}/api/songs/${encodeURIComponent(songId)}/stems/${name}`;
  }

  exportToSongbook(songId: string): Promise<{ filename: string; path: string }> {
    return this.request(`/api/songs/${encodeURIComponent(songId)}/export`, {
      method: 'POST',
    });
  }

  getConfig(): Promise<AppConfig> {
    return this.request('/api/config');
  }

  saveConfig(config: AppConfig): Promise<AppConfig> {
    return this.request('/api/config', {
      method: 'PUT',
      body: JSON.stringify(config),
    });
  }
}
