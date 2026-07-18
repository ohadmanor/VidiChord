import collections
import collections.abc
collections.MutableSequence = collections.abc.MutableSequence
collections.Sequence = collections.abc.Sequence

import numpy as np
np.float = float
np.int = int

import os
import sys
import json
import uuid
import hashlib
import datetime
import subprocess
import librosa
import soundfile as sf
from jinja2 import Template

# Now import madmom after monkeypatching collections and numpy
import madmom
from madmom.processors import SequentialProcessor
from madmom.features.chords import CNNChordFeatureProcessor, CRFChordRecognitionProcessor
from madmom.audio.signal import Signal

def get_audio_hash(audio_path):
    """Generate a unique short hash for the audio file to index local storage votes."""
    hasher = hashlib.md5()
    try:
        with open(audio_path, 'rb') as f:
            buf = f.read(65536)
            while len(buf) > 0:
                hasher.update(buf)
                buf = f.read(65536)
        return hasher.hexdigest()[:8]
    except Exception:
        return "default_audio"

def run_essentia_extractor(bin_path, audio_path):
    """Runs the native Essentia command-line music extractor on Windows."""
    print(f"-> Executing Essentia native extractor binary at: {bin_path}")
    if not os.path.exists(bin_path):
        raise FileNotFoundError(f"Essentia native binary not found at: {bin_path}")

    # Use unique filenames in the same folder to avoid clashes
    temp_dir = os.path.dirname(audio_path) or "."
    unique_id = uuid.uuid4().hex[:8]
    out_json_path = os.path.abspath(os.path.join(temp_dir, f"essentia_out_{unique_id}.json"))

    bin_dir = os.path.dirname(bin_path)
    env = os.environ.copy()
    if bin_dir not in env.get("PATH", ""):
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")

    cmd = [bin_path, audio_path, out_json_path]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding='utf-8', env=env)

    if result.returncode != 0:
        stderr_msg = (result.stderr or "").strip()
        raise RuntimeError(f"Essentia extractor failed (code {result.returncode}). stderr: {stderr_msg}")

    if not os.path.exists(out_json_path):
        raise FileNotFoundError("Essentia completed successfully but no output JSON was generated.")

    with open(out_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Clean up output JSON
    try:
        os.remove(out_json_path)
    except Exception as e:
        print(f"Warning: could not delete temp json {out_json_path}: {e}")

    # Load frames JSON output if it exists
    out_frames_path = out_json_path + "_frames"
    if not os.path.exists(out_frames_path) and os.path.exists(out_frames_path + ".json"):
        out_frames_path = out_frames_path + ".json"

    frames_data = {}
    if os.path.exists(out_frames_path):
        try:
            with open(out_frames_path, "r", encoding="utf-8") as f:
                frames_data = json.load(f)
        except Exception as e:
            print(f"Warning: Error reading frames file {out_frames_path}: {e}")
        finally:
            try:
                os.remove(out_frames_path)
            except Exception:
                pass

    return data, frames_data

def normalize_chord(chord_label):
    """Normalize enharmonics to preferred spellings according to user rules."""
    if not chord_label:
        return 'N'
    c = chord_label.replace('*', '').strip()
    if c == '-' or c == '' or c == 'N':
        return 'N'

    # Support slash chords (normalize both chord part and bass note part)
    chord_part = c
    bass_part = ''
    if '/' in c:
        parts = c.split('/')
        chord_part = parts[0]
        bass_part = parts[1]

    # Preferred spellings mappings
    mappings = {
        'Db': 'C#', 'Dbm': 'C#m', 'Db7': 'C#7', 'Dbmaj7': 'C#maj7', 'Dbm7': 'C#m7', 'Dbsus4': 'C#sus4', 'Dbsus2': 'C#sus2',
        'D#': 'Eb', 'D#m': 'Ebm', 'D#7': 'Eb7', 'D#maj7': 'Ebmaj7', 'D#m7': 'Ebm7', 'D#sus4': 'Ebsus4', 'D#sus2': 'Ebsus2',
        'Gb': 'F#', 'Gbm': 'F#m', 'Gb7': 'F#7', 'Gbmaj7': 'F#maj7', 'Gbm7': 'F#m7', 'Gbsus4': 'F#sus4', 'Gbsus2': 'F#sus2',
        'G#': 'Ab', 'G#m': 'Abm', 'G#7': 'Ab7', 'G#maj7': 'Abmaj7', 'G#m7': 'Abm7', 'G#sus4': 'Absus4', 'G#sus2': 'Absus2',
        'A#': 'Bb', 'A#m': 'Bbm', 'A#7': 'Bb7', 'A#maj7': 'Bbmaj7', 'A#m7': 'Bbm7', 'A#sus4': 'Bbsus4', 'A#sus2': 'Bbsus2'
    }

    bass_mappings = {
        'Db': 'C#', 'D#': 'Eb', 'Gb': 'F#', 'G#': 'Ab', 'A#': 'Bb'
    }

    norm_chord = mappings.get(chord_part, chord_part)
    norm_bass = bass_mappings.get(bass_part, bass_part)

    return f"{norm_chord}/{norm_bass}" if norm_bass else norm_chord

def get_chord_notes(root, quality):
    """Returns the note names belonging to a given root and quality."""
    roots = ['C', 'C#', 'D', 'Eb', 'E', 'F', 'F#', 'G', 'Ab', 'A', 'Bb', 'B']
    quality_offsets = {
        '': [0, 4, 7],
        'm': [0, 3, 7],
        '7': [0, 4, 7, 10],
        'maj7': [0, 4, 7, 11],
        'm7': [0, 3, 7, 10],
        'dim': [0, 3, 6],
        'aug': [0, 4, 8],
        'sus4': [0, 5, 7],
        'sus2': [0, 2, 7]
    }
    
    if root not in roots or quality not in quality_offsets:
        return []
        
    r_idx = roots.index(root)
    offsets = quality_offsets[quality]
    return [roots[(r_idx + offset) % 12] for offset in offsets]

def smooth_essentia_chords(chords_val):
    """Applies Viterbi smoothing to Essentia frame-level chords using Librosa."""
    try:
        chords_list = ['A', 'Am', 'Bb', 'Bbm', 'B', 'Bm', 'C', 'Cm', 'C#', 'C#m', 'D', 'Dm', 
                       'Eb', 'Ebm', 'E', 'Em', 'F', 'Fm', 'F#', 'F#m', 'G', 'Gm', 'Ab', 'Abm']
        chord_to_idx = {c: i for i, c in enumerate(chords_list)}
        n_states = len(chords_list)
        n_frames = len(chords_val)
        
        if n_frames > 0:
            prob = np.full((n_states, n_frames), 0.05 / (n_states - 1))
            for t, c in enumerate(chords_val):
                c_clean = c.replace('N', '')
                if c_clean in chord_to_idx:
                    prob[chord_to_idx[c_clean], t] = 0.95
                else:
                    prob[:, t] = 1.0 / n_states
                    
            transition = np.full((n_states, n_states), 0.05 / (n_states - 1))
            np.fill_diagonal(transition, 0.95)
            
            path = librosa.sequence.viterbi(prob, transition)
            return [chords_list[idx] for idx in path]
    except Exception as e:
        print(f"Warning: Viterbi smoothing skipped ({e})")
    return chords_val

def map_essentia_frames_to_beats(chords_val, time_step, beat_intervals):
    """Maps frame-level chord progression output from Essentia to beat intervals."""
    beat_chords = []
    from collections import Counter
    
    for start, end in beat_intervals:
        start_frame = int(round(start / time_step))
        end_frame = int(round(end / time_step))
        
        slice_chords = chords_val[start_frame:end_frame+1]
        slice_chords = [c for c in slice_chords if c]
        
        if not slice_chords:
            beat_chords.append('N')
            continue
            
        counter = Counter(slice_chords)
        most_common = counter.most_common(2)
        
        if most_common[0][0] == 'N' and len(most_common) > 1 and most_common[1][1] > len(slice_chords) * 0.3:
            beat_chords.append(normalize_chord(most_common[1][0]))
        else:
            beat_chords.append(normalize_chord(most_common[0][0]))
            
    return beat_chords

def running_mode(chords, window_size=3):
    from collections import Counter
    smoothed = []
    n = len(chords)
    half = window_size // 2
    for i in range(n):
        start = max(0, i - half)
        end = min(n, i + half + 1)
        sub = chords[start:end]
        c = Counter(sub)
        # Tie breaker: if tie, choose the chord of the current beat if it is one of the tied choices
        most_common = c.most_common()
        if len(most_common) > 1 and most_common[0][1] == most_common[1][1]:
            candidates = [item[0] for item in most_common if item[1] == most_common[0][1]]
            if chords[i] in candidates:
                smoothed.append(chords[i])
            else:
                smoothed.append(candidates[0])
        else:
            smoothed.append(most_common[0][0])
    return smoothed

def get_clean_librosa_beat_chords(y, sr, beat_intervals):
    """Extracts beat-level chords with running mode temporal smoothing and chord-tone bass filtering."""
    print("-> Librosa: Extracting CQT features...")
    hop_length = 512
    
    # 1. Full CQT chromagram for chord quality profiles
    chroma_full = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=hop_length)
    
    # 2. Low-frequency CQT magnitude for bass note tracking (32.7 Hz to 164.8 Hz, C1 to B3)
    cqt_bass = np.abs(librosa.cqt(y=y, sr=sr, fmin=32.7, n_bins=36, hop_length=hop_length))
    chroma_bass = np.zeros((12, cqt_bass.shape[1]))
    for p in range(12):
        chroma_bass[p, :] = np.sum(cqt_bass[p::12, :], axis=0)

    roots = ['C', 'C#', 'D', 'Eb', 'E', 'F', 'F#', 'G', 'Ab', 'A', 'Bb', 'B']
    qualities = ['', 'm', '7', 'maj7', 'm7', 'dim', 'aug', 'sus4', 'sus2']
    
    # 3. Define the templates for the 108 rich chord vocabulary states
    states = []
    for root in roots:
        for q in qualities:
            states.append(f"{root}{q}")
            
    n_states = len(states)
    n_beats = len(beat_intervals)
    
    # 4. Generate CQT templates for all states
    templates = []
    
    quality_notes = {
        '': [0, 4, 7],
        'm': [0, 3, 7],
        '7': [0, 4, 7, 10],
        'maj7': [0, 4, 7, 11],
        'm7': [0, 3, 7, 10],
        'dim': [0, 3, 6],
        'aug': [0, 4, 8],
        'sus4': [0, 5, 7],
        'sus2': [0, 2, 7]
    }
    
    # Weights to penalize rare/complex chord qualities and prevent noise
    quality_weights = {
        '': 1.0,        # Major
        'm': 1.0,       # Minor
        '7': 0.85,      # Dominant 7th
        'maj7': 0.80,   # Major 7th
        'm7': 0.80,     # Minor 7th
        'dim': 0.40,    # Diminished (penalized)
        'aug': 0.40,    # Augmented (penalized)
        'sus4': 0.80,    # Suspended 4
        'sus2': 0.80     # Suspended 2
    }
    
    state_weights = []
    for r_idx, root in enumerate(roots):
        for q in qualities:
            t = np.zeros(12)
            for offset in quality_notes[q]:
                t[(r_idx + offset) % 12] = 1.0
            t /= np.linalg.norm(t)
            templates.append(t)
            state_weights.append(quality_weights[q])
            
    templates = np.array(templates)
    state_weights = np.array(state_weights)

    # 5. Average chroma and bass vectors over each beat interval
    chroma_beats = np.zeros((12, n_beats))
    bass_beats = np.zeros((12, n_beats))
    
    for idx, (start, end) in enumerate(beat_intervals):
        start_frame = int(round(start * sr / hop_length))
        end_frame = int(round(end * sr / hop_length))
        start_frame = max(0, min(start_frame, chroma_full.shape[1] - 1))
        end_frame = max(0, min(end_frame, chroma_full.shape[1] - 1))

        if start_frame >= end_frame:
            chroma_beats[:, idx] = chroma_full[:, start_frame]
            bass_beats[:, idx] = chroma_bass[:, start_frame]
        else:
            chroma_beats[:, idx] = np.mean(chroma_full[:, start_frame:end_frame+1], axis=1)
            bass_beats[:, idx] = np.mean(chroma_bass[:, start_frame:end_frame+1], axis=1)

    # 6. Compute raw chord matches
    norm_chroma = chroma_beats / (np.linalg.norm(chroma_beats, axis=0) + 1e-9)
    raw_sim = np.dot(templates, norm_chroma)
    
    raw_chords = []
    for idx in range(n_beats):
        energy = np.sum(chroma_beats[:, idx])
        if energy < 1e-3:
            raw_chords.append('N')
            continue
            
        # Apply quality weights to the raw similarities
        weighted_sims = raw_sim[:, idx] * state_weights
        state_idx = np.argmax(weighted_sims)
        sim = raw_sim[state_idx, idx]
        
        # Threshold: if raw template matching similarity is below 0.35, classify as 'N'
        if sim < 0.35:
            raw_chords.append('N')
        else:
            raw_chords.append(states[state_idx])

    # 7. Apply Running Mode of size 3 (temporal noise smoothing)
    print("-> Librosa: Applying running mode temporal smoothing...")
    smoothed_chords = running_mode(raw_chords, 3)

    # 8. Post-process: Add slash chords with Chord-Tone Bass Filtering
    print("-> Librosa: Applying chord-tone bass filtering for slash chords...")
    final_chords = []
    
    for idx, chord in enumerate(smoothed_chords):
        if chord == 'N':
            final_chords.append('N')
            continue
            
        # Parse root and quality
        root_note = ''
        quality = ''
        
        # Check double-character roots first (C#, D#, F#, G#, A# or Eb, Ab, Bb)
        if len(chord) >= 2 and chord[0:2] in ['C#', 'Eb', 'F#', 'Ab', 'Bb']:
            root_note = chord[0:2]
            quality = chord[2:]
        else:
            root_note = chord[0]
            quality = chord[1:]
            
        # Get allowable bass notes (only notes belonging to the chord itself!)
        chord_tones = get_chord_notes(root_note, quality)
        
        # Analyze bass register
        bass_vec = bass_beats[:, idx]
        norm_bass = bass_vec / (np.linalg.norm(bass_vec) + 1e-9)
        bass_idx = np.argmax(norm_bass)
        bass_note = roots[bass_idx]
        
        bass_energy = np.max(bass_vec)
        
        # Enforce Chord-Tone Bass Constraint:
        # Bass note must be a chord tone, must differ from root, and have high register energy
        if (bass_note != root_note and 
            bass_note in chord_tones and 
            bass_energy > 0.18 and 
            norm_bass[bass_idx] > 0.40):
            
            slash_chord = f"{chord}/{bass_note}"
            final_chords.append(normalize_chord(slash_chord))
        else:
            final_chords.append(normalize_chord(chord))
            
    return final_chords

def run_madmom_chordrec(y_44k):
    """Runs the Madmom CNN feature extractor + CRF sequence decoder on the 44.1kHz waveform."""
    print("-> Madmom: Initializing CNN feature processor & CRF chord decoder...")
    # Madmom expects a float32 mono Signal object
    y_float = y_44k.astype(np.float32)
    sig = Signal(y_float, sample_rate=44100)
    
    featproc = CNNChordFeatureProcessor()
    decode = CRFChordRecognitionProcessor()
    chordrec = SequentialProcessor([featproc, decode])
    
    print("-> Madmom: Executing CNN-CRF deep chord model...")
    segments = chordrec(sig)
    print(f"-> Madmom: Decoded {len(segments)} segments.")
    return segments

def map_madmom_segments_to_beats(madmom_segments, beat_intervals):
    """Maps Madmom segments to beat intervals using maximum temporal overlap."""
    beat_chords = []
    
    for start, end in beat_intervals:
        best_label = 'N'
        max_overlap = -1.0
        
        for seg_start, seg_end, label in madmom_segments:
            # Calculate overlap length
            overlap = max(0.0, min(end, seg_end) - max(start, seg_start))
            if overlap > max_overlap:
                max_overlap = overlap
                best_label = label
                
        # Map Madmom label format (e.g. "C#:maj" -> "C#", "C#:min" -> "C#m", "N" -> "N")
        clean_label = 'N'
        if best_label and best_label != 'N':
            parts = best_label.split(':')
            root = parts[0]
            quality = parts[1] if len(parts) > 1 else 'maj'
            
            qual_map = {'maj': '', 'min': 'm'}
            mapped_qual = qual_map.get(quality, '')
            clean_label = f"{root}{mapped_qual}"
            
        beat_chords.append(normalize_chord(clean_label))
        
    return beat_chords

def main():
    audio_path = r"C:\Develop\Github\Text_Proj\test3.wav"
    bin_path = r"C:\Develop\Github\Audio-Chords-Lyrics\essentia\bin\streaming_extractor_music.exe"

    if len(sys.argv) > 1:
        audio_path = sys.argv[1]

    print("==========================================================")
    print("      3-WAY BEAT-LEVEL CHORD TRANSCRIPTION PIPELINE")
    print("==========================================================")
    print(f"Audio file: {audio_path}")
    
    if not os.path.exists(audio_path):
        print(f"Error: Audio file not found at {audio_path}")
        sys.exit(1)

    # 1. Load audio metadata
    print("-> Loading audio file metadata...")
    info = sf.info(audio_path)
    duration = info.duration
    sr_file = info.samplerate
    print(f"Duration: {duration:.2f} seconds | Native Sample Rate: {sr_file} Hz")

    # 2. Run Essentia native extractor to get beat locations
    bpm = 120.0
    beats = []
    estimated_key = "Unknown"
    essentia_available = False
    smoothed_chords = []

    try:
        data, frames_data = run_essentia_extractor(bin_path, audio_path)
        essentia_available = True
        
        rhythm = data.get("rhythm", {})
        bpm = float(rhythm.get("bpm", 120.0))
        beats = rhythm.get("beats_position", [])
        
        if bpm > 160.0:
            bpm /= 2.0
        elif bpm < 65.0:
            bpm *= 2.0
            
        key_edma = data.get("tonal", {}).get("key_edma", {})
        if key_edma:
            estimated_key = f"{key_edma.get('key', '')} {key_edma.get('scale', '')}"
            
        time_step = 2048.0 / 44100.0
        chords_val = frames_data.get("tonal", {}).get("chords_progression", [])
        if not chords_val:
            chords_val = data.get("tonal", {}).get("chords_progression", [])
            
        if chords_val:
            smoothed_chords = smooth_essentia_chords(chords_val)
        print("-> Essentia execution successful!")
    except Exception as e:
        print(f"Warning: Essentia extractor failed: {e}")
        print("-> Falling back to Librosa beat tracking...")
        data, frames_data = {}, {}
        smoothed_chords = []

    # 3. Load audio waveforms
    print("-> Loading audio waveforms for DSP processing...")
    # Load native sample rate for Librosa (CQT)
    y_librosa, sr_load = librosa.load(audio_path, sr=None)
    # Load 44.1kHz sample rate for Madmom to prevent internal ffmpeg resampling crashes
    y_madmom, _ = librosa.load(audio_path, sr=44100)

    # Fallback beat tracker if Essentia failed
    if not beats or len(beats) < 4:
        print("-> Running Librosa beat tracker...")
        tempo, lib_beats = librosa.beat.beat_track(y=y_librosa, sr=sr_load)
        beats = list(librosa.frames_to_time(lib_beats, sr=sr_load))
        bpm = float(tempo[0]) if isinstance(tempo, np.ndarray) else float(tempo)
        print(f"-> Librosa Estimated Tempo: {bpm:.1f} BPM")

    # Align beat grid starting at 0.0 and ending at duration
    beats_extended = list(beats)
    if beats_extended[0] > 0.2:
        beats_extended.insert(0, 0.0)
    else:
        beats_extended[0] = 0.0

    if beats_extended[-1] < duration - 0.2:
        beats_extended.append(duration)
    else:
        beats_extended[-1] = duration

    # Create beat intervals
    beat_intervals = [(beats_extended[i], beats_extended[i+1]) for i in range(len(beats_extended)-1)]
    print(f"-> Established {len(beat_intervals)} beats.")

    # 4. Run Model 1: Librosa (Clean Running Mode + Slash Chords)
    librosa_chords = get_clean_librosa_beat_chords(y_librosa, sr_load, beat_intervals)

    # 5. Run Model 2: Essentia beat-level chord mapping
    if essentia_available and len(smoothed_chords) > 0:
        time_step = 2048.0 / 44100.0
        essentia_chords = map_essentia_frames_to_beats(smoothed_chords, time_step, beat_intervals)
    else:
        essentia_chords = ['N'] * len(beat_intervals)

    # 6. Run Model 3: Madmom CNN-CRF beat-level chord mapping
    madmom_segments = run_madmom_chordrec(y_madmom)
    madmom_chords = map_madmom_segments_to_beats(madmom_segments, beat_intervals)

    # 7. Load ground truth if exists
    gt_filename = f"{os.path.splitext(os.path.basename(audio_path))[0]}_3way_beat_ground_truth.json"
    gt_path = os.path.join(os.path.dirname(audio_path), gt_filename)
    # If the file does not exist, also fallback to standard name for convenience
    if not os.path.exists(gt_path):
        gt_filename_fallback = f"{os.path.splitext(os.path.basename(audio_path))[0]}_beat_ground_truth.json"
        gt_path = os.path.join(os.path.dirname(audio_path), gt_filename_fallback)

    ground_truth = {}
    if os.path.exists(gt_path):
        try:
            with open(gt_path, "r", encoding="utf-8") as f:
                ground_truth = json.load(f)
            print(f"-> Loaded ground truth annotations from: {gt_path}")
        except Exception as e:
            print(f"Warning: Could not read ground truth file {gt_path}: {e}")

    # 8. Render HTML Report
    print("-> Rendering 3-way HTML visualizer report...")
    template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "beat_report_template_3way.html")
    if not os.path.exists(template_path):
        print(f"Error: HTML template not found at {template_path}")
        sys.exit(1)
        
    with open(template_path, "r", encoding="utf-8") as f:
        html_content = f.read()

    template = Template(html_content)
    
    # Prepare beat objects for rendering
    beats_data = []
    for idx, (start, end) in enumerate(beat_intervals):
        measure = idx // 4 + 1
        beat_idx = idx % 4 + 1
        beats_data.append({
            "id": f"{measure}.{beat_idx}",
            "measure": measure,
            "beat": beat_idx,
            "start": start,
            "end": end,
            "librosa": librosa_chords[idx],
            "essentia": essentia_chords[idx],
            "madmom": madmom_chords[idx]
        })

    audio_hash = get_audio_hash(audio_path)
    audio_filename = os.path.basename(audio_path)
    
    rendered_html = template.render(
        audio_filename=audio_filename,
        bpm=bpm,
        duration=duration,
        estimated_key=estimated_key,
        beats=beats_data,
        audio_hash=audio_hash,
        current_time=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ground_truth=ground_truth
    )

    # Write output HTML file
    out_html_filename = f"chord_beat_report_{os.path.splitext(os.path.basename(audio_path))[0]}_3way.html"
    out_html_path = os.path.join(os.path.dirname(audio_path), out_html_filename)
    with open(out_html_path, "w", encoding="utf-8") as f:
        f.write(rendered_html)
        
    print(f"SUCCESS: 3-way beat-level report generated successfully!")
    print(f"Please open the report in your web browser:")
    print(f"file:///{out_html_path.replace(os.sep, '/')}")
    print("==========================================================")

if __name__ == "__main__":
    main()
