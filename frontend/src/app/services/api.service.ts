import { Injectable } from '@angular/core';
import {
  AppConfig,
  ChordsDoc,
  CleanupConfig,
  FusionConfig,
  Job,
  LyricsDoc,
  SheetDoc,
  SongSummary,
  SourceDoc,
} from '../models/artifacts';

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

  getSong(songId: string): Promise<SongSummary & { job: Job | null }> {
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
  ): Promise<{ song_id: string; job: Job }> {
    return this.request('/api/songs', {
      method: 'POST',
      body: JSON.stringify({ url, ...options }),
    });
  }

  // --- artifacts -----------------------------------------------------------

  getSource(songId: string): Promise<SourceDoc> {
    return this.request(`/api/songs/${encodeURIComponent(songId)}/source`);
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
    choice: 'ai' | 'manual',
    options: {
      lyrics?: string;
      language?: string | null;
      fusion?: FusionConfig;
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
   * Returns a function that closes the stream. `onDone` fires once the job
   * reaches a terminal state, whether it succeeded or not.
   */
  watchJob(
    jobId: string,
    onProgress: (job: Job) => void,
    onDone?: (job: Job) => void
  ): () => void {
    const source = new EventSource(
      `${this.baseUrl}/api/jobs/${encodeURIComponent(jobId)}/events`
    );
    let last: Job | null = null;

    source.onmessage = (event) => {
      const job = JSON.parse(event.data) as Job;
      last = job;
      onProgress(job);
      if (job.state === 'done' || job.state === 'failed' || job.state === 'needs_input') {
        source.close();
        onDone?.(job);
      }
    };

    // A dropped connection after the job finished is expected, not an error.
    source.onerror = () => {
      source.close();
      if (last && last.state !== 'done' && last.state !== 'failed') {
        this.getJob(jobId).then((job) => {
          onProgress(job);
          onDone?.(job);
        }).catch(() => undefined);
      }
    };

    return () => source.close();
  }

  // --- audio, export, settings ---------------------------------------------

  audioUrl(songId: string): string {
    return `${this.baseUrl}/api/songs/${encodeURIComponent(songId)}/audio`;
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
