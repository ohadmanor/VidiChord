import os
import re
from google import genai
from google.genai import types

def refine_lyrics_with_llm(raw_text, api_key):
    """
    Takes the raw whisper transcript and uses Gemini to refine it,
    fix spelling, identify the artist/title if possible, and add song structure.
    """
    if not api_key:
        raise ValueError("Gemini API key is required for LLM refinement.")
        
    client = genai.Client(api_key=api_key)
    
    prompt = f"""
You are an expert music transcriber. I am giving you the raw, uncorrected output from a speech-to-text model (Whisper) for a song.
It contains timestamps and text lines. Some of the text might be gibberish, have spelling mistakes, or miss words.
Your job is to:
1. Identify the likely Song Title and Artist (if you can figure it out from the lyrics).
2. Fix all the spelling and grammar mistakes in the lyrics using your knowledge of official song lyrics.
3. Keep the original timestamps EXACTLY as they are for the corresponding lines.
4. Add structural tags like [Verse 1], [Chorus], etc. on their own lines (without timestamps) where appropriate.

Format your output exactly like this:
Title: [Song Title]
Artist: [Singer]
Language: [English/Hebrew]

[Verse 1]
[00:00.00] Corrected line 1
[00:05.00] Corrected line 2

Here is the raw text:
{raw_text}
"""
    
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=prompt,
    )
    
    return response.text.strip()
