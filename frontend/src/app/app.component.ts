import { Component, OnInit, OnDestroy, effect } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { 
  LucideMusic, 
  LucideUpload, 
  LucideLayers, 
  LucideFileText, 
  LucidePlay, 
  LucidePause, 
  LucideVolume2, 
  LucideAlertCircle, 
  LucideRefreshCw, 
  LucideClipboardCheck,
  LucideSettings,
  LucideX
} from '@lucide/angular';
import { WaveformComponent } from './components/waveform/waveform.component';
import { ApiService, BarData, ChordData, LyricsLine, LyricsData, FusionConfig } from './services/api.service';
import { AudioService } from './services/audio.service';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    WaveformComponent,
    LucideMusic,
    LucideUpload,
    LucideLayers,
    LucideFileText,
    LucidePlay,
    LucidePause,
    LucideVolume2,
    LucideAlertCircle,
    LucideRefreshCw,
    LucideClipboardCheck,
    LucideSettings,
    LucideX
  ],
  templateUrl: './app.component.html',
  styleUrls: ['./app.component.css']
})
export class AppComponent implements OnInit, OnDestroy {
  // Extracted Data
  public lyricsData: string | null = null;
  public chords: ChordData[] = [];
  public bars: BarData[] = [];
  
  // Extraction states
  public audioPath = '';
  public filename = '';
  public extractionStatus: 'idle' | 'starting' | 'downloading' | 'converting' | 'extracting_lyrics' | 'waiting_for_user_choice' | 'extracting_chords' | 'completed' | 'failed' = 'idle';
  public extractionProgress = 0;
  public extractionMessage = '';
  
  // Editor/Project state
  public chordsheetText = '';
  public unsyncedLyrics = '';
  public wordTimestamps: any[] = [];
  public youtubeUrl = '';
  public songTitle = '';
  public songArtist = '';
  public songBpm = 0;
  public error = '';
  public success = '';
  public parsedBars: { bar: number, timestamp: string, time: number, beats: any[], section?: string }[] = [];
  
  // Active bar index for playback highlighting in chord sheet
  public activeBarIndex = -1;
  
  // Section label options
  public sectionOptions = ['', 'Intro', 'Verse', 'Pre-Chorus', 'Chorus', 'Bridge', 'Solo', 'Outro', 'Interlude', 'Coda'];
  
  // User Choice Modal State
  public showUserChoiceModal = false;
  public manualLyricsInput = '';
  public selectedLanguage = 'auto';
  
  // View mode: 'raw' = side-by-side lyrics+chords, 'synced' = unified synced sheet
  public viewMode: 'raw' | 'synced' = 'raw'; // Toggle view
  public sheetMode: 'view' | 'edit' = 'view';
  public isExporting: boolean = false;
  
  // Active line index for playback highlighting
  public activeLineIndex = -1;
  
  // Hover playbar state
  public showHoverTooltip = false;
  public hoverLeft = 0;
  public hoverTimeText = '';

  // Settings Modal State
  public showSettingsModal = false;
  public appConfig = {
    audios_dir: '',
    lyrics_dir: '',
    chords_dir: '',
    sheets_dir: ''
  };
  public settingsMessage = '';

  // Advanced Fusion Settings
  public showAdvancedSettings = false;
  public songFusionConfig: FusionConfig = {
    emission_weights: {
      essentia_match: 0.5,
      librosa_match: 0.9,
      madmom_match: 0.5,
      none_state_bias: 0.8
    },
    transition_probabilities: {
      self_transition: 0.75,
      same_root_diff_quality: 0.05,
      circle_of_fifths_dist_1: 0.7,
      circle_of_fifths_dist_2: 0.4,
      unrelated_chord: 0.001
    }
  };

  private pollingTimer: any = null;

  constructor(
    public audioService: AudioService,
    private apiService: ApiService
  ) {
    // Sync volume with signal updates
    effect(() => {
      this.audioService.setVolume(this.audioService.volume());
    }, { allowSignalWrites: true });
    
    // Track active line during playback
    effect(() => {
      const currentTime = this.audioService.currentTime();
      if (this.syncedLines.length === 0) return;
      
      let newIndex = -1;
      for (let i = this.syncedLines.length - 1; i >= 0; i--) {
        if (currentTime >= this.syncedLines[i].time) {
          newIndex = i;
          break;
        }
      }
      
      if (newIndex !== this.activeLineIndex) {
        this.activeLineIndex = newIndex;
        // Auto-scroll to active line
        setTimeout(() => {
          const el = document.querySelector('.synced-block.active-line');
          if (el) {
            el.scrollIntoView({ behavior: 'smooth', block: 'center' });
          }
        }, 50);
      }
    });
    
    // Track active bar during playback
    effect(() => {
      const currentTime = this.audioService.currentTime();
      if (this.parsedBars.length === 0) return;
      
      let newBarIndex = -1;
      for (let i = this.parsedBars.length - 1; i >= 0; i--) {
        if (currentTime >= this.parsedBars[i].time) {
          newBarIndex = i;
          break;
        }
      }
      
      if (newBarIndex !== this.activeBarIndex) {
        this.activeBarIndex = newBarIndex;
        setTimeout(() => {
          const el = document.querySelector('.bar-card.active-bar');
          if (el) {
            el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
          }
        }, 50);
      }
    });
  }

  async ngOnInit() {
    try {
      this.appConfig = await this.apiService.getConfig();
    } catch (e) {
      console.error('Failed to load config', e);
    }
    const savedFusion = localStorage.getItem('vidiChordFusionConfig');
    if (savedFusion) {
      try {
        this.songFusionConfig = JSON.parse(savedFusion);
      } catch (e) {}
    }
  }

  ngOnDestroy() {
    this.clearPolling();
  }

  public async handleSelectYoutube() {
    if (!this.youtubeUrl || !this.youtubeUrl.trim()) return;
    this.error = '';
    this.success = '';
    this.extractionStatus = 'starting';
    this.extractionProgress = 0;
    this.extractionMessage = 'Connecting to YouTube...';
    
    // Reset state
    this.unsyncedLyrics = '';
    this.chordsheetText = '';

    try {
      localStorage.setItem('vidiChordFusionConfig', JSON.stringify(this.songFusionConfig));
      await this.apiService.extractYoutube(this.youtubeUrl, "", this.songFusionConfig);

      this.clearPolling();
      this.pollingTimer = setInterval(() => {
        this.pollStatus();
      }, 1000);
    } catch (err: any) {
      this.extractionStatus = 'failed';
      this.extractionMessage = `Error: ${err.message}`;
    }
  }

  private async pollStatus() {
    try {
      const data = await this.apiService.getExtractionStatus('');
      
      // Update local state based on backend response
      this.extractionProgress = (data.progress || 0) / 100;
      this.extractionMessage = data.log_msg || data.status;

      if (data.status === 'success') {
        this.extractionStatus = 'completed';
        this.success = `Lyrics extracted successfully for: ${data.video_title || ''}`;
        
        this.songTitle = data.video_title || '';
        this.songArtist = data.song_artist || '';
        this.songBpm = data.song_bpm || 0;
        
        if (data.chords_data) {
          this.groupChordsByBar(data.chords_data);
        }
        
        if (data.output_filename) {
            this.filename = data.output_filename;
            this.audioPath = data.output_filename; 
            this.audioService.loadTrack(data.output_filename); // load the wav file
        }
        
        // Grab the lyrics
        if (data.lyrics) {
          this.lyricsData = data.lyrics;
        }

        // Grab word timestamps
        try {
          const res = await this.apiService.getWordTimestamps(this.audioPath);
          if (res.status === 'success') {
            this.wordTimestamps = [];
            res.segments.forEach((seg: any) => {
              if (seg.words) {
                this.wordTimestamps.push(...seg.words);
              }
            });
          }
        } catch(e) {}

        // Build or load the synced chord sheet if both lyrics and chords are available
        if (this.lyricsData && this.parsedBars.length > 0) {
          try {
            const syncedRes = await this.apiService.getSyncedSheet(this.audioPath);
            if (syncedRes.status === 'success' && syncedRes.synced_lines) {
              this.syncedLines = syncedRes.synced_lines;
            } else {
              this.buildSyncedSheet();
            }
          } catch (e) {
            this.buildSyncedSheet();
          }
        }

        this.clearPolling();
      } else if (data.status === 'waiting_for_user_choice') {
        this.extractionStatus = 'waiting_for_user_choice';
        this.showUserChoiceModal = true;
        // Don't clear polling, or maybe we do clear it and then resume?
        // Let's clear polling while waiting, then restart it when they submit
        this.clearPolling();
        if (data.output_filename) {
          this.audioPath = data.output_filename;
        }
      } else if (data.status === 'failed' || data.status === 'error') {
        this.extractionStatus = 'failed';
        this.extractionMessage = data.error_msg || 'An error occurred';
        this.clearPolling();
      } else {
        // Still processing
        this.extractionStatus = data.status;
      }
    } catch (err) {
      console.error('Error polling extraction status:', err);
    }
  }

  private clearPolling() {
    if (this.pollingTimer) {
      clearInterval(this.pollingTimer);
      this.pollingTimer = null;
    }
  }

  public async submitAiChoice() {
    this.showUserChoiceModal = false;
    this.extractionStatus = 'extracting_lyrics';
    this.extractionMessage = 'Starting AI transcription...';
    try {
      localStorage.setItem('vidiChordFusionConfig', JSON.stringify(this.songFusionConfig));
      const lang = this.selectedLanguage === 'auto' ? null : this.selectedLanguage;
      await this.apiService.continueConvert(this.audioPath, 'ai', '', lang, this.songFusionConfig);
      // Restart polling
      this.pollingTimer = setInterval(() => {
        this.pollStatus();
      }, 1000);
    } catch (err: any) {
      this.error = `Failed to start AI transcript: ${err.message}`;
    }
  }

  public async submitManualChoice() {
    if (!this.manualLyricsInput.trim()) return;
    this.showUserChoiceModal = false;
    this.extractionStatus = 'extracting_lyrics';
    this.extractionMessage = 'Processing manual lyrics...';
    try {
      localStorage.setItem('vidiChordFusionConfig', JSON.stringify(this.songFusionConfig));
      await this.apiService.continueConvert(this.audioPath, 'manual', this.manualLyricsInput, null, this.songFusionConfig);
      // Restart polling
      this.pollingTimer = setInterval(() => {
        this.pollStatus();
      }, 1000);
    } catch (err: any) {
      this.error = `Failed to start manual transcript: ${err.message}`;
    }
  }

  public isAligning = false;

  public async saveEditedLyrics() {
    if (!this.lyricsData || !this.audioPath) return;
    try {
      const hasTimestamps = /\[\d{1,2}:\d{2}\.\d{1,3}\]/.test(this.lyricsData);
      
      if (!hasTimestamps) {
        this.success = 'Aligning lyrics with AI...';
        this.isAligning = true;
        const response = await this.apiService.alignLyrics(this.audioPath, this.lyricsData);
        if (response.status === 'success') {
          this.lyricsData = response.aligned_lyrics;
          this.success = 'Lyrics aligned successfully! Resyncing...';
          setTimeout(() => this.success = '', 3000);
          this.buildSyncedSheet();
        } else {
          this.error = response.error || 'Failed to align lyrics.';
        }
        this.isAligning = false;
        return;
      }

      this.success = 'Saving lyrics...';
      const response = await this.apiService.saveLyrics(this.audioPath, this.lyricsData);
      if (response.status === 'success') {
        this.success = 'Lyrics saved successfully! Resyncing...';
        setTimeout(() => this.success = '', 3000);
        // Re-run the sync logic with the new lyrics
        this.buildSyncedSheet();
      } else {
        this.error = response.error || 'Failed to save lyrics.';
      }
    } catch (err: any) {
      this.error = `Error saving lyrics: ${err.message}`;
      this.isAligning = false;
    }
  }

  public async doubleBpm() {
    if (!this.parsedBars.length || !this.audioPath) return;
    // Flatten all beats
    const flatBeats: any[] = [];
    this.parsedBars.forEach(bar => flatBeats.push(...bar.beats));
    
    // Insert a new beat at the midpoint between each existing pair
    const newBeats: any[] = [];
    for (let i = 0; i < flatBeats.length; i++) {
      const b = flatBeats[i];
      const midTime = (b.start_time + b.end_time) / 2;
      // First half-beat keeps original chord
      newBeats.push({
        start_time: b.start_time,
        end_time: midTime,
        chord: b.chord
      });
      // Second half-beat is 'N' (no chord unless same chord continues)
      newBeats.push({
        start_time: midTime,
        end_time: b.end_time,
        chord: b.chord
      });
    }
    // Re-number bars and beats (4 beats per bar)
    for (let i = 0; i < newBeats.length; i++) {
      newBeats[i].beat_index = i + 1;
      newBeats[i].bar = Math.floor(i / 4) + 1;
      newBeats[i].beat_in_bar = (i % 4) + 1;
    }
    this.songBpm = Math.round(this.songBpm * 2);
    this.groupChordsByBar(newBeats);
    this.buildSyncedSheet();
    await this.apiService.saveChords(this.audioPath, newBeats);
    this.success = `BPM doubled to ${this.songBpm}`;
    setTimeout(() => this.success = '', 3000);
  }

  public async halfBpm() {
    if (!this.parsedBars.length || !this.audioPath) return;
    // Flatten all beats
    const flatBeats: any[] = [];
    this.parsedBars.forEach(bar => flatBeats.push(...bar.beats));
    
    // Merge every 2 consecutive beats into one
    const newBeats: any[] = [];
    for (let i = 0; i < flatBeats.length; i += 2) {
      const b1 = flatBeats[i];
      const b2 = flatBeats[i + 1];
      newBeats.push({
        start_time: b1.start_time,
        end_time: b2 ? b2.end_time : b1.end_time,
        chord: b1.chord !== 'N' ? b1.chord : (b2 ? b2.chord : 'N')
      });
    }
    // Re-number bars and beats (4 beats per bar)
    for (let i = 0; i < newBeats.length; i++) {
      newBeats[i].beat_index = i + 1;
      newBeats[i].bar = Math.floor(i / 4) + 1;
      newBeats[i].beat_in_bar = (i % 4) + 1;
    }
    this.songBpm = Math.round(this.songBpm / 2);
    this.groupChordsByBar(newBeats);
    this.buildSyncedSheet();
    await this.apiService.saveChords(this.audioPath, newBeats);
    this.success = `BPM halved to ${this.songBpm}`;
    setTimeout(() => this.success = '', 3000);
  }

  public onSectionChange(barIndex: number, section: string) {
    if (barIndex < 0 || barIndex >= this.parsedBars.length) return;
    this.parsedBars[barIndex].section = section || undefined;
    // Store section on the first beat of this bar for persistence
    this.parsedBars[barIndex].beats.forEach((b, i) => {
      b.section = i === 0 ? (section || undefined) : undefined;
    });
    // Save to backend
    if (this.audioPath) {
      const flatBeats: any[] = [];
      this.parsedBars.forEach(bar => flatBeats.push(...bar.beats));
      this.apiService.saveChords(this.audioPath, flatBeats);
    }
    
    // Sync section back to Extracted Lyrics and adapt the timestamp
    if (this.lyricsData) {
      const barTime = this.parsedBars[barIndex].time;
      const lines = this.lyricsData.split('\n');
      const tsRegex = /^\[(\d{1,2}):(\d{2})\.(\d{1,3})\]/;
      const sectionTagRegex = /^\[([^:\]]+)\]$/;
      
      let bestLineIdx = -1;
      let minDiff = Infinity;
      
      for (let i = 0; i < lines.length; i++) {
        const trimmed = lines[i].trim();
        const match = trimmed.match(tsRegex);
        if (match) {
          const mins = parseInt(match[1], 10);
          const secs = parseInt(match[2], 10);
          const ms = parseInt(match[3].padEnd(3, '0'), 10);
          const time = mins * 60 + secs + ms / 1000;
          
          const diff = Math.abs(time - barTime);
          if (diff < minDiff && diff < 4.0) { // must be within 4 seconds
            minDiff = diff;
            bestLineIdx = i;
          }
        }
      }
      
      if (bestLineIdx !== -1) {
        // Update or insert the section tag before the found line
        let foundExistingTagIdx = -1;
        for (let j = bestLineIdx - 1; j >= 0; j--) {
          const prevTrim = lines[j].trim();
          if (tsRegex.test(prevTrim)) {
            break;
          }
          if (sectionTagRegex.test(prevTrim)) {
            foundExistingTagIdx = j;
            break;
          }
        }
        
        if (foundExistingTagIdx !== -1) {
          if (section) {
            lines[foundExistingTagIdx] = `[${section}]`;
          } else {
            lines.splice(foundExistingTagIdx, 1);
            bestLineIdx--; // Shift bestLineIdx up because we removed a line
          }
        } else {
          if (section) {
            lines.splice(bestLineIdx, 0, `[${section}]`);
            bestLineIdx++; // Shift bestLineIdx down because we inserted a line
          }
        }
        
        // Adapt timestamp of the lyric line to exactly match the bar's timestamp
        if (section) {
          const m = Math.floor(barTime / 60).toString().padStart(2, '0');
          const s = Math.floor(barTime % 60).toString().padStart(2, '0');
          const ms = Math.floor((barTime % 1) * 100).toString().padStart(2, '0');
          
          const oldLine = lines[bestLineIdx];
          const textPart = oldLine.replace(tsRegex, '').trim();
          lines[bestLineIdx] = `[${m}:${s}.${ms}] ${textPart}`;
        }
        
        this.lyricsData = lines.join('\n');
        
        if (this.audioPath) {
          this.apiService.saveLyrics(this.audioPath, this.lyricsData).then(() => {
            // Re-build synced sheet after save completes
            this.buildSyncedSheet();
          });
        }
      }
    }
  }

  public seekToBar(bar: any) {
    if (bar && bar.time !== undefined) {
      this.audioService.seek(bar.time);
    }
  }

  // Audio delegates
  public togglePlayPause() {
    this.audioService.togglePlay();
  }

  public handleSeek(time: number) {
    this.audioService.seek(time);
  }

  public formatTime(secs: number): string {
    if (!secs || isNaN(secs)) return '00:00.000';
    const m = Math.floor(secs / 60);
    const s = Math.floor(secs % 60);
    const ms = Math.floor((secs % 1) * 1000);
    return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}.${ms.toString().padStart(3, '0')}`;
  }

  public handlePlayBarMouseMove(event: MouseEvent) {
    const container = event.currentTarget as HTMLElement;
    if (!container) return;
    const rect = container.getBoundingClientRect();
    const x = event.clientX - rect.left;
    this.hoverLeft = x;
    
    const ratio = Math.max(0, Math.min(1, x / rect.width));
    const duration = this.audioService.duration() || 0;
    const hoverTime = ratio * duration;
    
    this.hoverTimeText = this.formatTime(hoverTime);
  }

  public isActiveLine(time: number): boolean {
    const current = this.audioService.currentTime();
    return current >= time && current < time + 3; // Highlight active within 3 sec window (naive sync without end time)
  }

  public async saveSettings() {
    this.settingsMessage = 'Saving...';
    try {
      localStorage.setItem('vidiChordFusionConfig', JSON.stringify(this.songFusionConfig));
      const response = await this.apiService.saveConfig(this.appConfig);
      if (response.status === 'success') {
        this.appConfig = response.config;
        this.settingsMessage = 'Settings saved successfully!';
        setTimeout(() => {
          this.showSettingsModal = false;
          this.settingsMessage = '';
        }, 1500);
      } else {
        this.settingsMessage = 'Error: ' + response.error;
      }
    } catch (e: any) {
      this.settingsMessage = 'Failed to save settings: ' + e.message;
    }
  }

  public get isHebrew(): boolean {
    if (!this.lyricsData) return false;
    return /[\u0590-\u05FF]/.test(this.lyricsData);
  }

  // ======== Synced Chord Sheet Types ========
  // A parsed lyric line with its timestamp
  public syncedLines: SyncedLine[] = [];

  groupChordsByBar(beats: any[]) {
    // 1. Shift chords to align the first non-'N' chord to the beginning of its bar
    let firstChordIdx = -1;
    for (let i = 0; i < beats.length; i++) {
      if (beats[i].chord !== 'N') {
        firstChordIdx = i;
        break;
      }
    }

    if (firstChordIdx !== -1) {
      const firstBeat = beats[firstChordIdx];
      let barStartIdx = firstChordIdx;
      // Find the first beat of the same bar
      while (barStartIdx > 0 && beats[barStartIdx - 1].bar === firstBeat.bar) {
        barStartIdx--;
      }
      
      const offset = firstChordIdx - barStartIdx;
      if (offset > 0) {
        // Shift all chords left by `offset`
        const originalChords = beats.map(b => b.chord);
        for (let i = 0; i < beats.length; i++) {
          if (i + offset < originalChords.length) {
            beats[i].chord = originalChords[i + offset];
          } else {
            beats[i].chord = 'N';
          }
        }
      }
    }

    const barsMap = new Map<number, any[]>();
    beats.forEach(b => {
      if (!barsMap.has(b.bar)) {
        barsMap.set(b.bar, []);
      }
      barsMap.get(b.bar)?.push(b);
    });
    
    this.parsedBars = Array.from(barsMap.entries())
      .map(([barNum, beatsArr]) => {
        const startTime = beatsArr[0]?.start_time || 0;
        const mins = Math.floor(startTime / 60);
        const secs = Math.floor(startTime % 60);
        const ms = Math.floor((startTime % 1) * 10);
        const timestamp = `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}.${ms}`;
        const section = beatsArr[0]?.section || undefined;
        return { bar: barNum, beats: beatsArr, timestamp, time: startTime, section };
      })
      .sort((a, b) => a.bar - b.bar);
      
    // Mark repeated chords for visual styling
    let lastChord = '';
    this.parsedBars.forEach(bar => {
      bar.beats.forEach(b => {
        b.isRepeated = (b.chord === lastChord) && b.chord !== 'N';
        lastChord = b.chord;
      });
    });
  }

  /**
   * Main sync entry point — called after both lyrics and chords are available.
   * Builds the syncedLines array used by the HTML template.
   */
  buildSyncedSheet() {
    if (!this.lyricsData) return;
    
    // 1. Parse lyrics into timed lines
    const lyricLines = this.parseLyricLines(this.lyricsData);
    if (lyricLines.length === 0) return;

    // Clear sections from bars, as lyrics are the source of truth
    this.parsedBars.forEach(bar => {
      bar.section = undefined;
      bar.beats.forEach(b => b.section = undefined);
    });

    let chordsChanged = false;

    // Apply sections from lyrics to nearest bars
    for (const line of lyricLines) {
      if (line.sectionName) {
        let closestBar = this.parsedBars[0];
        let minDiff = Infinity;
        for (const bar of this.parsedBars) {
          const diff = Math.abs(bar.time - line.time);
          if (diff < minDiff) {
            minDiff = diff;
            closestBar = bar;
          }
        }
        if (closestBar) {
          closestBar.section = line.sectionName;
          if (closestBar.beats.length > 0) closestBar.beats[0].section = line.sectionName;
          chordsChanged = true;
          // Optionally add it to options if it's custom
          if (!this.sectionOptions.includes(line.sectionName)) {
            this.sectionOptions.push(line.sectionName);
          }
        }
      }
    }

    if (chordsChanged && this.audioPath) {
      const flatBeats: any[] = [];
      this.parsedBars.forEach(bar => flatBeats.push(...bar.beats));
      this.apiService.saveChords(this.audioPath, flatBeats);
    }

    const result: any[] = [];
    let barIdx = 0;

    // Helper to get bars until a certain time
    const getBarsUntil = (timeLimit: number) => {
      const bars: any[] = [];
      while (barIdx < this.parsedBars.length && this.parsedBars[barIdx].time < timeLimit) {
        bars.push(this.parsedBars[barIdx]);
        barIdx++;
      }
      return bars;
    };

    // 1. Intro Instrumental
    const introBars = getBarsUntil(lyricLines[0].time - 0.5);
    if (introBars.length > 0) {
      result.push({
        type: 'instrumental',
        text: this.formatInstrumentalBars(introBars),
        bars: introBars,
        time: introBars[0].time
      });
    }

    // 2. Process each lyric line
    for (let i = 0; i < lyricLines.length; i++) {
      const line = lyricLines[i];
      const nextLineTime = i + 1 < lyricLines.length ? lyricLines[i + 1].time : Number.MAX_VALUE;
      
      let actualLineEnd = nextLineTime !== Number.MAX_VALUE ? nextLineTime : line.time + 5;
      
      // If the gap is massive, limit the lyric line's duration to 5 seconds
      if (actualLineEnd - line.time > 8.0) {
          actualLineEnd = line.time + 5.0;
      }
      
      const duration = actualLineEnd - line.time || 1;
      
      // Chords for the lyric line
      const lineBars = getBarsUntil(actualLineEnd);
      
      const lineChords: any[] = [];
      lineBars.forEach(bar => {
        bar.beats.forEach((beat: any) => {
          if (beat.chord !== 'N' && !beat.isRepeated) {
            lineChords.push({ chord: beat.chord, time: beat.start_time });
          }
        });
      });
      lineChords.sort((a,b) => a.time - b.time);

      // 1. Build interpolation anchors
      let anchors: {time: number, charIdx: number}[] = [];
      anchors.push({ time: line.time, charIdx: 0 });
      
      let lastCharIdx = 0;
      let lastTime = line.time;
      
      if (this.wordTimestamps && this.wordTimestamps.length > 0) {
          for (const w of this.wordTimestamps) {
             if (w.start >= line.time - 1.0 && w.start <= actualLineEnd + 1.0) {
                 const searchWord = w.word.trim().toLowerCase();
                 // Find index starting from lastCharIdx to ensure monotonic progression
                 const matchIdx = line.text.toLowerCase().indexOf(searchWord, lastCharIdx);
                 if (matchIdx !== -1) {
                     anchors.push({ time: w.start, charIdx: matchIdx });
                     const endIdx = matchIdx + searchWord.length;
                     anchors.push({ time: w.end || w.start + 0.3, charIdx: endIdx });
                     lastCharIdx = endIdx;
                     lastTime = w.end || w.start + 0.3;
                 }
             }
          }
      }
      
      // Final anchor for the end of the line (plus trailing gap space)
      const timeRemaining = Math.max(0, actualLineEnd - lastTime);
      const extraChars = Math.floor(timeRemaining * 3); // 3 chars per second of trailing gap
      anchors.push({ time: actualLineEnd, charIdx: line.text.length + extraChars });
      
      // Clean anchors: strictly increasing in time, non-decreasing in charIdx
      let cleanAnchors = [anchors[0]];
      for (let j = 1; j < anchors.length; j++) {
          const prev = cleanAnchors[cleanAnchors.length - 1];
          const curr = anchors[j];
          if (curr.time > prev.time) {
              if (curr.charIdx < prev.charIdx) curr.charIdx = prev.charIdx;
              cleanAnchors.push(curr);
          }
      }
      anchors = cleanAnchors;

      // Map chords using piecewise linear interpolation
      const mappedChords = lineChords.map(chord => {
         let targetCharIdx = 0;
         if (chord.time <= anchors[0].time) {
             targetCharIdx = anchors[0].charIdx;
         } else if (chord.time >= anchors[anchors.length - 1].time) {
             targetCharIdx = anchors[anchors.length - 1].charIdx;
         } else {
             for (let j = 0; j < anchors.length - 1; j++) {
                 if (chord.time >= anchors[j].time && chord.time <= anchors[j+1].time) {
                     const t0 = anchors[j].time;
                     const t1 = anchors[j+1].time;
                     const c0 = anchors[j].charIdx;
                     const c1 = anchors[j+1].charIdx;
                     const ratio = t1 === t0 ? 0 : (chord.time - t0) / (t1 - t0);
                     targetCharIdx = Math.floor(c0 + ratio * (c1 - c0));
                     break;
                 }
             }
         }
         return { chord: chord.chord, targetCharIdx, time: chord.time, ltrPos: 0 };
      });
      
      // Calculate max virtual length for correct LTR/RTL padding
      let maxVirtualLength = line.text.length;
      mappedChords.forEach(mc => {
          if (mc.targetCharIdx + mc.chord.length > maxVirtualLength) {
              maxVirtualLength = mc.targetCharIdx + mc.chord.length;
          }
      });
      
      // Calculate LTR absolute position for the string builder
      mappedChords.forEach(mc => {
          if (this.isHebrew) {
              mc.ltrPos = Math.max(0, maxVirtualLength - mc.targetCharIdx - mc.chord.length);
          } else {
              mc.ltrPos = mc.targetCharIdx;
          }
      });
      
      mappedChords.sort((a, b) => a.ltrPos - b.ltrPos);

      let chordText = '';
      let lastPos = 0;
      
      for (const mc of mappedChords) {
         let pos = mc.ltrPos;
         if (pos < lastPos) pos = lastPos;
         
         const spacesNeeded = pos - chordText.length;
         if (spacesNeeded > 0) {
            chordText += ' '.repeat(spacesNeeded);
         } else if (chordText.length > 0 && !chordText.endsWith(' ')) {
            chordText += ' ';
         }
         
         chordText += mc.chord;
         lastPos = chordText.length;
      }
      
      // For Hebrew, pad with trailing spaces up to maxVirtualLength to push right-aligned text leftward
      if (this.isHebrew && chordText.length < maxVirtualLength) {
          chordText += ' '.repeat(maxVirtualLength - chordText.length);
      }
      
      result.push({
        type: 'lyric',
        text: line.text,
        bars: lineBars,
        chordText: chordText,
        time: line.time,
        sectionBreakBefore: line.sectionBreak,
        sectionName: line.sectionName,
        rawLineIndex: line.rawLineIndex
      });
      
      // Gap / Instrumental
      if (nextLineTime !== Number.MAX_VALUE && actualLineEnd < nextLineTime) {
          const gapBars = getBarsUntil(nextLineTime);
          if (gapBars.length > 0) {
              result.push({
                  type: 'instrumental',
                  text: this.formatInstrumentalBars(gapBars),
                  bars: gapBars,
                  time: gapBars[0].time
              });
          }
      }
    }

    // 3. Outro instrumental
    const outroBars = [];
    while (barIdx < this.parsedBars.length) {
      outroBars.push(this.parsedBars[barIdx]);
      barIdx++;
    }
    if (outroBars.length > 0) {
      result.push({
        type: 'instrumental',
        text: this.formatInstrumentalBars(outroBars),
        bars: outroBars,
        time: outroBars[0].time
      });
    }

    this.syncedLines = result;
  }

  public onSyncedLineTextChange(index: number, event: any) {
    const newText = event.target.innerText.replace(/\n/g, '').trim();
    const line = this.syncedLines[index];
    if (line.text === newText || line.rawLineIndex === undefined || !this.lyricsData) return;
    
    line.text = newText;
    
    // Update the raw lyricsData
    const lines = this.lyricsData.split('\n');
    const rawLine = lines[line.rawLineIndex];
    // rawLine has format [MM:SS.xx] text...
    // Replace the text part while keeping the timestamp
    const tsMatch = rawLine.match(/^(\[\d{1,2}:\d{2}\.\d{1,3}\])\s*(.*)$/);
    if (tsMatch) {
      lines[line.rawLineIndex] = `${tsMatch[1]} ${newText}`;
      this.lyricsData = lines.join('\n');
      
      // Auto-save back to backend to keep files in sync
      this.saveEditedLyrics();
      if (this.audioPath) {
        this.apiService.saveSyncedSheet(this.audioPath, this.syncedLines);
      }
    }
  }

  public onSyncedChordTextChange(index: number, event: any) {
    // Preserve exact spacing for the chordText row
    const newText = event.target.innerText.replace(/\n/g, '').trimRight();
    const line = this.syncedLines[index];
    if (line.chordText === newText) return;
    
    line.chordText = newText;
    
    if (this.audioPath) {
      this.apiService.saveSyncedSheet(this.audioPath, this.syncedLines).then(res => {
        console.log('Synced sheet saved');
      });
    }
  }



  public onChordTextChange(beat: any, event: any) {
    let newChord = event.target.innerText.replace(/[\n\r]/g, '').trim();
    newChord = newChord.replace(/^\(|\)$/g, '').trim();
    
    if (newChord === '-' || newChord === '') {
      newChord = 'N';
    }
    
    if (beat.chord === newChord || !this.audioPath) {
      event.target.innerText = beat.chord === 'N' ? '-' : (beat.isRepeated ? `(${beat.chord})` : beat.chord);
      return;
    }
    
    beat.chord = newChord;
    
    // Flatten parsedBars back to flat beats array
    const flatBeats: any[] = [];
    this.parsedBars.forEach(bar => {
      flatBeats.push(...bar.beats);
    });
    
    // Auto-save back to backend to keep files in sync
    this.apiService.saveChords(this.audioPath, flatBeats).then(res => {
      console.log('Chords saved', res);
      // Re-evaluate repeated chords and re-render
      this.groupChordsByBar(flatBeats);
      this.buildSyncedSheet();
    });
  }

  private formatInstrumentalBars(bars: any[]): string {
    if (bars.length === 0) return '';
    let out = '// ';
    for (const bar of bars) {
      // Get unique non-N chords in this bar to display
      const uniqueChords = new Set<string>();
      for (const b of bar.beats) {
        if (b.chord !== 'N') uniqueChords.add(b.chord);
      }
      const chordStr = Array.from(uniqueChords).join(' ');
      if (chordStr) {
        out += chordStr + ' / ';
      } else {
        out += '- / ';
      }
    }
    out = out.replace(/ \/ $/, ' //');
    return out;
  }

  /**
   * Parse the raw lyrics text (with [MM:SS.xx] timestamps) into an array of timed lines.
   */
  private parseLyricLines(raw: string): { time: number, text: string, sectionBreak: boolean, sectionName: string | null, rawLineIndex: number }[] {
    const lines = raw.split('\n');
    const result: { time: number, text: string, sectionBreak: boolean, sectionName: string | null, rawLineIndex: number }[] = [];
    const tsRegex = /^\[(\d{1,2}):(\d{2})\.(\d{1,3})\]\s*(.*)$/;
    const sectionTagRegex = /^\[([^:\]]+)\]$/; // Matches [Verse] but NOT [00:15.00]
    let pendingSectionBreak = false;
    let pendingSectionName: string | null = null;

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const trimmed = line.trim();
      
      // Blank line = section break
      if (!trimmed) {
        pendingSectionBreak = true;
        continue;
      }

      const secMatch = trimmed.match(sectionTagRegex);
      if (secMatch) {
        pendingSectionBreak = true;
        pendingSectionName = secMatch[1].trim();
        continue;
      }
      
      const match = trimmed.match(tsRegex);
      if (match) {
        const mins = parseInt(match[1], 10);
        const secs = parseInt(match[2], 10);
        const frac = match[3].padEnd(3, '0');
        const ms = parseInt(frac, 10);
        const time = mins * 60 + secs + ms / 1000;
        const text = match[4].trim();
        if (text) {
          result.push({ time, text, sectionBreak: pendingSectionBreak, sectionName: pendingSectionName, rawLineIndex: i });
          pendingSectionBreak = false;
          pendingSectionName = null;
        }
      }
    }
    return result;
  }

  /**
   * Format instrumental chords grouped by bars into the `// C G / F / Am //` notation.
   */
  private formatInstrumentalBlock(chords: any[]): string {
    if (chords.length === 0) return '';
    
    // Group by bar number
    const barsMap = new Map<number, string[]>();
    for (const chord of chords) {
      const bar = chord.bar;
      if (!barsMap.has(bar)) {
        barsMap.set(bar, []);
      }
      const arr = barsMap.get(bar)!;
      // Only add if different from last chord in this bar
      if (arr.length === 0 || arr[arr.length - 1] !== chord.chord) {
        arr.push(chord.chord);
      }
    }

    const barStrings = Array.from(barsMap.values()).map(chds => chds.join(' '));
    return '// ' + barStrings.join(' / ') + ' //';
  }
  public async exportToSongbook() {
    if (!this.audioPath || this.syncedLines.length === 0) return;
    this.isExporting = true;
    try {
      const payload = {
        title: this.songTitle,
        artist: this.songArtist,
        syncedLines: this.syncedLines,
        isHebrew: this.isHebrew
      };
      const res = await this.apiService.exportToSongbook(payload);
      if (res && res.message) {
        alert(res.message);
      } else {
        alert('Successfully exported to Songbook!');
      }
    } catch (err: any) {
      console.error('Export failed', err);
      alert(err.message || 'Failed to export to Songbook. Check console.');
    } finally {
      this.isExporting = false;
    }
  }
}

// Types
export interface LyricChunk {
  text: string;
  chord?: string;
}

export interface SyncedLine {
  type: 'lyric' | 'instrumental';
  text: string;
  bars: any[];
  chordText?: string;
  time: number;
  sectionBreakBefore?: boolean;
  sectionName?: string | null;
  rawLineIndex?: number;
}
