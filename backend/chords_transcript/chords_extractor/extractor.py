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
import subprocess
import librosa
import soundfile as sf

# Now import madmom after monkeypatching collections and numpy
import madmom
from madmom.processors import SequentialProcessor
from madmom.features.chords import CNNChordFeatureProcessor, CRFChordRecognitionProcessor
from madmom.audio.signal import Signal

def run_essentia_extractor(bin_path, audio_path):
    """Runs the native Essentia command-line music extractor on Windows."""
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
        # Silently log warning to stderr
        print(f"Warning: could not delete temp json {out_json_path}: {e}", file=sys.stderr)

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
            print(f"Warning: Error reading frames file {out_frames_path}: {e}", file=sys.stderr)
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
        print(f"Warning: Essentia Viterbi smoothing skipped ({e})", file=sys.stderr)
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
        # Tie breaker
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
    hop_length = 512
    chroma_full = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=hop_length)
    
    cqt_bass = np.abs(librosa.cqt(y=y, sr=sr, fmin=32.7, n_bins=36, hop_length=hop_length))
    chroma_bass = np.zeros((12, cqt_bass.shape[1]))
    for p in range(12):
        chroma_bass[p, :] = np.sum(cqt_bass[p::12, :], axis=0)

    roots = ['C', 'C#', 'D', 'Eb', 'E', 'F', 'F#', 'G', 'Ab', 'A', 'Bb', 'B']
    qualities = ['', 'm', '7', 'maj7', 'm7', 'dim', 'aug', 'sus4', 'sus2']
    
    states = []
    for root in roots:
        for q in qualities:
            states.append(f"{root}{q}")
            
    templates = []
    quality_notes = {
        '': [0, 4, 7], 'm': [0, 3, 7], '7': [0, 4, 7, 10], 'maj7': [0, 4, 7, 11],
        'm7': [0, 3, 7, 10], 'dim': [0, 3, 6], 'aug': [0, 4, 8], 'sus4': [0, 5, 7], 'sus2': [0, 2, 7]
    }
    
    quality_weights = {
        '': 1.0, 'm': 1.0, '7': 0.85, 'maj7': 0.80, 'm7': 0.80,
        'dim': 0.40, 'aug': 0.40, 'sus4': 0.80, 'sus2': 0.80
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

    n_beats = len(beat_intervals)
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

    norm_chroma = chroma_beats / (np.linalg.norm(chroma_beats, axis=0) + 1e-9)
    raw_sim = np.dot(templates, norm_chroma)
    
    raw_chords = []
    for idx in range(n_beats):
        energy = np.sum(chroma_beats[:, idx])
        if energy < 1e-3:
            raw_chords.append('N')
            continue
            
        weighted_sims = raw_sim[:, idx] * state_weights
        state_idx = np.argmax(weighted_sims)
        sim = raw_sim[state_idx, idx]
        
        if sim < 0.35:
            raw_chords.append('N')
        else:
            raw_chords.append(states[state_idx])

    smoothed_chords = running_mode(raw_chords, 3)

    final_chords = []
    for idx, chord in enumerate(smoothed_chords):
        if chord == 'N':
            final_chords.append('N')
            continue
            
        if len(chord) >= 2 and chord[0:2] in ['C#', 'Eb', 'F#', 'Ab', 'Bb']:
            root_note = chord[0:2]
            quality = chord[2:]
        else:
            root_note = chord[0]
            quality = chord[1:]
            
        chord_tones = get_chord_notes(root_note, quality)
        
        bass_vec = bass_beats[:, idx]
        norm_bass = bass_vec / (np.linalg.norm(bass_vec) + 1e-9)
        bass_idx = np.argmax(norm_bass)
        bass_note = roots[bass_idx]
        bass_energy = np.max(bass_vec)
        
        if (bass_note != root_note and 
            bass_note in chord_tones and 
            bass_energy > 0.18 and 
            norm_bass[bass_idx] > 0.40):
            
            slash_chord = f"{chord}/{bass_note}"
            final_chords.append(normalize_chord(slash_chord))
        else:
            final_chords.append(normalize_chord(chord))
            
    return final_chords

def estimate_time_signature_and_offset(y, sr, beat_times):
    """Estimate time signature (3 or 4) and downbeat offset using onset energy."""
    if len(beat_times) < 4:
        return 4, 0
    
    # Get frame indices for each beat
    beat_frames = librosa.time_to_frames(beat_times, sr=sr)
    onset_env = librosa.onset.onset_strength(y=y, sr=sr)
    
    best_score = 0
    best_ts = 4
    best_offset = 0

    for ts in [3, 4]:
        for offset in range(ts):
            downbeat_indices = [i for i in range(offset, len(beat_frames), ts)]
            downbeat_frames = beat_frames[downbeat_indices]
            # Ensure frames don't exceed onset_env bounds
            valid_frames = [f for f in downbeat_frames if f < len(onset_env)]
            if not valid_frames:
                continue
            
            score = np.mean(onset_env[valid_frames])
            if score > best_score:
                best_score = score
                best_ts = ts
                best_offset = offset
                
    return best_ts, best_offset

def parse_madmom_chord(madmom_str):
    if madmom_str == 'N':
        return 'N'
    parts = madmom_str.split(':')
    root = parts[0]
    if len(parts) > 1:
        qual = parts[1]
        if qual == 'maj':
            return root
        elif qual == 'min':
            return f"{root}m"
        elif qual == 'dim':
            return f"{root}dim"
        elif qual == 'aug':
            return f"{root}aug"
        elif qual == 'maj7':
            return f"{root}maj7"
        elif qual == 'min7':
            return f"{root}m7"
        elif qual == '7':
            return f"{root}7"
    return root

def run_madmom_chordrec(y_44k):
    """Runs the Madmom CNN feature extractor + CRF sequence decoder on the 44.1kHz waveform."""
    y_float = y_44k.astype(np.float32)
    sig = Signal(y_float, sample_rate=44100)
    
    featproc = CNNChordFeatureProcessor()
    decode = CRFChordRecognitionProcessor()
    chordrec = SequentialProcessor([featproc, decode])
    
    segments = chordrec(sig)
    return segments

def map_madmom_segments_to_beats(madmom_segments, beat_intervals):
    """Maps Madmom segments to beat intervals using maximum temporal overlap."""
    beat_chords = []
    
    for start, end in beat_intervals:
        best_label = 'N'
        max_overlap = -1.0
        
        for seg_start, seg_end, label in madmom_segments:
            overlap = max(0.0, min(end, seg_end) - max(start, seg_start))
            if overlap > max_overlap:
                max_overlap = overlap
                best_label = label
                
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

def get_triad_equivalent(chord_label):
    """Convert a rich chord state (e.g. Cmaj7, Cdim) to its closest triad (C or Cm)."""
    if chord_label == 'N' or not chord_label:
        return 'N'
        
    base_chord = chord_label.split('/')[0] if '/' in chord_label else chord_label
    
    if len(base_chord) >= 2 and base_chord[0:2] in ['C#', 'Eb', 'F#', 'Ab', 'Bb']:
        root = base_chord[0:2]
        quality = base_chord[2:]
    else:
        root = base_chord[0]
        quality = base_chord[1:]
        
    if quality in ['', '7', 'maj7', 'aug', 'sus4', 'sus2']:
        return root
    elif quality in ['m', 'm7', 'dim']:
        return f"{root}m"
        
    return root

def compute_emission_prob(o_lib, o_ess, o_mad, s_state, config=None):
    """Calculate P(o_lib, o_ess, o_mad | s_state) assuming conditional independence."""
    if config is None:
        config = {}
        
    e_weights = config.get("emission_weights", {})
    p_ess_match = e_weights.get("essentia_match", 0.85)
    p_lib_match = e_weights.get("librosa_match", 0.40)
    p_mad_match = e_weights.get("madmom_match", 0.45)
    p_none_bias = e_weights.get("none_state_bias", 0.80)

    o_lib_base = o_lib.split('/')[0] if '/' in o_lib else o_lib
    
    if s_state == 'N':
        p_lib = p_none_bias if o_lib_base == 'N' else (1.0 - p_none_bias) / 108
    else:
        if o_lib_base == s_state:
            p_lib = p_lib_match
        elif o_lib_base != 'N' and o_lib_base[0] == s_state[0]:
            p_lib = 0.15 / 8
        else:
            p_lib = 0.10 / 107

    triad_s = get_triad_equivalent(s_state)
    if triad_s == 'N':
        p_ess = p_none_bias if o_ess == 'N' else (1.0 - p_none_bias) / 108
    else:
        o_ess_base = o_ess.split('/')[0] if '/' in o_ess else o_ess
        if o_ess_base == triad_s:
            p_ess = p_ess_match
        else:
            p_ess = 0.10 / 23

    if triad_s == 'N':
        p_mad = 0.85 if o_mad == 'N' else 0.15 / 24
    else:
        if o_mad == triad_s:
            p_mad = 0.45
        elif o_mad != 'N' and o_mad[0] == triad_s[0]:
            p_mad = 0.12
        else:
            p_mad = 0.08 / 23

    return p_lib * p_ess * p_mad

def build_transition_matrix(states, config=None):
    """Build a transition matrix A reflecting Circle of Fifths and self-transition bias."""
    if config is None:
        config = {}
        
    t_probs = config.get("transition_probabilities", {})
    p_self = t_probs.get("self_transition", 0.75)
    p_same_root = t_probs.get("same_root_diff_quality", 0.05)
    p_circle_1 = t_probs.get("circle_of_fifths_dist_1", 0.04)
    p_circle_2 = t_probs.get("circle_of_fifths_dist_2", 0.01)
    p_unrelated = t_probs.get("unrelated_chord", 0.001)

    n_states = len(states)
    A = np.zeros((n_states, n_states))
    
    root_to_fifths = {
        'C': 0, 'G': 1, 'D': 2, 'A': 3, 'E': 4, 'B': 5, 'F#': 6, 'C#': 7, 'Ab': 8, 'Eb': 9, 'Bb': 10, 'F': 11
    }
    
    def circle_distance(r1, r2):
        if r1 not in root_to_fifths or r2 not in root_to_fifths:
            return 6
        d = abs(root_to_fifths[r1] - root_to_fifths[r2])
        return min(d, 12 - d)

    def parse_state(s):
        if s == 'N':
            return 'N', ''
        if len(s) >= 2 and s[0:2] in ['C#', 'Eb', 'F#', 'Ab', 'Bb']:
            return s[0:2], s[2:]
        return s[0], s[1:]

    for i, s_i in enumerate(states):
        for j, s_j in enumerate(states):
            if s_i == 'N' or s_j == 'N':
                if s_i == s_j:
                    A[i, j] = p_self
                else:
                    A[i, j] = (1.0 - p_self) / (n_states - 1)
                continue

            r_i, q_i = parse_state(s_i)
            r_j, q_j = parse_state(s_j)

            if s_i == s_j:
                A[i, j] = p_self
            elif r_i == r_j:
                A[i, j] = p_same_root
            else:
                dist = circle_distance(r_i, r_j)
                if dist == 1:
                    A[i, j] = p_circle_1
                elif dist == 2:
                    A[i, j] = p_circle_2
                else:
                    A[i, j] = p_unrelated

        A[i, :] /= np.sum(A[i, :])
        
    return A

def viterbi_fusion(librosa_preds, essentia_preds, madmom_preds, states, A, config=None):
    """Run dynamic programming Viterbi decoder in log-space to decode the true chord sequence."""
    T = len(librosa_preds)
    N = len(states)
    
    state_to_idx = {s: i for i, s in enumerate(states)}
    
    # Initialize pi array. If N is None state, give it high init prob.
    log_pi = np.full(N, np.log(0.15 / 108))
    log_pi[state_to_idx['N']] = np.log(0.85)
    
    viterbi = np.zeros((T, N))
    backpointer = np.zeros((T, N), dtype=int)
    log_A = np.log(A + 1e-100)
    
    # Initialize t=0
    o_lib, o_ess, o_mad = librosa_preds[0], essentia_preds[0], madmom_preds[0]
    for i, s in enumerate(states):
        p_em = compute_emission_prob(o_lib, o_ess, o_mad, s, config)
        viterbi[0, i] = log_pi[i] + np.log(p_em + 1e-100)
        
    # Forward pass
    for t in range(1, T):
        o_lib, o_ess, o_mad = librosa_preds[t], essentia_preds[t], madmom_preds[t]
        log_emissions = np.zeros(N)
        for i, s in enumerate(states):
            log_emissions[i] = np.log(compute_emission_prob(o_lib, o_ess, o_mad, s, config) + 1e-100)
            
        for j in range(N):
            temp = viterbi[t-1, :] + log_A[:, j]
            best_state = np.argmax(temp)
            viterbi[t, j] = temp[best_state] + log_emissions[j]
            backpointer[t, j] = best_state
            
    # Backtrack
    best_path_pointer = np.argmax(viterbi[T-1, :])
    best_path = [best_path_pointer]
    for t in range(T-1, 0, -1):
        best_path_pointer = backpointer[t, best_path_pointer]
        best_path.append(best_path_pointer)
        
    best_path.reverse()
    decoded_chords = [states[idx] for idx in best_path]
    
    # Post-processing: Slash chord recovery
    final_fused = []
    for f_ch, lib_ch in zip(decoded_chords, librosa_preds):
        if '/' in lib_ch:
            base_lib, bass = lib_ch.split('/')
            if f_ch == base_lib:
                final_fused.append(f"{f_ch}/{bass}")
                continue
        final_fused.append(f_ch)
        
    return final_fused

def extract_chords(audio_path, essentia_bin_path=None, manual_offset=None, config=None):
    """
    Main SDK extraction API: Loads audio, runs models, performs Viterbi fusion,
    and returns a structured dict of the final combined predictions.
    Allows manual_offset to override the heuristic downbeat detection.
    Allows passing a config dict to tune the Viterbi mathematical parameters.
    """
    if not os.path.exists(audio_path):
        raise FileNotFoundError(f"Audio file not found at: {audio_path}")

    # Discover packaged Essentia binary relative to this package if not specified
    if not essentia_bin_path:
        package_dir = os.path.dirname(os.path.abspath(__file__))
        essentia_bin_path = os.path.join(package_dir, "essentia", "bin", "streaming_extractor_music.exe")

    # 1. Load Audio info
    info = sf.info(audio_path)
    duration = info.duration
    sr_file = info.samplerate

    # 2. Load Audio waveforms
    y_librosa, sr_load = librosa.load(audio_path, sr=None)
    y_madmom, _ = librosa.load(audio_path, sr=44100)

    # 3. Beat and Downbeat Tracking using Madmom
    bpm = 120.0
    beats = []
    ts = 4
    estimated_offset = 0
    madmom_beat_success = False
    
    try:
        from madmom.features.beats import RNNBeatProcessor, DBNBeatTrackingProcessor
        from madmom.audio.signal import Signal
        
        y_float = y_madmom.astype(np.float32)
        sig = Signal(y_float, sample_rate=44100)
        
        proc = RNNBeatProcessor()
        act = proc(sig)
        
        processor = DBNBeatTrackingProcessor(fps=100)
        madmom_beats = processor(act)
        beats = [float(b) for b in madmom_beats]
        
        if len(beats) >= 4:
            beat_diffs = np.diff(beats)
            bpm = 60.0 / np.median(beat_diffs) if len(beat_diffs) > 0 else 120.0
            madmom_beat_success = True
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Warning: Madmom beat extraction failed: {e}", file=sys.stderr)

    # 4. Run Essentia native extractor for Chords and Key
    estimated_key = "Unknown"
    essentia_available = False
    smoothed_chords = []

    try:
        data, frames_data = run_essentia_extractor(essentia_bin_path, audio_path)
        essentia_available = True
        
        # If Madmom failed, fallback to Essentia beats
        if not madmom_beat_success:
            rhythm = data.get("rhythm", {})
            bpm = float(rhythm.get("bpm", 120.0))
            beats = rhythm.get("beats_position", [])
            
        key_edma = data.get("tonal", {}).get("key_edma", {})
        if key_edma:
            estimated_key = f"{key_edma.get('key', '')} {key_edma.get('scale', '')}"
            
        time_step = 2048.0 / 44100.0
        chords_val = frames_data.get("tonal", {}).get("chords_progression", [])
        if not chords_val:
            chords_val = data.get("tonal", {}).get("chords_progression", [])
            
        if chords_val:
            smoothed_chords = smooth_essentia_chords(chords_val)
    except Exception as e:
        print(f"Warning: Essentia extraction skipped. ({e})", file=sys.stderr)

    # Fallback beat tracker if both Madmom and Essentia failed
    if not beats or len(beats) < 4:
        tempo, lib_beats = librosa.beat.beat_track(y=y_librosa, sr=sr_load)
        beats = list(librosa.frames_to_time(lib_beats, sr=sr_load))
        bpm = float(tempo[0]) if isinstance(tempo, np.ndarray) else float(tempo)

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

    beat_intervals = [(beats_extended[i], beats_extended[i+1]) for i in range(len(beats_extended)-1)]

    # 5. Extract raw engine predictions
    # Librosa
    librosa_chords = get_clean_librosa_beat_chords(y_librosa, sr_load, beat_intervals)

    # Essentia
    if essentia_available and len(smoothed_chords) > 0:
        time_step = 2048.0 / 44100.0
        essentia_chords = map_essentia_frames_to_beats(smoothed_chords, time_step, beat_intervals)
    else:
        essentia_chords = ['N'] * len(beat_intervals)

    # Madmom
    try:
        madmom_segments = run_madmom_chordrec(y_madmom)
        madmom_chords = map_madmom_segments_to_beats(madmom_segments, beat_intervals)
    except Exception as e:
        print(f"Warning: Madmom extraction failed ({e}). Falling back to N.", file=sys.stderr)
        madmom_chords = ['N'] * len(beat_intervals)

    # 6. HMM Viterbi Fusion
    roots_list = ['C', 'C#', 'D', 'Eb', 'E', 'F', 'F#', 'G', 'Ab', 'A', 'Bb', 'B']
    qualities_list = ['', 'm', '7', 'maj7', 'm7', 'dim', 'aug', 'sus4', 'sus2']
    
    hmm_states = []
    for r in roots_list:
        for q in qualities_list:
            hmm_states.append(f"{r}{q}")
    hmm_states.append('N')
    
    transition_matrix = build_transition_matrix(hmm_states, config)
    fused_chords = viterbi_fusion(librosa_chords, essentia_chords, madmom_chords, hmm_states, transition_matrix, config)

    # 7. Format SDK Output dictionary
    ts, estimated_offset = estimate_time_signature_and_offset(y_librosa, sr_load, np.array([start for start, end in beat_intervals]))
    
    # Allow manual override of the offset
    if manual_offset is not None:
        offset = manual_offset
        print(f"Using manual downbeat offset: {offset}")
    else:
        offset = estimated_offset
    
    total_beats = len(beat_intervals)
    total_bars = (total_beats - offset + ts - 1) // ts if total_beats > offset else 1
    
    beats_list = []
    for idx, (start, end) in enumerate(beat_intervals):
        if idx < offset:
            # Pickup measure
            measure = 0
            beat_idx = ts - (offset - idx) + 1
        else:
            measure = (idx - offset) // ts + 1
            beat_idx = (idx - offset) % ts + 1
            
        beats_list.append({
            "beat_index": idx + 1,
            "bar": measure,
            "beat_in_bar": beat_idx,
            "start_time": round(start, 4),
            "end_time": round(end, 4),
            "chord": fused_chords[idx],
            "librosa_chord": librosa_chords[idx],
            "essentia_chord": essentia_chords[idx],
            "madmom_chord": madmom_chords[idx]
        })
        
    output = {
        "metadata": {
            "audio_file": os.path.basename(audio_path),
            "bpm": round(bpm, 2),
            "estimated_key": estimated_key,
            "duration": round(duration, 2),
            "total_beats": total_beats,
            "total_bars": total_bars
        },
        "beats": beats_list
    }
    
    return output
