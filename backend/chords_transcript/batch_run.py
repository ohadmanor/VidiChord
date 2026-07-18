import os
import sys
import glob
import json
import argparse
from chords_extractor.extractor import extract_chords
from chords_extractor.visualizer import generate_html

def main():
    if sys.platform.startswith('win'):
        try:
            sys.stdout.reconfigure(encoding='utf-8')
            sys.stderr.reconfigure(encoding='utf-8')
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Batch process audio files for chord extraction.")
    parser.add_argument("audios_dir", nargs='?', default="audios", help="Directory containing .wav files")
    parser.add_argument("--offset", type=int, default=None, help="Manual downbeat offset to override the heuristic")
    parser.add_argument("--config", type=str, default="config.json", help="Path to config.json for Viterbi statistical parameters")
    args = parser.parse_args()

    config_dict = None
    if os.path.exists(args.config):
        try:
            with open(args.config, "r", encoding="utf-8") as c:
                config_dict = json.load(c)
            print(f"Loaded config from {args.config}")
        except Exception as e:
            print(f"Failed to load config {args.config}: {e}", file=sys.stderr)
    else:
        print(f"No config file found at {args.config}. Using default parameters.")

    target_path = args.audios_dir

    if not os.path.exists(target_path):
        print(f"Error: path '{target_path}' does not exist.", file=sys.stderr)
        sys.exit(1)
        
    if os.path.isfile(target_path) and target_path.lower().endswith(".wav"):
        wav_files = [target_path]
    else:
        wav_files = glob.glob(os.path.join(target_path, "*.wav"))
        
    if not wav_files:
        print(f"No .wav files found in '{target_path}'.", file=sys.stderr)
        sys.exit(0)
        
    print(f"Found {len(wav_files)} WAV files to process:")
    for f in wav_files:
        print(f" - {f}")
        
    for wav_file in wav_files:
        output_json = os.path.splitext(wav_file)[0] + "_chords.json"
        print(f"\nProcessing {os.path.basename(wav_file)}...")
        try:
            result = extract_chords(wav_file, manual_offset=args.offset, config=config_dict)
            with open(output_json, "w", encoding="utf-8") as out:
                json.dump(result, out, indent=2)
                
            output_html = os.path.splitext(wav_file)[0] + "_chords.html"
            generate_html(result, output_html)
            
            print(f"SUCCESS: Saved to {os.path.basename(output_json)} and {os.path.basename(output_html)}")
            print(f"BPM: {result['metadata']['bpm']} | Beats: {result['metadata']['total_beats']} | Key: {result['metadata']['estimated_key']}")
        except Exception as e:
            print(f"ERROR processing {wav_file}: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
