import os
import json

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')

FILES_DIR = os.path.join(BASE_DIR, "VidiChord_Files")
DEFAULT_CONFIG = {
    "audios_dir": os.path.join(FILES_DIR, "Audios"),
    "lyrics_dir": os.path.join(FILES_DIR, "Lyrics"),
    "chords_dir": os.path.join(FILES_DIR, "Chords"),
    "sheets_dir": os.path.join(FILES_DIR, "Sheets")
}

def load_config():
    if not os.path.exists(CONFIG_PATH):
        return DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            config = json.load(f)
            # Ensure all keys exist
            for k, v in DEFAULT_CONFIG.items():
                if k not in config:
                    config[k] = v
            return config
    except Exception:
        return DEFAULT_CONFIG.copy()

def save_config(new_config):
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(new_config, f, indent=4)
    ensure_directories(new_config)

def ensure_directories(config=None):
    if config is None:
        config = load_config()
    
    for key, path in config.items():
        if key.endswith('_dir'):
            os.makedirs(path, exist_ok=True)

# Ensure default directories exist on startup
ensure_directories()
