# Black-Box Lyric Extractor

The Black-Box Lyric Extractor is an intelligent pipeline that takes an audio file (e.g. `.wav`) and outputs perfectly formatted, structured, and timestamped `.lrc` lyrics. 

It handles multiple languages, automatically pulling the optimal AI model based on the language (e.g., standard `Whisper` for English, and `Ivrit.ai-Turbo` for Hebrew).

## Project Structure
- `lyrics_data/audios/`: Place all your input audio files (`.wav`, `.mp3`) here.
- `lyrics_data/manual_lyrics/`: If the auto-fetcher cannot find lyrics on the internet, you can paste the plain lyrics into a `.txt` file here. Ensure it has the exact same name as the audio file (e.g., `lyrics_data/audios/Song1.wav` -> `lyrics_data/manual_lyrics/Song1.txt`).
- `lyrics_data/final_results/`: All perfectly aligned, final timestamped lyrics will be saved here automatically.

## How it Works
1. **Metadata Extraction:** The script parses the filename (e.g., `Artist - Title.wav`) to get search keywords.
2. **Official Fetching:** It queries LRClib and the web (DuckDuckGo + Genius) to find the clean, official lyrics.
3. **Manual Override:** If it finds a `.txt` file in `lyrics_data/manual_lyrics/` matching the song name, it completely bypasses the web search and uses your manual text.
4. **Transcription:** It uses `faster-whisper` to process the audio. It runs `ivrit-ai/whisper-large-v3-turbo-ct2` for Hebrew, and standard OpenAI `large-v3` (or `base`) for English.
5. **Alignment (Word Interpolation):** It uses the Whisper timestamps and aligns them directly to the clean official/manual text, injecting verse and chorus structures seamlessly.

## Usage
Simply run the extractor on an audio file in your `lyrics_data/audios` folder:
```bash
python extract.py "lyrics_data/audios/Song Title.wav"
```

If you wish to force the language rather than relying on auto-detect:
```bash
python extract.py "lyrics_data/audios/Song Title.wav" --lang he
```
