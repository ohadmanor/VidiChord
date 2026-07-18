import os
import sys
import json
import urllib.parse
import threading
import http.server
import socketserver
import traceback
import mimetypes

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Ensure ffmpeg is in PATH for whisper
FFMPEG_DIR = os.path.join(BASE_DIR, 'ffmpeg')
os.environ["PATH"] += os.pathsep + FFMPEG_DIR

# Add Node.js to PATH for yt-dlp JS runtime
NODE_DIR = r"C:\Program Files\Microsoft Visual Studio\18\Enterprise\MSBuild\Microsoft\VisualStudio\NodeJs"
if os.path.exists(NODE_DIR):
    os.environ["PATH"] += os.pathsep + NODE_DIR

from core.downloader import download_audio
from lyric_extractor import LyricExtractor
from chords_extractor.extractor import extract_chords
import config_manager

PORT = 8001

status_lock = threading.Lock()
conversion_status = {
    "status": "idle",
    "video_title": "",
    "output_filename": "",
    "progress": 0,
    "log_msg": "",
    "error_msg": "",
    "lyrics": None
}

def update_status(key, value):
    with status_lock:
        if key == "error_msg_append":
            conversion_status["error_msg"] += value
        else:
            conversion_status[key] = value

def process_bg(url, official_lyrics="", fusion_config=None):
    try:
        config = config_manager.load_config()
        audios_dir = config.get("audios_dir", os.path.join(BASE_DIR, "VidiChord_Files", "Audios"))
        lyrics_dir = config.get("lyrics_dir", os.path.join(BASE_DIR, "VidiChord_Files", "Lyrics"))
        
        # Step 1: Download Audio
        wav_path, title, filename = download_audio(url, audios_dir, update_status)
        update_status("output_filename", wav_path)
        
        # Step 2: Attempt fast lyrics lookup
        update_status("status", "extracting_lyrics")
        update_status("log_msg", "Searching for lyrics online...")
        
        extractor = LyricExtractor(model_name="base")
        found = extractor.find_lyrics_by_filename(wav_path, force_title=None)
        
        if found:
            # We found lyrics online automatically, continue directly
            update_status("log_msg", "Lyrics found! Proceeding with alignment...")
            process_bg_continue(wav_path, choice="ai", pre_found_lyrics=found, fusion_config=fusion_config)
        else:
            if official_lyrics:
                 # Was provided initially for some reason
                 process_bg_continue(wav_path, choice="manual", manual_lyrics=official_lyrics, fusion_config=fusion_config)
            else:
                 # Pause and ask user
                 update_status("log_msg", "Lyrics not found automatically. Waiting for user choice...")
                 update_status("status", "waiting_for_user_choice")
                 
    except Exception as e:
        traceback.print_exc()
        update_status("status", "failed")
        update_status("error_msg", str(e))

def process_bg_continue(wav_path, choice="ai", manual_lyrics="", pre_found_lyrics=None, language=None, fusion_config=None):
    try:
        update_status("status", "extracting_lyrics")
        update_status("log_msg", "Continuing transcription and extraction pipeline...")
        
        config = config_manager.load_config()
        lyrics_dir = config.get("lyrics_dir", os.path.join(BASE_DIR, "VidiChord_Files", "Lyrics"))
        
        base_name = os.path.splitext(os.path.basename(wav_path))[0]
        import re
        base_name = re.sub(r' \(\d+\)$', '', base_name)
        
        extractor = LyricExtractor(model_name="base")
        
        official_lyrics = manual_lyrics if choice == "manual" else None
        synced_lyrics = None
        
        if pre_found_lyrics:
            official_lyrics = pre_found_lyrics.get("lyrics")
            synced_lyrics = pre_found_lyrics.get("synced_lyrics")
        
        result = extractor.extract_black_box(
            audio_path=wav_path,
            language=language,
            manual_lyrics=official_lyrics,
            synced_lyrics=synced_lyrics
        )
        
        title = result['metadata'].get('title', base_name)
        artist = result['metadata'].get('artist', 'Unknown')
        
        update_status("video_title", title)
        update_status("song_artist", artist)
        
        raw_text = result["text"] + "\n"
        
        # Save lyrics to Lyrics dir as txt and json
        lyrics_filename = f"{base_name}_lyrics.txt"
        lyrics_filepath = os.path.join(lyrics_dir, lyrics_filename)
        os.makedirs(lyrics_dir, exist_ok=True)
        with open(lyrics_filepath, 'w', encoding='utf-8') as f:
            f.write(raw_text)
            
        json_filename = f"{base_name}_lyrics.json"
        json_filepath = os.path.join(lyrics_dir, json_filename)
        with open(json_filepath, 'w', encoding='utf-8') as f:
            json.dump({"segments": result.get("segments", [])}, f, ensure_ascii=False, indent=2)
            
        update_status("log_msg", f"Saved lyrics to {lyrics_filename} and {json_filename}")
        
        update_status("lyrics", raw_text)
        update_status("log_msg", "Lyrics extraction completed. Starting chords extraction...")
        update_status("progress", 50)
        
        # --- Chords Extraction ---
        update_status("status", "extracting_chords")
        try:
            chords_result = extract_chords(wav_path, config=fusion_config)
            update_status("chords_data", chords_result.get("beats", []))
            update_status("song_bpm", chords_result.get("metadata", {}).get("bpm", 0))
            update_status("log_msg", "Chords extraction completed successfully.")
        except Exception as e:
            traceback.print_exc()
            update_status("log_msg", f"Chords extraction failed: {str(e)}")
            
        update_status("status", "success")
        update_status("progress", 100)
        
    except Exception as e:
        traceback.print_exc()
        update_status("status", "failed")
        update_status("error_msg", str(e))

class LocalHTTPServerHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass

    def send_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)

        if path == '/api/status':
            with status_lock:
                status_copy = conversion_status.copy()
            self.send_json(status_copy)
            
        elif path == '/api/config':
            config = config_manager.load_config()
            self.send_json(config)
            
        elif path == '/api/stream-audio':
            filepath_list = query.get('path')
            if not filepath_list:
                self.send_error(400, "Missing path parameter")
                return
            
            filepath = filepath_list[0]
            if not os.path.exists(filepath):
                self.send_error(404, "File not found")
                return
                
            try:
                mime_type, _ = mimetypes.guess_type(filepath)
                if mime_type is None:
                    mime_type = 'audio/wav'
                    
                file_size = os.path.getsize(filepath)
                
                self.send_response(200)
                self.send_header('Content-Type', mime_type)
                self.send_header('Content-Length', str(file_size))
                self.send_header('Accept-Ranges', 'bytes')
                self.send_cors_headers()
                self.end_headers()
                
                with open(filepath, 'rb') as f:
                    self.wfile.write(f.read())
            except Exception as e:
                self.send_error(500, str(e))
                
        elif path == '/api/get_word_timestamps':
            wav_path_list = query.get('wav_path')
            if not wav_path_list:
                self.send_error(400, "Missing wav_path parameter")
                return
            
            wav_path = wav_path_list[0]
            config = config_manager.load_config()
            lyrics_dir = config.get("lyrics_dir", os.path.join(BASE_DIR, "VidiChord_Files", "Lyrics"))
            
            base_name = os.path.splitext(os.path.basename(wav_path))[0]
            import re
            base_name = re.sub(r' \(\d+\)$', '', base_name)
            
            json_filename = f"{base_name}_lyrics.json"
            json_filepath = os.path.join(lyrics_dir, json_filename)
            
            if not os.path.exists(json_filepath):
                self.send_json({"status": "not_found", "segments": []})
                return
                
            try:
                with open(json_filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.send_json({"status": "success", "segments": data.get("segments", [])})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
                
        else:
            # Serve static frontend files
            if hasattr(sys, '_MEIPASS'):
                frontend_dir = os.path.join(sys._MEIPASS, 'frontend', 'dist', 'frontend', 'browser')
            else:
                frontend_dir = os.path.abspath(os.path.join(BASE_DIR, '..', 'frontend', 'dist', 'frontend', 'browser'))
            
            # Map path to file
            if path == '/':
                file_path = os.path.join(frontend_dir, 'index.html')
            else:
                # Remove leading slash
                rel_path = path.lstrip('/')
                file_path = os.path.join(frontend_dir, rel_path)
                
            # If file does not exist, serve index.html (SPA routing)
            if not os.path.exists(file_path) or not os.path.isfile(file_path):
                file_path = os.path.join(frontend_dir, 'index.html')
                
            if os.path.exists(file_path) and os.path.isfile(file_path):
                try:
                    with open(file_path, 'rb') as f:
                        content = f.read()
                    self.send_response(200)
                    mime_type, _ = mimetypes.guess_type(file_path)
                    if mime_type:
                        self.send_header('Content-Type', mime_type)
                    self.end_headers()
                    self.wfile.write(content)
                except Exception as e:
                    self.send_error(500, f"Error reading file: {str(e)}")
            else:
                self.send_response(404)
                self.send_cors_headers()
                self.end_headers()
                self.wfile.write(b"Not Found")
            
    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        
        if path == '/api/convert':
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            try:
                data = json.loads(body)
                url = data.get('url')
                official_lyrics = data.get('official_lyrics', '')
                fusion_config = data.get('fusion_config', None)
                
                if not url:
                    self.send_json({"error": "No URL provided"}, 400)
                    return
                
                with status_lock:
                    conversion_status.update({
                        "status": "starting",
                        "video_title": "",
                        "output_filename": "",
                        "progress": 0,
                        "log_msg": "Initializing process...",
                        "error_msg": "",
                        "lyrics": None
                    })

                thread = threading.Thread(target=process_bg, args=(url, official_lyrics, fusion_config))
                thread.daemon = True
                thread.start()
                
                self.send_json({"status": "started"})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
                
        elif path == '/api/convert_continue':
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            try:
                data = json.loads(body)
                wav_path = data.get('wav_path')
                choice = data.get('choice') # 'manual' or 'ai'
                manual_lyrics = data.get('lyrics', '')
                fusion_config = data.get('fusion_config', None)
                
                if not wav_path or not choice:
                    self.send_json({"error": "Missing wav_path or choice"}, 400)
                    return
                language = data.get('language')
                
                thread = threading.Thread(target=process_bg_continue, args=(wav_path, choice, manual_lyrics, None, language, fusion_config))
                thread.daemon = True
                thread.start()
                
                self.send_json({"status": "resumed"})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
                
        elif path == '/api/config':
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            
            try:
                new_config = json.loads(body)
                config_manager.save_config(new_config)
                self.send_json({"status": "success", "config": new_config})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
                
        elif path == '/api/save_lyrics':
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            try:
                data = json.loads(body)
                wav_path = data.get('wav_path')
                new_lyrics = data.get('lyrics')
                
                if not wav_path or not new_lyrics:
                    self.send_json({"error": "Missing wav_path or lyrics"}, 400)
                    return
                    
                config = config_manager.load_config()
                lyrics_dir = config.get("lyrics_dir", os.path.join(BASE_DIR, "VidiChord_Files", "Lyrics"))
                
                base_name = os.path.splitext(os.path.basename(wav_path))[0]
                import re
                base_name = re.sub(r' \(\d+\)$', '', base_name)
                
                # Update the frontend cache file
                lyrics_filename = f"{base_name}_lyrics.txt"
                lyrics_filepath = os.path.join(lyrics_dir, lyrics_filename)
                os.makedirs(lyrics_dir, exist_ok=True)
                with open(lyrics_filepath, 'w', encoding='utf-8') as f:
                    f.write(new_lyrics)
                    
                # Update the current conversion status if it matches
                with status_lock:
                    if conversion_status["output_filename"] == wav_path:
                        conversion_status["lyrics"] = new_lyrics
                        
                self.send_json({"status": "success", "message": "Lyrics saved"})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
                
        elif path == '/api/save_chords':
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            try:
                data = json.loads(body)
                wav_path = data.get('wav_path')
                new_beats = data.get('beats')
                
                if not wav_path or not new_beats:
                    self.send_json({"error": "Missing wav_path or beats"}, 400)
                    return
                    
                config = config_manager.load_config()
                chords_dir = config.get("chords_dir", os.path.join(BASE_DIR, "VidiChord_Files", "Chords"))
                
                base_name = os.path.splitext(os.path.basename(wav_path))[0]
                import re
                base_name = re.sub(r' \(\d+\)$', '', base_name)
                
                chords_filename = f"{base_name}_chords.json"
                chords_filepath = os.path.join(chords_dir, chords_filename)
                
                if os.path.exists(chords_filepath):
                    with open(chords_filepath, 'r', encoding='utf-8') as f:
                        chords_data = json.load(f)
                    chords_data['beats'] = new_beats
                else:
                    chords_data = {"beats": new_beats}
                    
                os.makedirs(chords_dir, exist_ok=True)
                with open(chords_filepath, 'w', encoding='utf-8') as f:
                    json.dump(chords_data, f, indent=2, ensure_ascii=False)
                        
                self.send_json({"status": "success", "message": "Chords saved"})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
                
        elif path == '/api/save_synced':
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            try:
                data = json.loads(body)
                wav_path = data.get('wav_path')
                synced_lines = data.get('synced_lines')
                
                if not wav_path or not synced_lines:
                    self.send_json({"error": "Missing wav_path or synced_lines"}, 400)
                    return
                    
                config = config_manager.load_config()
                synced_dir = config.get("synced_dir", os.path.join(BASE_DIR, "VidiChord_Files", "Synced"))
                os.makedirs(synced_dir, exist_ok=True)
                
                base_name = os.path.splitext(os.path.basename(wav_path))[0]
                import re
                base_name = re.sub(r' \(\d+\)$', '', base_name)
                
                synced_filepath = os.path.join(synced_dir, f"{base_name}_synced.json")
                with open(synced_filepath, 'w', encoding='utf-8') as f:
                    json.dump(synced_lines, f, indent=2, ensure_ascii=False)
                    
                self.send_json({"status": "success"})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
                
        elif path == '/api/get_synced':
            query = urlparse(self.path).query
            query_components = dict(qc.split("=") for qc in query.split("&") if "=" in qc)
            wav_path = query_components.get("wav_path", "")
            
            try:
                config = config_manager.load_config()
                synced_dir = config.get("synced_dir", os.path.join(BASE_DIR, "VidiChord_Files", "Synced"))
                
                # Unquote path
                wav_path = urllib.parse.unquote(wav_path)
                
                base_name = os.path.splitext(os.path.basename(wav_path))[0]
                import re
                base_name = re.sub(r' \(\d+\)$', '', base_name)
                
                synced_filepath = os.path.join(synced_dir, f"{base_name}_synced.json")
                
                if os.path.exists(synced_filepath):
                    with open(synced_filepath, 'r', encoding='utf-8') as f:
                        synced_lines = json.load(f)
                    self.send_json({"status": "success", "synced_lines": synced_lines})
                else:
                    self.send_json({"status": "not_found", "message": "No synced file found"})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        elif path == '/api/export-to-songbook':
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            try:
                import time, re
                data = json.loads(body)
                title = data.get('title', 'Unknown Title')
                artist = data.get('artist', 'Unknown Artist')
                synced_lines = data.get('syncedLines', [])
                is_hebrew = data.get('isHebrew', False)
                
                raw_lines = []
                for line in synced_lines:
                    line_type = line.get('type')
                    if line_type == 'instrumental':
                        raw_lines.append(line.get('text', '') + "\n")
                    elif line_type == 'lyric':
                        chord_text = line.get('chordText', '')
                        text = line.get('text', '')
                        if chord_text and chord_text.strip():
                            raw_lines.append(chord_text)
                        raw_lines.append(text)
                
                raw_text = "\n".join(raw_lines)
                config = config_manager.load_config()
                sheets_dir = config.get("sheets_dir")
                if not sheets_dir or not os.path.isdir(sheets_dir):
                    self.send_json({"error": "Sheets directory is not configured or does not exist. Please configure it in Settings."}, 400)
                    return
                    
                import time, re
                safe_title = re.sub(r'[\\/*?:"<>|]', "", title).strip()
                safe_artist = re.sub(r'[\\/*?:"<>|]', "", artist).strip()
                filename = f"{safe_title} - {safe_artist}.json" if safe_title else f"song_{int(time.time())}.json"
                file_path = os.path.join(sheets_dir, filename)
                
                new_song = {
                    "id": "song_" + str(int(time.time())),
                    "title": title,
                    "artist": artist,
                    "key": "",
                    "isRTL": is_hebrew,
                    "rawText": raw_text,
                    "modifiedByUser": True
                }
                
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump(new_song, f, indent=2, ensure_ascii=False)
                    
                self.send_json({"status": "success", "message": f"Exported to {filename} successfully!"})
                    
                    
            except Exception as e:
                import traceback
                traceback.print_exc()
                self.send_json({"error": str(e)}, 500)
                
        else:
            self.send_response(404)
            self.send_cors_headers()
            self.end_headers()

    def send_json(self, data, status_code=200):
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(json.dumps(data).encode('utf-8'))

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

def run_web_server():
    server_address = ('', PORT)
    socketserver.TCPServer.allow_reuse_address = True
    with ThreadedHTTPServer(server_address, LocalHTTPServerHandler) as httpd:
        print(f"VidiChord Backend Server running on port {PORT}...")
        
        # Open browser in a separate thread
        def open_browser():
            import time, webbrowser
            time.sleep(1)
            webbrowser.open(f"http://localhost:{PORT}")
            
        threading.Thread(target=open_browser, daemon=True).start()
        
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down server.")

if __name__ == "__main__":
    run_web_server()
