import os
import sys
import argparse

# Add SDK to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), 'lyric_extractor')))
from lyric_extractor import LyricExtractor

def main():
    sys.stdout.reconfigure(encoding='utf-8')
    
    # Parse CLI Arguments
    parser = argparse.ArgumentParser(
        description="Black-box Lyric Extractor: Converts WAV audio files directly into structured, timestamped lyrics."
    )
    parser.add_argument("audio_path", help="Path to the input WAV/MP3 audio file. (e.g. lyrics_data/audios/song.wav)")
    parser.add_argument(
        "-o", "--output", 
        help="Path to save the output lyric text file. (Defaults to lyrics_data/final_results/<filename>_lyrics.txt)"
    )
    parser.add_argument(
        "-l", "--lyrics", 
        help="Path to an optional text file containing clean official lyrics. (Auto-detects from lyrics_data/manual_lyrics/ folder)"
    )
    parser.add_argument(
        "--lang", 
        help="Optional language code (e.g. 'en', 'he') to force Whisper to use. Defaults to auto-detect."
    )
    parser.add_argument(
        "--title", 
        help="Manually specify the track title (skips automated audio recognition)."
    )
    parser.add_argument(
        "--artist", 
        help="Manually specify the artist name (skips automated audio recognition)."
    )
    
    args = parser.parse_args()
    
    if not os.path.exists(args.audio_path):
        print(f"Error: Audio file not found at '{args.audio_path}'", file=sys.stderr)
        sys.exit(1)
        
    print("="*60)
    print("BLACK-BOX LYRIC EXTRACTOR")
    print("="*60)
    
    # 1. Initialize the SDK Extractor
    print("Initializing Extractor Engine...")
    extractor = LyricExtractor()
    
    # 2. Black-Box Transcription and Alignment
    manual_text = None
    base_name = os.path.splitext(os.path.basename(args.audio_path))[0]
    
    # 2a. Check explicit --lyrics flag
    if args.lyrics:
        if not os.path.exists(args.lyrics):
            print(f"Warning: Lyrics file not found at '{args.lyrics}'. Falling back.", file=sys.stderr)
        else:
            print(f"Loading official lyrics from '{args.lyrics}'...")
            with open(args.lyrics, "r", encoding="utf-8") as f:
                manual_text = f.read()
    
    # 2b. Auto-detect from lyrics_data/manual_lyrics/ folder
    if not manual_text:
        auto_manual = os.path.join("lyrics_data", "manual_lyrics", f"{base_name}.txt")
        if os.path.exists(auto_manual):
            print(f"Auto-detected manual lyrics file at '{auto_manual}'!")
            with open(auto_manual, "r", encoding="utf-8") as f:
                manual_text = f.read()

    print(f"Running black-box extraction on '{args.audio_path}'...")
    try:
        res = extractor.extract_black_box(
            args.audio_path,
            language=args.lang,
            manual_lyrics=manual_text,
            force_title=args.title,
            force_artist=args.artist
        )
        print(f"Extraction complete. Language: {res['language']} | Source: {res['source']}")
    except Exception as e:
        print(f"Error during black-box extraction: {e}", file=sys.stderr)
        sys.exit(1)
        
    # 3. Save Final Output
    if args.output:
        out_path = args.output
    else:
        # Default output folder: lyrics_data/final_results/
        output_dir = os.path.join("lyrics_data", "final_results")
        os.makedirs(output_dir, exist_ok=True)
        base_name = os.path.splitext(os.path.basename(args.audio_path))[0]
        out_path = os.path.join(output_dir, f"{base_name}_lyrics.txt")
        
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            # Metadata headers
            f.write(f"Title: {res['metadata']['title']}\n")
            f.write(f"Artist: {res['metadata']['artist']}\n")
            f.write(f"Language: {res['language']}\n")
            f.write(f"Source: {res['source']}\n\n")
            f.write(res['text'])
            f.write("\n")
            
        print("="*60)
        print(f"SUCCESS: Lyric file successfully saved to:")
        print(f"  {out_path}")
        print("="*60)
        
    except Exception as e:
        print(f"Error saving output file: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
