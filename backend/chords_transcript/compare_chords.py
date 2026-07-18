import os
import sys
import json
import uuid
import hashlib
import datetime
import subprocess
import numpy as np
import librosa
import soundfile as sf
from jinja2 import Template

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
    
    # Run the process
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding='utf-8', env=env)

    if result.returncode != 0:
        stderr_msg = (result.stderr or "").strip()
        raise RuntimeError(f"Essentia extractor failed (code {result.returncode}). stderr: {stderr_msg}")

    if not os.path.exists(out_json_path):
        raise FileNotFoundError("Essentia completed successfully but no output JSON was generated.")

    # Read output JSON
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

def get_librosa_chords(y, sr, bar_intervals):
    """Extracts chords for each bar interval using CQT chromagram template-matching."""
    print("-> Extracting chords via Librosa (CQT Template Matching)...")
    hop_length = 512
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=hop_length)

    roots = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
    templates = {}
    
    # Generate templates
    for r_idx, root in enumerate(roots):
        # Major: root (0), major third (4), perfect fifth (7)
        t_maj = np.zeros(12)
        t_maj[r_idx] = 1.0
        t_maj[(r_idx + 4) % 12] = 1.0
        t_maj[(r_idx + 7) % 12] = 1.0
        t_maj /= np.linalg.norm(t_maj)
        templates[root] = t_maj

        # Minor: root (0), minor third (3), perfect fifth (7)
        t_min = np.zeros(12)
        t_min[r_idx] = 1.0
        t_min[(r_idx + 3) % 12] = 1.0
        t_min[(r_idx + 7) % 12] = 1.0
        t_min /= np.linalg.norm(t_min)
        templates[root + 'm'] = t_min

    bar_chords = []
    for start, end in bar_intervals:
        start_frame = int(round(start * sr / hop_length))
        end_frame = int(round(end * sr / hop_length))

        start_frame = max(0, min(start_frame, chroma.shape[1] - 1))
        end_frame = max(0, min(end_frame, chroma.shape[1] - 1))

        if start_frame >= end_frame:
            chroma_avg = chroma[:, start_frame]
        else:
            chroma_avg = np.mean(chroma[:, start_frame:end_frame+1], axis=1)

        # Silence or noise detection
        if np.sum(chroma_avg) < 1e-3 or np.var(chroma_avg) < 1e-4:
            bar_chords.append('N')
            continue

        best_chord = 'N'
        best_sim = -1.0
        norm_chroma = chroma_avg / (np.linalg.norm(chroma_avg) + 1e-9)

        for chord_name, template in templates.items():
            sim = np.dot(norm_chroma, template)
            if sim > best_sim:
                best_sim = sim
                best_chord = chord_name

        if best_sim < 0.45:
            bar_chords.append('N')
        else:
            bar_chords.append(best_chord)

    return bar_chords

def get_madmom_chords(audio_path, bar_intervals):
    """Attempts to use the Madmom package for chord recognition; returns None if not installed."""
    try:
        from madmom.features.chords import CNNChordFeatureProcessor, CRFChordRecognitionProcessor
        print("-> Extracting chords via Madmom (Neural Net + CRF)...")
        feat_proc = CNNChordFeatureProcessor()
        decode = CRFChordRecognitionProcessor()
        features = feat_proc(audio_path)
        chord_segments = decode(features)

        bar_chords = []
        for start, end in bar_intervals:
            best_chord = 'N'
            max_duration = 0.0

            for seg in chord_segments:
                seg_start = seg['start']
                seg_end = seg['end']
                seg_label = seg['label']

                overlap_start = max(start, seg_start)
                overlap_end = min(end, seg_end)
                overlap = max(0.0, overlap_end - overlap_start)

                if overlap > max_duration:
                    max_duration = overlap
                    best_chord = seg_label

            # Clean and map labels
            clean_chord = best_chord
            if ':' in clean_chord:
                root, quality = clean_chord.split(':')
                if quality == 'maj':
                    clean_chord = root
                elif quality == 'min':
                    clean_chord = root + 'm'
            elif clean_chord == 'N':
                clean_chord = 'N'

            bar_chords.append(clean_chord)
        return bar_chords
    except ImportError:
        return None

def simulate_madmom_chords(base_chords):
    """Generates musically logical simulated chord transcriptions to mimic a third engine."""
    print("-> Madmom Python library not installed. Generating simulated fallback chords...")
    simulated = []
    relative_minors = {
        'C': 'Am', 'C#': 'A#m', 'D': 'Bm', 'D#': 'Cm', 'E': 'C#m', 'F': 'Dm',
        'F#': 'D#m', 'G': 'Em', 'G#': 'Fm', 'A': 'F#m', 'A#': 'Gm', 'B': 'G#m'
    }
    relative_majors = {v: k for k, v in relative_minors.items()}
    
    # Seed based on content length for consistency
    import random
    random.seed(len(base_chords) + 12345)
    
    for chord in base_chords:
        if chord == 'N' or not chord:
            simulated.append('N')
            continue
            
        rand = random.random()
        if rand < 0.12:  # Switch to relative minor/major
            if chord in relative_minors:
                simulated.append(relative_minors[chord])
            elif chord in relative_majors:
                simulated.append(relative_majors[chord])
            else:
                simulated.append(chord)
        elif rand < 0.20:  # Switch to dominant fifth
            roots = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
            is_minor = chord.endswith('m')
            root_part = chord[:-1] if is_minor else chord
            if root_part in roots:
                idx = roots.index(root_part)
                fifth_idx = (idx + 7) % 12
                fifth_chord = roots[fifth_idx] + ('m' if is_minor else '')
                simulated.append(fifth_chord)
            else:
                simulated.append(chord)
        else:
            simulated.append(chord)
            
    return simulated

def map_essentia_frames_to_bars(chords_val, time_step, bar_intervals):
    """Maps frame-level chord progression output from Essentia to bar intervals."""
    bar_chords = []
    from collections import Counter
    
    for start, end in bar_intervals:
        start_frame = int(round(start / time_step))
        end_frame = int(round(end / time_step))
        
        slice_chords = chords_val[start_frame:end_frame+1]
        slice_chords = [c for c in slice_chords if c]
        
        if not slice_chords:
            bar_chords.append('N')
            continue
            
        counter = Counter(slice_chords)
        most_common = counter.most_common(2)
        
        # If 'N' is dominant but another music chord occupies >30%, prefer the chord
        if most_common[0][0] == 'N' and len(most_common) > 1 and most_common[1][1] > len(slice_chords) * 0.3:
            bar_chords.append(most_common[1][0])
        else:
            bar_chords.append(most_common[0][0])
            
    return bar_chords

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
                # strip minor sign formatting variation if any
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

def main():
    audio_path = r"C:\Develop\Github\Text_Proj\test.wav"
    bin_path = r"C:\Develop\Github\Audio-Chords-Lyrics\essentia\bin\streaming_extractor_music.exe"

    if len(sys.argv) > 1:
        audio_path = sys.argv[1]
    if len(sys.argv) > 2:
        bin_path = sys.argv[2]

    print("==========================================================")
    print("      CHORD TRANSCRIPTION COMPARISON PROJECT")
    print("==========================================================")
    print(f"Audio file: {audio_path}")
    
    if not os.path.exists(audio_path):
        print(f"Error: Audio file not found at {audio_path}")
        sys.exit(1)

    # Load audio metadata
    print("-> Loading audio file metadata...")
    info = sf.info(audio_path)
    duration = info.duration
    sr = info.samplerate
    print(f"Duration: {duration:.2f} seconds | Sample Rate: {sr} Hz | Channels: {info.channels}")

    # Run Essentia
    bpm = 120.0
    beats = []
    estimated_key = "Unknown"
    essentia_available = False
    essentia_chords = []

    try:
        data, frames_data = run_essentia_extractor(bin_path, audio_path)
        essentia_available = True
        
        rhythm = data.get("rhythm", {})
        bpm = float(rhythm.get("bpm", 120.0))
        beats = rhythm.get("beats_position", [])
        
        # Octave correction
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
            # We map frames later when bars are established
        else:
            smoothed_chords = []
            
        print("-> Essentia execution successful!")
    except Exception as e:
        print(f"Warning: Essentia extractor failed or not configured correctly: {e}")
        print("-> Falling back to Librosa for beat/bar tracking...")
        data, frames_data = {}, {}
        smoothed_chords = []

    # Load full audio array for Librosa processing
    print("-> Loading audio waveforms for DSP processing...")
    y, sr_load = librosa.load(audio_path, sr=None)

    # Establish bar boundaries
    # If beats were loaded from Essentia, use them; otherwise extract using Librosa
    if not beats or len(beats) < 4:
        print("-> Extracting beats using Librosa tracker...")
        tempo, lib_beats = librosa.beat.beat_track(y=y, sr=sr_load)
        beats = list(librosa.frames_to_time(lib_beats, sr=sr_load))
        bpm = float(tempo[0]) if isinstance(tempo, np.ndarray) else float(tempo)
        print(f"-> Librosa Estimated Tempo: {bpm:.1f} BPM")

    # Group beats into bars (assuming 4/4 meter)
    bar_intervals = []
    num_beats = len(beats)
    bar_beats = 4
    
    # First bar starts at 0.0
    for i in range(0, num_beats, bar_beats):
        start_t = beats[i]
        # End at the start of next bar, or audio end
        if i + bar_beats < num_beats:
            end_t = beats[i + bar_beats]
        else:
            end_t = duration
            
        if len(bar_intervals) == 0:
            # Stretch first bar to start at 0.0 to capture intro
            bar_intervals.append((0.0, end_t))
        else:
            bar_intervals.append((start_t, end_t))

    # Catch edge case of no beats detected
    if not bar_intervals:
        # Divide into arbitrary 3-second segments
        seg_len = 3.0
        num_segs = int(np.ceil(duration / seg_len))
        bar_intervals = [(i * seg_len, min((i+1) * seg_len, duration)) for i in range(num_segs)]

    print(f"-> Established {len(bar_intervals)} bars.")

    # 1. Librosa Chord Extraction
    librosa_chords = get_librosa_chords(y, sr_load, bar_intervals)

    # 2. Essentia Chord Mapping (if available)
    if essentia_available and len(smoothed_chords) > 0:
        essentia_chords = map_essentia_frames_to_bars(smoothed_chords, time_step, bar_intervals)
    else:
        print("-> Essentia chords unavailable. Filling with fallback values...")
        essentia_chords = ['N'] * len(bar_intervals)

    # 3. Madmom Chord Extraction
    madmom_chords = get_madmom_chords(audio_path, bar_intervals)
    madmom_is_simulated = False
    
    if madmom_chords is None:
        # Fallback to simulation using Essentia/Librosa chords as guide
        reference_chords = essentia_chords if essentia_available else librosa_chords
        madmom_chords = simulate_madmom_chords(reference_chords)
        madmom_is_simulated = True
    else:
        print("-> Madmom execution successful!")

    # Standardize empty chords and clean strings
    def clean(label):
        if not label or label == 'N':
            return 'N'
        return label

    librosa_chords = [clean(c) for c in librosa_chords]
    essentia_chords = [clean(c) for c in essentia_chords]
    madmom_chords = [clean(c) for c in madmom_chords]

    # Console display
    print("\n" + "="*70)
    print(f"{'BAR':<6} | {'TIME RANGE':<16} | {'LIBROSA':<10} | {'ESSENTIA':<10} | {'MADMOM':<10}")
    print("="*70)
    
    for idx, (start, end) in enumerate(bar_intervals):
        b_lbl = f"Bar {idx+1}"
        t_lbl = f"{start:.2f}s - {end:.2f}s"
        l_c = librosa_chords[idx]
        e_c = essentia_chords[idx]
        m_c = madmom_chords[idx]
        
        # Mark simulator in console
        m_c_display = f"{m_c}*" if madmom_is_simulated else m_c
        print(f"{b_lbl:<6} | {t_lbl:<16} | {l_c:<10} | {e_c:<10} | {m_c_display:<10}")
        
    print("="*70)
    if madmom_is_simulated:
        print("(*) Madmom output is simulated because the library is not installed in Python.")
    print("==========================================================\n")

    # Load ground truth if exists
    gt_filename = f"{os.path.splitext(os.path.basename(audio_path))[0]}_ground_truth.json"
    gt_path = os.path.join(os.path.dirname(audio_path), gt_filename)
    ground_truth = {}
    if os.path.exists(gt_path):
        try:
            with open(gt_path, "r", encoding="utf-8") as f:
                ground_truth = json.load(f)
            print(f"-> Loaded ground truth annotations from: {gt_path}")
        except Exception as e:
            print(f"Warning: Could not read ground truth file {gt_path}: {e}")

    def normalize_chord(chord_label):
        if not chord_label:
            return 'N'
        c = chord_label.replace('*', '').strip()
        if c == '-' or c == '' or c == 'N':
            return 'N'
        mappings = {
            'Db': 'C#', 'Dbm': 'C#m',
            'D#': 'Eb', 'D#m': 'Ebm',
            'Gb': 'F#', 'Gbm': 'F#m',
            'G#': 'Ab', 'G#m': 'Abm',
            'A#': 'Bb', 'A#m': 'Bbm'
        }
        return mappings.get(c, c)

    # Print accuracy scoreboard in console if ground truth exists
    if ground_truth:
        librosa_matches = 0
        essentia_matches = 0
        madmom_matches = 0
        annotated_count = 0
        
        for idx in range(len(bar_intervals)):
            bar_key = str(idx + 1)
            if bar_key in ground_truth:
                gt_chord = normalize_chord(ground_truth[bar_key])
                annotated_count += 1
                if normalize_chord(librosa_chords[idx]) == gt_chord:
                    librosa_matches += 1
                if normalize_chord(essentia_chords[idx]) == gt_chord:
                    essentia_matches += 1
                if normalize_chord(madmom_chords[idx]) == gt_chord:
                    madmom_matches += 1
                    
        print("\n" + "="*50)
        print("          ACCURACY EVALUATION SUMMARY")
        print("="*50)
        print(f"Annotated Bars: {annotated_count} / {len(bar_intervals)}")
        if annotated_count > 0:
            print(f"Librosa Accuracy : {librosa_matches/annotated_count*100:5.1f}% ({librosa_matches}/{annotated_count})")
            print(f"Essentia Accuracy: {essentia_matches/annotated_count*100:5.1f}% ({essentia_matches}/{annotated_count})")
            print(f"Madmom Accuracy  : {madmom_matches/annotated_count*100:5.1f}% ({madmom_matches}/{annotated_count})")
        else:
            print("No bars evaluated yet. Use the HTML report to add ground truth chords!")
        print("="*50 + "\n")

    # Generate HTML Report
    print("-> Rendering HTML visualizer report...")
    
    # Read the HTML template
    template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report_template.html")
    if not os.path.exists(template_path):
        print(f"Error: HTML template not found at {template_path}")
        sys.exit(1)
        
    with open(template_path, "r", encoding="utf-8") as f:
        html_content = f.read()

    template = Template(html_content)
    
    # Prepare bar objects for rendering
    bars_data = []
    for idx, (start, end) in enumerate(bar_intervals):
        bars_data.append({
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
        bars=bars_data,
        audio_hash=audio_hash,
        current_time=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        madmom_simulated=madmom_is_simulated,
        ground_truth=ground_truth
    )

    # Write output HTML file
    out_html_filename = f"chord_comparison_report_{os.path.splitext(os.path.basename(audio_path))[0]}.html"
    out_html_path = os.path.join(os.path.dirname(audio_path), out_html_filename)
    with open(out_html_path, "w", encoding="utf-8") as f:
        f.write(rendered_html)
        
    print(f"SUCCESS: Comparison report generated successfully!")
    print(f"Please open the report in your web browser:")
    print(f"file:///{out_html_path.replace(os.sep, '/')}")
    print("==========================================================")

if __name__ == "__main__":
    main()
