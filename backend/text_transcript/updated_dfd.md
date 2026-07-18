# Updated System Architecture (Data Flow Diagram)

Here is the updated logic flow reflecting the new manual lyrics auto-detection and language-specific model routing.

```mermaid
flowchart TD
    %% Inputs
    AudioInput([Audio File\n`lyrics_data/audios/Song.wav`]) --> ExtractMetadata

    %% Metadata Extraction
    subgraph PreProcessing [1. Pre-Processing]
        ExtractMetadata[Parse Filename\nArtist - Title]
    end

    %% Manual File Check
    subgraph CheckManual [2a. Manual File Check]
        CheckManualDir{Check `lyrics_data/manual_lyrics/`\nExists `Song.txt`?}
    end

    ExtractMetadata --> CheckManualDir

    %% Web Fetching
    subgraph FetchLyrics [2b. Official Lyrics Fetching]
        SearchLRClib[LRClib API Search\nExact: Artist + Title]
        FuzzyLRClib[LRClib Fuzzy Fallback\nTitle + Fuzzy Artist]
        WebScrape[Web Scraper\nDDG -> Genius.com]
        
        SearchLRClib -- Success --> HasLyrics
        SearchLRClib -- Fail --> FuzzyLRClib
        FuzzyLRClib -- Success --> HasLyrics
        FuzzyLRClib -- Fail --> WebScrape
        WebScrape -- Success --> HasLyrics
        WebScrape -- Fail --> NoLyrics(No Lyrics Found)
    end
    
    CheckManualDir -- No --> SearchLRClib
    CheckManualDir -- Yes --> HasLyrics(Official Lyrics Found)

    %% Audio Transcription
    subgraph Transcription [3. Audio Transcription (faster-whisper)]
        DetectLang{Language\nDetection?}
        ModelHE[Load: ivrit-ai/\nwhisper-large-v3-turbo-ct2]
        ModelEN[Load: large-v3]
        
        HasLyrics --> DetectLang
        NoLyrics --> DetectLang
        
        DetectLang -- 'he' --> ModelHE
        DetectLang -- 'en' / other --> ModelEN
    end

    %% Processing & Alignment
    subgraph PostProcessing [4. Post-Processing & Alignment]
        Structure[Structure Injection\nIdentify Verse/Chorus]
        WordAlign[Word Interpolation\nAlign Official Text to Timestamps]
        RawFormat[Format Raw Output\nPhonetic Whisper Text]
        
        HasLyrics -.-> Structure
        ModelHE -- Word Timestamps --> WordAlign
        ModelEN -- Word Timestamps --> WordAlign
        Structure --> WordAlign
        
        ModelHE -- Segment Timestamps --> RawFormat
        ModelEN -- Segment Timestamps --> RawFormat
        NoLyrics -.-> RawFormat
    end

    %% Outputs
    SyncedLRC([Perfectly Synced LRC\n`lyrics_data/final_results/Song.txt`])
    RawLRC([Raw/Hallucinated LRC\n`lyrics_data/final_results/Song.txt`])

    WordAlign --> SyncedLRC
    RawFormat --> RawLRC
    
    style HasLyrics fill:#d4edda,stroke:#28a745,stroke-width:2px
    style NoLyrics fill:#f8d7da,stroke:#dc3545,stroke-width:2px
    style SyncedLRC fill:#cce5ff,stroke:#0056b3,stroke-width:2px
    style RawLRC fill:#fff3cd,stroke:#856404,stroke-width:2px
```
