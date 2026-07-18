import os
import difflib

class LyricExtractor:
    """
    A reusable SDK module to load audio files
    and extract timestamped lyrics using faster-whisper.
    """
    def __init__(self, model_name=None):
        self.model_name = model_name
        self._model = None

    def _get_model(self, lang):
        # Determine which model to load based on language
        if lang == "he":
            target_model = "ivrit-ai/whisper-large-v3-turbo-ct2"
        else:
            # Default for English and others
            target_model = "large-v3"
            
        # If the user forced a model name via __init__, use it
        if self.model_name:
            target_model = self.model_name
            
        # If the requested model is different from the cached one, reload
        if self._model is None or getattr(self, '_current_model_name', None) != target_model:
            from faster_whisper import WhisperModel
            print(f"Loading faster-whisper model '{target_model}' on CPU...")
            self._model = WhisperModel(target_model, device="cpu", compute_type="int8")
            self._current_model_name = target_model
            
        return self._model

    def format_timestamp(self, seconds):
        minutes = int(seconds // 60)
        seconds_rem = seconds % 60
        return f"[{minutes:02d}:{seconds_rem:05.2f}]"

    def extract_lyrics(self, filepath, language=None):
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"Audio file not found: {filepath}")

        # If language is None, we need to detect it.
        # But we don't know the language yet, so we load the default model (large-v3)
        model = self._get_model(language or "en")
        
        kwargs = {}
        if language:
            kwargs['language'] = language
            
        print(f"Transcribing '{os.path.basename(filepath)}' (this may take time on CPU)...")
        segments_gen, info = model.transcribe(filepath, word_timestamps=True, **kwargs)
        detected_lang = info.language
        
        # If we didn't know it was Hebrew, but it is, we should switch to Ivrit.ai!
        if language is None and detected_lang == "he":
            print(f"Detected Hebrew audio. Switching to optimized Ivrit.ai model...")
            model = self._get_model("he")
            segments_gen, info = model.transcribe(filepath, language="he", word_timestamps=True)
            
        # Extract segments
        segments = []
        full_text = []
        for segment in segments_gen:
            start = segment.start
            end = segment.end
            text = segment.text.strip()
            full_text.append(text)
            
            words_list = []
            if hasattr(segment, 'words') and segment.words:
                for w in segment.words:
                    words_list.append({
                        'word': w.word.strip(),
                        'start': w.start,
                        'end': w.end
                    })
                    
            segments.append({
                'start': start,
                'end': end,
                'timestamp': self.format_timestamp(start),
                'text': text,
                'words': words_list
            })
            
        return {
            'language': detected_lang,
            'text': "\n".join(full_text),
            'segments': segments
        }

    def align_lyrics(self, raw_segments, official_lyrics_text):
        """
        Aligns clean official lyrics text with raw Whisper segments to assign timestamps.
        Uses word-count interpolation within each segment for sub-segment precision.

        :param raw_segments: List of dicts with keys 'start', 'end', 'text', 'timestamp'.
        :param official_lyrics_text: String containing the clean lyrics lines.
        :return: String containing the aligned and structured lyrics.
        """
        # Split official lyrics into lines and strip whitespace
        official_lines = [line.strip() for line in official_lyrics_text.split('\n')]
        official_lines = [line for line in official_lines if line]

        # 1. Flatten Whisper segments into words using exact word_timestamps if available,
        #    otherwise fallback to INTERPOLATED timestamps.
        raw_words = []
        for segment in raw_segments:
            if 'words' in segment and segment['words']:
                for w in segment['words']:
                    raw_words.append({
                        'word': w['word'].strip(',.?!":()[]{}').lower(),
                        'time': w['start'],
                        'timestamp': self.format_timestamp(w['start']),
                        'end_time': w['end']
                    })
            else:
                words = segment['text'].split()
                n = max(len(words), 1)
                seg_start = segment.get('start', 0.0)
                seg_end = segment.get('end', seg_start)
                seg_dur = seg_end - seg_start
                for pos, w in enumerate(words):
                    interp_time = seg_start + (pos / n) * seg_dur
                    raw_words.append({
                        'word': w.strip(',.?!":()[]{}').lower(),
                        'time': interp_time,
                        'timestamp': self.format_timestamp(interp_time),
                        'end_time': interp_time + (seg_dur / n)
                    })

        # 2. Flatten official lyrics into words, tracking line index (skip structure tags)
        official_words = []
        word_to_line_idx = []
        for line_idx, line in enumerate(official_lines):
            if line.startswith('[') and line.endswith(']'):
                continue
            for w in line.split():
                official_words.append(w.strip(',.?!":()[]{}').lower())
                word_to_line_idx.append(line_idx)

        # 3. Use SequenceMatcher to find matching word subsequences
        matcher = difflib.SequenceMatcher(
            None,
            [rw['word'] for rw in raw_words],
            official_words
        )

        # 4. Assign the interpolated timestamp of the first matched word in each line
        line_timestamps = [None] * len(official_lines)
        for block in matcher.get_matching_blocks():
            raw_start, official_start, length = block
            for i in range(length):
                line_idx = word_to_line_idx[official_start + i]
                if line_timestamps[line_idx] is None:
                    line_timestamps[line_idx] = raw_words[raw_start + i]['timestamp']

        # 5. Forward-fill any lines that didn't get a direct match
        last_known = "[00:00.00]"
        for idx in range(len(official_lines)):
            if official_lines[idx].startswith('[') and official_lines[idx].endswith(']'):
                continue
            if line_timestamps[idx] is not None:
                last_known = line_timestamps[idx]
            else:
                line_timestamps[idx] = last_known

        # 5.5. Resolve duplicate timestamps: space consecutive same-timestamp
        # content lines evenly between their shared time and the next distinct time.
        def ts_to_sec(ts):
            """Parse '[MM:SS.xx]' into float seconds."""
            ts = ts.strip('[]')
            m, s = ts.split(':')
            return int(m) * 60 + float(s)

        content_indices = [i for i, ln in enumerate(official_lines)
                           if not (ln.startswith('[') and ln.endswith(']'))]
        i = 0
        while i < len(content_indices):
            ci = content_indices[i]
            t_cur = ts_to_sec(line_timestamps[ci])
            # Find end of run with the same timestamp
            j = i
            while j + 1 < len(content_indices) and \
                    line_timestamps[content_indices[j + 1]] == line_timestamps[ci]:
                j += 1
            if j > i:  # there's a run of duplicates
                run_len = j - i + 1
                # Determine end time: next distinct timestamp, or +2s per line default
                if j + 1 < len(content_indices):
                    t_next = ts_to_sec(line_timestamps[content_indices[j + 1]])
                else:
                    t_next = t_cur + run_len * 2.0
                gap = (t_next - t_cur) / run_len
                for k in range(run_len):
                    line_timestamps[content_indices[i + k]] = self.format_timestamp(
                        t_cur + k * gap
                    )
            i = j + 1

        # 6. Rebuild the final structured, timestamped lyrics
        aligned_lyrics = []
        for idx, line in enumerate(official_lines):
            if line.startswith('[') and line.endswith(']'):
                aligned_lyrics.append(f"\n{line}")
            else:
                aligned_lyrics.append(f"{line_timestamps[idx]} {line}")

        return "\n".join(aligned_lyrics).strip()

    def parse_synced_lyrics(self, synced_lrc, plain_lyrics_text):
        """
        Parse an LRC-format synced lyrics string into a timestamped output.
        Maps the timestamps from synced_lrc 1:1 to the text lines in plain_lyrics_text.
        Preserves any structure tags (e.g. [Verse 1]) present in plain_lyrics_text.

        :param synced_lrc: String in LRC format: '[MM:SS.xx] Line text\n...'
        :param plain_lyrics_text: Plain lyrics optionally containing section tags.
        :return: Formatted string with per-line timestamps and section structure.
        """
        import re

        # Extract timestamps from LRC
        lrc_line_pattern = re.compile(r'^\[(\d{1,2}):(\d{2}\.\d+)\]')
        timestamps = []
        for raw_line in synced_lrc.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
            m = lrc_line_pattern.match(raw_line.strip())
            if m:
                mins = int(m.group(1))
                secs = float(m.group(2))
                total = mins * 60 + secs
                # Only keep timestamps for actual text lines (ignoring LRC blank dividers)
                text = raw_line.strip()[m.end():].strip()
                if text:
                    timestamps.append(self.format_timestamp(total))

        # Walk plain_lyrics_text and inject timestamps
        output_lines = []
        ts_idx = 0
        plain_lines = [l.strip() for l in plain_lyrics_text.split('\n')]
        
        for line in plain_lines:
            if not line:
                continue
            # If it's a structural tag, output it as is
            if line.startswith('[') and line.endswith(']') and not re.match(r'^\[\d', line):
                output_lines.append(f"\n{line}")
            else:
                # Assign the next available timestamp, or [00:00.00] if we ran out
                ts = timestamps[ts_idx] if ts_idx < len(timestamps) else "[00:00.00]"
                output_lines.append(f"{ts} {line}")
                ts_idx += 1

        return '\n'.join(output_lines).strip()

    def search_web_lyrics(self, snippet_candidates):
        """
        Web search fallback for lyrics when LRClib returns no results.
        Searches DuckDuckGo for the song on Genius.com then scrapes the lyrics
        page to extract full lyrics with verse/chorus structure.

        :param snippet_candidates: List of short text snippets from the Whisper transcript.
        :return: Dict with track_name, artist_name, lyrics, synced_lyrics=None, or None.
        """
        import requests
        import urllib.parse
        import re

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/124.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        }

        genius_url = None

        def find_genius_in_html(html_text):
            """Extract Genius lyrics URL from DuckDuckGo HTML results."""
            # Direct URLs in page
            direct = re.findall(
                r'https://genius\.com/[A-Za-z0-9][A-Za-z0-9\-]+-lyrics',
                html_text
            )
            if direct:
                return direct[0]
            # URL-encoded inside DDG redirect links: uddg=https%3A%2F%2Fgenius...
            encoded = re.findall(r'uddg=(https?%3A%2F%2Fgenius\.com%2F[^&"<]+)', html_text)
            for enc in encoded:
                decoded = urllib.parse.unquote(enc)
                if '-lyrics' in decoded:
                    return decoded
            return None

        # Try each candidate snippet with multiple query strategies
        for snippet in snippet_candidates:
            if genius_url:
                break
            for query_str in [
                f'{snippet} lyrics genius',   # explicit genius search
                f'{snippet} song lyrics',      # any lyrics site result
                f'"{snippet}" lyrics',         # quoted for exact phrase
            ]:
                if genius_url:
                    break
                try:
                    encoded_q = urllib.parse.quote(query_str)
                    resp = requests.get(
                        f'https://html.duckduckgo.com/html/?q={encoded_q}',
                        headers=headers, timeout=15
                    )
                    found = find_genius_in_html(resp.text)
                    if found:
                        genius_url = found
                        print(f"  Found Genius page: {genius_url}")
                except Exception:
                    continue

        if not genius_url:
            print("  No Genius page found via web search.")
            return None

        return self._scrape_genius(genius_url, headers)

    def _scrape_genius(self, url, headers):
        """
        Scrape a Genius.com song page to extract the song title, artist, and
        full lyrics with structural section tags ([Verse 1], [Chorus], etc.).
        """
        import requests
        import re
        from html import unescape

        try:
            resp = requests.get(url, headers=headers, timeout=20)
            html = resp.text

            # --- Extract artist and track name from page <title> ---
            # Format: "Artist – Song Title Lyrics | Genius Lyrics"
            title_tag = re.search(r'<title>([^<]+)</title>', html)
            page_title = unescape(title_tag.group(1)) if title_tag else ''
            tm = re.match(r'^(.+?)\s+[–\-]\s+(.+?)\s+Lyrics', page_title)
            artist = tm.group(1).strip() if tm else 'Unknown'
            track  = tm.group(2).strip() if tm else url.split('/')[-1].replace('-lyrics', '').replace('-', ' ').title()

            # --- Extract all lyrics containers ---
            # Genius wraps lyrics in divs with data-lyrics-container="true"
            # We find each container's inner HTML and clean it up.
            container_re = re.compile(
                r'data-lyrics-container="true"[^>]*>(.+?)</div>\s*(?=<div|$)',
                re.DOTALL
            )
            containers = container_re.findall(html)

            # Fallback: broader pattern if the tight one finds nothing
            if not containers:
                containers = re.findall(
                    r'data-lyrics-container="true"[^>]*>(.*?)</div>',
                    html, re.DOTALL
                )

            if not containers:
                print(f"  Could not extract lyrics from {url}")
                return None

            lyrics_parts = []
            for container in containers:
                # <br> → newline
                text = re.sub(r'<br\s*/?>', '\n', container, flags=re.IGNORECASE)
                # Section headers are inside <a><h3>…</h3></a> — keep the text
                text = re.sub(r'<a[^>]*>\s*<h3[^>]*>([^<]+)</h3>\s*</a>', r'\n[\1]\n', text)
                # Any other heading tags
                text = re.sub(r'<h[1-6][^>]*>([^<]+)</h[1-6]>', r'\n[\1]\n', text)
                # Strip all remaining HTML tags
                text = re.sub(r'<[^>]+>', '', text)
                text = unescape(text).strip()
                if text:
                    lyrics_parts.append(text)

            full_lyrics = '\n\n'.join(lyrics_parts).strip()

            if not full_lyrics or len(full_lyrics) < 50:
                print(f"  Scraped lyrics too short, skipping.")
                return None

            print(f"  Scraped: '{track}' by {artist} ({len(full_lyrics)} chars)")
            return {
                'track_name': track,
                'artist_name': artist,
                'lyrics': full_lyrics,
                'synced_lyrics': None,
                '_source': 'web',
            }

        except Exception as e:
            print(f"  Genius scrape failed: {e}")
            return None

    def fetch_lyrics(self, query, expected_artist=None):
        """
        Query LRClib search API to retrieve plain lyrics for the song.
        Retries up to 2 times on timeout to handle transient network issues.
        """
        import requests
        import urllib.parse

        encoded_query = urllib.parse.quote(query)
        url = f"https://lrclib.net/api/search?q={encoded_query}"
        headers = {
            "User-Agent": "LyricExtractorSDK/0.1.0 (https://github.com/google-deepmind/antigravity)"
        }
        for attempt in range(3):  # up to 3 attempts
            try:
                response = requests.get(url, headers=headers, timeout=20)
                if response.status_code == 200:
                    results = response.json()
                    for item in results:
                        plain_lyrics = item.get("plainLyrics")
                        if plain_lyrics:
                            if expected_artist:
                                import difflib
                                item_artist = item.get("artistName", "").lower()
                                sim = difflib.SequenceMatcher(None, expected_artist.lower(), item_artist).ratio()
                                if sim > 0.6:  # Allow typos, missing 'י', etc.
                                    return {
                                        "track_name": item.get("trackName"),
                                        "artist_name": item.get("artistName"),
                                        "lyrics": plain_lyrics,
                                        "synced_lyrics": item.get("syncedLyrics")
                                    }
                            else:
                                return {
                                    "track_name": item.get("trackName"),
                                    "artist_name": item.get("artistName"),
                                    "lyrics": plain_lyrics,
                                    "synced_lyrics": item.get("syncedLyrics")
                                }
                break  # got a valid response (even if empty results)
            except Exception:
                if attempt == 2:
                    pass  # exhausted retries, fall through
        return None

    def check_overlap(self, segments, official_lyrics):
        """
        Check if the unique words in Whisper transcription overlap significantly
        with the words in the official lyrics.
        Uses the smaller word set as the denominator to stay resilient against
        Whisper transcription errors (mis-spelled words lower whisper_words count).
        Returns a float between 0.0 and 1.0.
        """
        def clean_words(text):
            return set("".join(c for c in w.lower() if c.isalnum()) for w in text.split())

        whisper_text = " ".join(s['text'] for s in segments)
        whisper_words = clean_words(whisper_text)
        whisper_words.discard("")

        ly_words = clean_words(official_lyrics)
        ly_words.discard("")

        if not whisper_words or not ly_words:
            return 0.0

        matches = whisper_words.intersection(ly_words)
        # Use the smaller set as denominator: more robust when one side has errors
        denominator = min(len(whisper_words), len(ly_words))
        return len(matches) / denominator

    def inject_structure_tags(self, text):
        """
        Takes plain lyrics and heuristically injects [Verse N] and [Chorus] tags.
        """
        if not text:
            return text
        
        # If it already has structure tags like [Verse 1], return as is
        if any(line.strip().startswith('[') and line.strip().endswith(']') for line in text.split('\n')):
            return text

        # Split into stanzas (paragraphs separated by blank lines)
        stanzas = []
        current_stanza = []
        for line in text.split('\n'):
            line = line.strip()
            if line:
                current_stanza.append(line)
            else:
                if current_stanza:
                    stanzas.append('\n'.join(current_stanza))
                    current_stanza = []
        if current_stanza:
            stanzas.append('\n'.join(current_stanza))

        if not stanzas:
            return text

        # Count frequencies to identify chorus
        from collections import Counter
        counts = Counter(stanzas)
        
        # Any stanza that appears more than once is considered a Chorus
        structured = []
        verse_idx = 1
        for s in stanzas:
            if counts[s] > 1:
                structured.append(f"[Chorus]\n{s}")
            else:
                structured.append(f"[Verse {verse_idx}]\n{s}")
                verse_idx += 1
                
        return '\n\n'.join(structured)

    def find_lyrics_by_filename(self, audio_path, force_title=None, force_artist=None):
        import os
        base_name = os.path.splitext(os.path.basename(audio_path))[0]
        
        parsed_artist, parsed_title = "Unknown", base_name
        if " - " in base_name:
            parts = base_name.split(" - ", 1)
            parsed_artist, parsed_title = parts[0].strip(), parts[1].strip()
            
        if not force_title:
            force_title = parsed_title
        if not force_artist:
            force_artist = parsed_artist

        # Check Cache
        cache_dir = os.path.join(os.path.dirname(audio_path), "..", "lyric")
        cache_plain = os.path.join(cache_dir, f"{base_name}_official.txt")
        cache_synced = os.path.join(cache_dir, f"{base_name}_synced.lrc")
        
        if os.path.exists(cache_plain) and not force_title:
            with open(cache_plain, 'r', encoding='utf-8') as f:
                cached = f.read()
            lines = cached.split('\n', 2)
            official_lyrics = lines[-1].strip() if len(lines) == 3 else cached
            synced_lrc = None
            if os.path.exists(cache_synced):
                with open(cache_synced, 'r', encoding='utf-8') as f:
                    synced_lrc = f.read()
            return {"lyrics": official_lyrics, "synced_lyrics": synced_lrc, "source": "cache"}

        match = None
        # Forced lookup
        if force_title:
            q = f"{force_title} {force_artist}" if force_artist else force_title
            match = self.fetch_lyrics(q)
            if not match and force_artist and force_artist != "Unknown":
                match = self.fetch_lyrics(force_title, expected_artist=force_artist)
            if not match:
                web_result = self.search_web_lyrics([q])
                if web_result: match = web_result
        else:
            # Filename lookup
            import re
            cleaned_name = re.sub(r'[^a-zA-Z0-9\u0590-\u05FF ]', ' ', base_name).strip()
            generic_terms = {"test", "audio", "sample", "track", "song", "recording", "voice", "mic", "he", "en"}
            name_words = set(cleaned_name.lower().split())
            is_generic = name_words.issubset(generic_terms) or not name_words

            if not is_generic:
                match = self.fetch_lyrics(cleaned_name)

        if match:
            # Cache it
            os.makedirs(cache_dir, exist_ok=True)
            with open(cache_plain, 'w', encoding='utf-8') as f:
                f.write(f"Title: {match['track_name']}\nArtist: {match['artist_name']}\n{match['lyrics']}")
            if match.get('synced_lyrics'):
                with open(cache_synced, 'w', encoding='utf-8') as f:
                    f.write(match['synced_lyrics'])
            return {"lyrics": match['lyrics'], "synced_lyrics": match.get('synced_lyrics'), "source": "api"}
            
        return None

    def extract_black_box(self, audio_path, language=None, manual_lyrics=None, force_title=None, force_artist=None, synced_lyrics=None):
        """
        Black-box transcription and alignment utility.
        Input: filepath to WAV audio.
        Output: Dict containing final lyrics, detected/forced language, and metadata.
        """
        import os
        base_name = os.path.splitext(os.path.basename(audio_path))[0]
        
        # 1. Run Whisper transcription
        print(f"Transcribing audio '{audio_path}' with Whisper...")
        res = self.extract_lyrics(audio_path, language=language)
        segments = res['segments']
        detected_lang = res['language']
        
        official_lyrics = None
        synced_lrc = None
        lyrics_source = "raw"
        parsed_artist, parsed_title = "Unknown", base_name
        if " - " in base_name:
            parts = base_name.split(" - ", 1)
            parsed_artist, parsed_title = parts[0].strip(), parts[1].strip()
            # Automatically force the parsed title/artist if not explicitly provided
            if not force_title:
                force_title = parsed_title
            if not force_artist:
                force_artist = parsed_artist

        metadata = {
            "title": force_title if force_title else parsed_title,
            "artist": force_artist if force_artist else parsed_artist,
            "language": detected_lang
        }
        
        # Format the raw transcript as a fallback
        output_lines = []
        for s in segments:
            start_str = self.format_timestamp(s['start'])
            text = s['text'].strip()
            if text:
                output_lines.append(f"[{start_str}] {text}")
        raw_transcript = '\n'.join(output_lines)
        
        final_text = raw_transcript
        lyrics_source = "raw"
        official_lyrics = manual_lyrics
        synced_lrc = synced_lyrics
        
        # 2. If manual lyrics provided, we skip the search
        if official_lyrics:
            lyrics_source = "manual"
            print("Using provided manual lyrics.")
        else:
            # 3a. Check Cache
            cache_dir = os.path.join(os.path.dirname(audio_path), "..", "manual_lyrics")
            cache_plain = os.path.join(cache_dir, f"{base_name}_official.txt")
            cache_synced = os.path.join(cache_dir, f"{base_name}_synced.lrc")
            if os.path.exists(cache_plain) and not force_title:
                print(f"Loading cached official lyrics from '{cache_plain}'...")
                with open(cache_plain, 'r', encoding='utf-8') as f:
                    cached = f.read()
                # First line is metadata "Title: ...\nArtist: ..."
                lines = cached.split('\n', 2)
                title_line = next((l for l in lines[:2] if l.startswith('Title:')), '')
                artist_line = next((l for l in lines[:2] if l.startswith('Artist:')), '')
                official_lyrics = lines[-1].strip() if len(lines) == 3 else cached
                metadata['title'] = title_line.replace('Title:', '').strip() or base_name
                metadata['artist'] = artist_line.replace('Artist:', '').strip() or 'Unknown'
                lyrics_source = f"auto_alignment (cached: {metadata['title']} by {metadata['artist']})"
                if os.path.exists(cache_synced):
                    with open(cache_synced, 'r', encoding='utf-8') as f:
                        synced_lrc = f.read()

            if not official_lyrics:
                match = None
                
                # 3b. Forced title/artist lookup
                if force_title:
                    q = f"{force_title} {force_artist}" if force_artist else force_title
                    print(f"Attempting manual override lookup: '{q}'...")
                    match = self.fetch_lyrics(q)
                    
                    if not match and force_artist and force_artist != "Unknown":
                        print(f"LRClib search failed. Retrying fuzzy lookup with title: '{force_title}'...")
                        match = self.fetch_lyrics(force_title, expected_artist=force_artist)

                    if match:
                        print(f"Success! Song identified from manual override:")
                    else:
                        print("LRClib manual search failed. Falling back to web search...")
                        web_result = self.search_web_lyrics([q])
                        if web_result:
                            match = web_result
                            print(f"Song identified via web search: {web_result['track_name']} by {web_result['artist_name']}")
                else:
                    # 3c. Network search — filename first, then snippets
                    import re
                    cleaned_name = re.sub(r'[^a-zA-Z0-9\u0590-\u05FF ]', ' ', base_name).strip()
                    generic_terms = {"test", "audio", "sample", "track", "song", "recording", "voice", "mic", "he", "en"}
                    name_words = set(cleaned_name.lower().split())
                    is_generic = name_words.issubset(generic_terms) or not name_words

                    if not is_generic:
                        print(f"Attempting song lookup using cleaned filename: '{cleaned_name}'...")
                        match = self.fetch_lyrics(cleaned_name)
                        if match:
                            overlap = self.check_overlap(segments, match['lyrics'])
                            if overlap >= 0.30:
                                print(f"Success! Song identified by filename query (Overlap: {overlap:.2%}):")
                            else:
                                print(f"Filename match rejected (Overlap too low: {overlap:.2%})")
                                match = None

                    # Build snippet candidates (used for both LRClib and web fallback)
                    all_words = []
                    for s in segments:
                        all_words.extend(s['text'].split())
                    total = len(all_words)
                    positions = [0, total // 4, total // 2]
                    candidates = []
                    seen = set()
                    for pos in positions:
                        snippet = " ".join(all_words[pos: pos + 6]).strip()
                        if snippet and snippet not in seen:
                            candidates.append(snippet)
                            seen.add(snippet)
                    long_snippet = " ".join(all_words[:12]).strip()
                    if long_snippet not in seen:
                        candidates.append(long_snippet)

                    if not match:
                        for snippet in candidates:
                            if match:
                                break
                            print(f"Attempting song lookup using snippet: '{snippet}'...")
                            m = self.fetch_lyrics(snippet)
                            if m:
                                overlap = self.check_overlap(segments, m['lyrics'])
                                if overlap >= 0.25:
                                    print(f"Success! Song identified (Overlap: {overlap:.2%}):")
                                    match = m
                                else:
                                    print(f"Snippet match rejected (Overlap too low: {overlap:.2%})")

                    # 3d. Web search fallback — search DuckDuckGo + scrape Genius.com
                    if not match:
                        print("LRClib search failed. Searching the web for lyrics...")
                        web_result = self.search_web_lyrics(candidates)
                        if web_result:
                            # Trust the web result — Genius matched our search snippet.
                            # Overlap may be near 0 for non-Latin scripts (e.g. Hebrew)
                            # where Whisper heavily mis-transcribes phonetically.
                            match = web_result
                            print(f"Song identified via web search: {web_result['track_name']} by {web_result['artist_name']}")

                if match:
                    print(f"  Title: {match['track_name']}")
                    print(f"  Artist: {match['artist_name']}")
                    official_lyrics = match['lyrics']
                    synced_lrc = match.get('synced_lyrics')
                    source_tag = 'web search' if not match.get('synced_lyrics') and match.get('_source') == 'web' \
                                 else 'LRClib match'
                    lyrics_source = f"auto_alignment ({source_tag}: {match['track_name']} by {match['artist_name']})"
                    metadata["title"] = match["track_name"]
                    metadata["artist"] = match["artist_name"]

                    # 3c. Cache the result to disk for future runs
                    os.makedirs(cache_dir, exist_ok=True)
                    with open(cache_plain, 'w', encoding='utf-8') as f:
                        f.write(f"Title: {match['track_name']}\nArtist: {match['artist_name']}\n{official_lyrics}")
                    if synced_lrc:
                        with open(cache_synced, 'w', encoding='utf-8') as f:
                            f.write(synced_lrc)
                    print(f"  Lyrics cached to '{cache_plain}' for future runs.")
        
        # 4. Perform alignment or format raw fallback
        if official_lyrics:
            # Dynamically inject structure if missing
            official_lyrics = self.inject_structure_tags(official_lyrics)
            
            if synced_lrc:
                print("Using LRClib synced timestamps (high precision)...")
                parsed = self.parse_synced_lyrics(synced_lrc, official_lyrics)
                final_text = parsed if parsed else self.align_lyrics(segments, official_lyrics)
            else:
                print("Aligning official lyrics with Whisper timestamps (word interpolation)...")
                final_text = self.align_lyrics(segments, official_lyrics)
        else:
            print("No official lyrics found. Falling back to formatted raw Whisper transcript...")
            formatted_segments = []
            for s in segments:
                seg_text = s['text'].strip()
                seg_start = s.get('start', 0.0)
                seg_end = s.get('end', seg_start)
                seg_dur = seg_end - seg_start
                # Split long segments at clause boundaries so each line is readable
                # and gets its own interpolated timestamp
                import re as _re
                clauses = _re.split(r'(?<=[.!?,])\s+', seg_text)
                clauses = [c.strip() for c in clauses if c.strip()]
                if len(clauses) <= 1:
                    formatted_segments.append(f"{s['timestamp']} {seg_text}")
                else:
                    # Count words per clause to distribute time proportionally
                    word_counts = [len(c.split()) for c in clauses]
                    total_words = max(sum(word_counts), 1)
                    elapsed = 0.0
                    for clause, wc in zip(clauses, word_counts):
                        t = seg_start + (elapsed / total_words) * seg_dur
                        formatted_segments.append(f"{self.format_timestamp(t)} {clause}")
                        elapsed += wc
            final_text = "\n".join(formatted_segments)
            
        return {
            "text": final_text,
            "source": lyrics_source,
            "language": detected_lang,
            "metadata": metadata,
            "segments": segments
        }
