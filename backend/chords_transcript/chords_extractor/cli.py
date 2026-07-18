import argparse
import sys
import os
import json
from chords_extractor.extractor import extract_chords
from chords_extractor.visualizer import generate_html

def main():
    parser = argparse.ArgumentParser(
        description="SDK tool to extract and fuse chord predictions from multiple models (Librosa, Essentia, Madmom) using an HMM Viterbi decoder."
    )
    parser.add_argument(
        "audio_file",
        help="Path to the input audio file (.wav)"
    )
    parser.add_argument(
        "-o", "--output",
        help="Path to save the output JSON. Defaults to <audio_basename>_chords.json in the audio folder."
    )
    parser.add_argument(
        "--essentia",
        help="Path to custom Essentia streaming_extractor_music.exe binary. If omitted, uses the prepackaged binary."
    )

    args = parser.parse_args()

    # Determine output file path
    if not args.output:
        base, _ = os.path.splitext(args.audio_file)
        output_path = f"{base}_chords.json"
    else:
        output_path = args.output

    print("==========================================================", file=sys.stderr)
    print("      HMM VITERBI CHORD EXTRACTOR SDK RUNNER", file=sys.stderr)
    print("==========================================================", file=sys.stderr)
    print(f"Input Audio: {args.audio_file}", file=sys.stderr)
    print(f"Output JSON: {output_path}", file=sys.stderr)

    try:
        print("-> Running chord transcription & Viterbi fusion pipeline...", file=sys.stderr)
        result = extract_chords(args.audio_file, essentia_bin_path=args.essentia)
        
        # Save JSON output
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
            
        # Generate HTML output
        html_path = os.path.splitext(output_path)[0] + ".html"
        generate_html(result, html_path)
            
        print("==========================================================", file=sys.stderr)
        print("SUCCESS: Chord extraction and HMM Viterbi fusion completed!", file=sys.stderr)
        print(f"BPM: {result['metadata']['bpm']} | Total Beats: {result['metadata']['total_beats']} | Total Bars: {result['metadata']['total_bars']}", file=sys.stderr)
        print(f"Chords saved to: {output_path} and {html_path}", file=sys.stderr)
        print("==========================================================", file=sys.stderr)
        
    except Exception as e:
        print(f"\nERROR: Extraction failed: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
