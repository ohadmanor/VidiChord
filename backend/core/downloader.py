import os
import re
import requests
import zipfile
import yt_dlp

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FFMPEG_DIR = os.path.join(BASE_DIR, 'ffmpeg')
FFMPEG_PATH = os.path.join(FFMPEG_DIR, 'ffmpeg.exe')
FFPROBE_PATH = os.path.join(FFMPEG_DIR, 'ffprobe.exe')

def ensure_ffmpeg():
    if os.path.exists(FFMPEG_PATH) and os.path.exists(FFPROBE_PATH):
        return FFMPEG_DIR

    print("=== Local FFmpeg Setup ===")
    print("Downloading a lightweight FFmpeg binary...")
    
    os.makedirs(FFMPEG_DIR, exist_ok=True)
    zip_path = os.path.join(FFMPEG_DIR, "ffmpeg.zip")
    url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    
    try:
        response = requests.get(url, stream=True)
        response.raise_for_status()
        with open(zip_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk: f.write(chunk)
        
        print("\nDownload complete. Extracting FFmpeg binaries...")
        extracted_ffmpeg = False
        extracted_ffprobe = False
        
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            for file_info in zip_ref.infolist():
                filename = file_info.filename
                if filename.endswith('ffmpeg.exe'):
                    with zip_ref.open(file_info) as source, open(FFMPEG_PATH, 'wb') as target:
                        target.write(source.read())
                    extracted_ffmpeg = True
                elif filename.endswith('ffprobe.exe'):
                    with zip_ref.open(file_info) as source, open(FFPROBE_PATH, 'wb') as target:
                        target.write(source.read())
                    extracted_ffprobe = True
        
        if os.path.exists(zip_path):
            os.remove(zip_path)
            
        if extracted_ffmpeg and extracted_ffprobe:
            return FFMPEG_DIR
        else:
            raise Exception("Failed to locate ffmpeg.exe or ffprobe.exe.")
    except Exception as e:
        if os.path.exists(zip_path):
            try: os.remove(zip_path)
            except: pass
        raise Exception(f"Error setting up local FFmpeg: {e}")

def sanitize_filename(name):
    cleaned = re.sub(r'[\\/:*?"<>|]', ' - ', name)
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return cleaned.strip('. ')

def get_song_filename(info_dict):
    title = info_dict.get('title', '')
    artist = info_dict.get('artist', '')
    track = info_dict.get('track', '')
    uploader = info_dict.get('uploader', '')

    if artist and track: return f"{artist} - {track}"
    if track and uploader:
        if uploader.endswith(' - Topic'): uploader = uploader[:-8]
        return f"{uploader} - {track}"
    return title

def download_audio(url, out_dir, status_callback):
    ffmpeg_bin_dir = ensure_ffmpeg()
    
    status_callback("status", "downloading")
    status_callback("progress", 0)
    status_callback("log_msg", "Retrieving YouTube video metadata...")

    ydl_opts_meta = {
        'ffmpeg_location': ffmpeg_bin_dir,
        'noplaylist': True,
        'quiet': True,
    }
    
    with yt_dlp.YoutubeDL(ydl_opts_meta) as ydl:
        info = ydl.extract_info(url, download=False)

    raw_filename = get_song_filename(info)
    clean_filename = sanitize_filename(raw_filename)
    
    base_filepath = os.path.join(out_dir, clean_filename)
    final_filepath = f"{base_filepath}.wav"
    
    counter = 1
    while os.path.exists(final_filepath):
        final_filepath = f"{base_filepath} ({counter}).wav"
        counter += 1
        
    final_filename = os.path.basename(final_filepath)
    outtmpl_path = final_filepath[:-4]
    
    status_callback("video_title", info.get('title', 'Unknown Title'))
    status_callback("output_filename", final_filename)
    status_callback("log_msg", f"Found video: {info.get('title')}\nStarting download...")

    class WebProgressLogger:
        def debug(self, msg):
            if msg.startswith('[download]'):
                percent_match = re.search(r'(\d+\.\d+)%', msg)
                if percent_match:
                    percent = float(percent_match.group(1))
                    status_callback("progress", percent)
                    status_callback("log_msg", msg)
                else:
                    status_callback("log_msg", msg)
            elif msg.startswith('[ExtractAudio]'):
                status_callback("status", "converting")
                status_callback("log_msg", "Extracting and encoding audio stream to 16-bit WAV...")
            else:
                status_callback("log_msg", msg)
        def info(self, msg): pass
        def warning(self, msg):
            status_callback("log_msg", f"Warning: {msg}")
        def error(self, msg):
            status_callback("error_msg_append", f"{msg}\n")
            status_callback("log_msg", f"Error: {msg}")

    ydl_opts = {
        'ffmpeg_location': ffmpeg_bin_dir,
        'format': 'bestaudio/best',
        'outtmpl': f"{outtmpl_path}.%(ext)s",
        'noplaylist': True,
        'logger': WebProgressLogger(),
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'wav',
        }],
        'postprocessor_args': {
            'ffmpeg': ['-acodec', 'pcm_s16le', '-ar', '44100']
        },
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        retcode = ydl.download([url])
        if retcode != 0:
            raise Exception("Download or conversion failed. Please check the logs.")

    status_callback("log_msg", f"WAV created: {final_filename}")
    return final_filepath, info.get('title', 'Unknown Title'), final_filename
