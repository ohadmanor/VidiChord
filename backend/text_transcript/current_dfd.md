# Current System Architecture (Data Flow Diagram)

Here is the current logical flow of the Black-Box Lyric Extractor pipeline. Review this diagram to see exactly how audio files are processed today, and let me know which components you would like to change!

```mermaid
flowchart TD
    %% Inputs
    AudioInput([Audio File (.wav)]) --> ExtractMetadata
    ManualLyrics([Manual Lyrics File]) -.-> CheckManual

    %% Metadata Extraction
    subgraph PreProcessing [1. Pre-Processing]
        ExtractMetadata[Parse Filename\nArtist - Title]
    end

    %% Web Fetching
    subgraph FetchLyrics [2. Official Lyrics Fetching]
        CheckManual{Manual Lyrics\nProvided?}
        SearchLRClib[LRClib API Search\nExact: Artist + Title]
        FuzzyLRClib[LRClib Fuzzy Fallback\nTitle + Fuzzy Artist]
        WebScrape[Web Scraper\nDDG -> Genius.com]
        
        CheckManual -- Yes --> HasLyrics(Official Lyrics Found)
        CheckManual -- No --> SearchLRClib
        SearchLRClib -- Success --> HasLyrics
        SearchLRClib -- Fail --> FuzzyLRClib
        FuzzyLRClib -- Success --> HasLyrics
        FuzzyLRClib -- Fail --> WebScrape
        WebScrape -- Success --> HasLyrics
        WebScrape -- Fail --> NoLyrics(No Lyrics Found)
    end
    
    ExtractMetadata --> CheckManual

    %% Audio Transcription
    subgraph Transcription [3. Audio Transcription]
        WhisperASR[Whisper Model\nDefault: 'base' on CPU]
        
        HasLyrics --> WhisperASR
        NoLyrics --> WhisperASR
    end

    %% Processing & Alignment
    subgraph PostProcessing [4. Post-Processing & Alignment]
        Structure[Structure Injection\nIdentify Verse/Chorus]
        WordAlign[Word Interpolation\nAlign Official Text to Timestamps]
        RawFormat[Format Raw Output\nPhonetic Whisper Text]
        
        HasLyrics -.-> Structure
        WhisperASR -- Word Timestamps --> WordAlign
        Structure --> WordAlign
        
        WhisperASR -- Segment Timestamps --> RawFormat
        NoLyrics -.-> RawFormat
    end

    %% Outputs
    SyncedLRC([Perfectly Synced & Structured LRC])
    RawLRC([Raw/Hallucinated Whisper LRC])

    WordAlign --> SyncedLRC
    RawFormat --> RawLRC
    
    style HasLyrics fill:#d4edda,stroke:#28a745,stroke-width:2px
    style NoLyrics fill:#f8d7da,stroke:#dc3545,stroke-width:2px
    style SyncedLRC fill:#cce5ff,stroke:#0056b3,stroke-width:2px
    style RawLRC fill:#fff3cd,stroke:#856404,stroke-width:2px
```

### Key Components to Consider Changing
Based on our experiments, here are the current bottlenecks in this DFD:
1. **The Web Scraper (`WebScrape`)** only works for English (Genius). Sites like Shironet block the scraper with a WAF.
2. **The Whisper Model (`WhisperASR`)** defaults to `base`, which fails severely on Hebrew when `NoLyrics` is reached.
