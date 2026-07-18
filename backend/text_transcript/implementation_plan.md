# Architecture Update & Optimization

We are updating the system architecture to deeply integrate language-specific ASR models (Ivrit.ai for Hebrew, Whisper for English), standardize the project's folder structure, and dramatically improve the user experience for manual lyric entry.

## Proposed Changes

### 1. Folder Restructuring
We will enforce a standardized directory structure at the root of the project:
- `audios/` (For all `.wav` or `.mp3` inputs)
- `manual_lyrics/` (Where users can save `.txt` files containing manual lyrics)
- `final_results/` (Where the final perfectly synced `.txt` files will be saved)

#### [MODIFY] [extract.py](file:///c:/Develop/Github/text_transcript/extract.py)
Update the CLI script to automatically look for input and save outputs to these new directories.

### 2. Streamlined Manual Lyrics Entry
#### [MODIFY] [extract.py](file:///c:/Develop/Github/text_transcript/extract.py)
If the web search (LRClib) completely fails, the script will:
1. **Auto-check** the `manual_lyrics/` folder to see if a file with the **exact same name** as the audio file exists (e.g., if audio is `Song.wav`, it looks for `manual_lyrics/Song.txt`).
2. If found, the script skips the rest of the web search.
3. The script proceeds to **Step 3 (Transcription)** to generate the required audio timestamps using the ASR model, and then to **Step 4 (Alignment)** to synchronize the manual text with the timestamps.

### 3. Language-Specific ASR Engine Routing
#### [MODIFY] [core.py](file:///c:/Develop/Github/text_transcript/lyric_extractor/core.py)
We will refactor the inner engine to use `faster-whisper`.
- If `lang == "he"` (or auto-detected as Hebrew): The engine will automatically load `ivrit-ai/whisper-large-v3-turbo-ct2`.
- If `lang == "en"` (or auto-detected as English): The engine will load standard OpenAI Whisper `large-v3` (or `base`).

### 4. New System DFD & Documentation
#### [NEW] [README.md](file:///c:/Develop/Github/text_transcript/README.md)
I will write a comprehensive, professional README explaining how the tool works, the folder structure, and detailing the internal architecture.

#### [NEW] [updated_dfd.md](file:///c:/Develop/Github/text_transcript/updated_dfd.md)
I will create a fresh Mermaid diagram showing the new bifurcated language flow and the manual file auto-detection step.

## Verification Plan
1. **English Test:** Process an English song to ensure it routes to standard Whisper and completes fast.
2. **Hebrew Test:** Process a Hebrew song to ensure it routes to `Ivrit.ai-turbo` and produces highly accurate results.
3. **Manual Entry Test:** Place a `.txt` file in `manual_lyrics/` and verify the script automatically detects it and perfectly aligns it.
