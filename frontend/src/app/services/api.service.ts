import { Injectable } from '@angular/core';

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
}
export interface LyricsLine {
  text: string;
  start: number;
  end: number;
  timestamp: string;
}

export interface LyricsData {
  language: string;
  text: string;
  segments: LyricsLine[];
}

export interface ChordData {
  time: number;
  chord: string;
}

export interface BarData {
  bar_index: number;
  time: number;
  chords: string[];
}

@Injectable({
  providedIn: 'root'
})
export class ApiService {
  private baseUrl = 'http://127.0.0.1:8001';

  constructor() {}

  async selectFile(): Promise<any> {
    const res = await fetch(`${this.baseUrl}/api/select-file`);
    return res.json();
  }

  async exportToSongbook(payload: any): Promise<any> {
    const res = await fetch(`${this.baseUrl}/api/export-to-songbook`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (!res.ok) throw new Error('Failed to export to songbook');
    return res.json();
  }

  async extractChords(path: string): Promise<any> {
    const res = await fetch(`${this.baseUrl}/api/extract-chords`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path })
    });
    return res.json();
  }

  async extractYoutube(url: string, officialLyrics: string = '', fusionConfig: FusionConfig | null = null): Promise<any> {
    const res = await fetch(`${this.baseUrl}/api/convert`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, official_lyrics: officialLyrics, fusion_config: fusionConfig })
    });
    return res.json();
  }

  async getExtractionStatus(taskId: string): Promise<any> {
    const res = await fetch(`${this.baseUrl}/api/status`);
    return res.json();
  }

  async continueConvert(wavPath: string, choice: string, lyrics: string = '', language: string | null = null, fusionConfig: FusionConfig | null = null): Promise<any> {
    const res = await fetch(`${this.baseUrl}/api/convert_continue`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ wav_path: wavPath, choice, lyrics, language, fusion_config: fusionConfig })
    });
    return res.json();
  }

  async generateChordsheet(chords: ChordData[], lyrics: LyricsLine[], duration: number): Promise<any> {
    const res = await fetch(`${this.baseUrl}/api/generate-chordsheet`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chords, lyrics, duration })
    });
    return res.json();
  }

  async getConfig(): Promise<any> {
    const res = await fetch(`${this.baseUrl}/api/config`);
    return res.json();
  }

  async saveConfig(config: any): Promise<any> {
    const res = await fetch(`${this.baseUrl}/api/config`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config)
    });
    return res.json();
  }

  async saveLyrics(wavPath: string, lyrics: string): Promise<any> {
    const url = `${this.baseUrl}/api/save_lyrics`;
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ wav_path: wavPath, lyrics })
    });
    return response.json();
  }

  async alignLyrics(wavPath: string, lyrics: string): Promise<any> {
    const url = `${this.baseUrl}/api/align_lyrics`;
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ wav_path: wavPath, lyrics })
    });
    return response.json();
  }

  async saveChords(wavPath: string, beats: any[]): Promise<any> {
    const url = `${this.baseUrl}/api/save_chords`;
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ wav_path: wavPath, beats })
    });
    return response.json();
  }
  
  async saveSyncedSheet(wavPath: string, syncedLines: any[]): Promise<any> {
    const url = `${this.baseUrl}/api/save_synced`;
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ wav_path: wavPath, synced_lines: syncedLines })
    });
    return response.json();
  }

  async getSyncedSheet(wavPath: string): Promise<any> {
    const url = `${this.baseUrl}/api/get_synced?wav_path=${encodeURIComponent(wavPath)}`;
    const response = await fetch(url);
    return response.json();
  }

  async getWordTimestamps(wavPath: string): Promise<any> {
    const url = `${this.baseUrl}/api/get_word_timestamps?wav_path=${encodeURIComponent(wavPath)}`;
    const response = await fetch(url);
    return response.json();
  }
}
