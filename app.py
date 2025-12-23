import streamlit as st
import os
import shutil
import requests
import json
import re
import time
from mutagen.mp4 import MP4, MP4Cover
from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, COMM, TRCK
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# --- Configuration ---
DOWNLOAD_DIR = "/downloads"
LIBRARY_DIR = "/audiobooks"
DATA_DIR = "/app/data"
HISTORY_FILE = os.path.join(DATA_DIR, "processed_log.json")
CACHE_FILE = os.path.join(DATA_DIR, "library_map_cache.json") # Renamed for clarity
AUDNEXUS_API = "https://api.audnexus.com/books"

os.makedirs(DATA_DIR, exist_ok=True)

st.set_page_config(page_title="TidyBooks", layout="wide", page_icon="📚")

# --- Persistence & Caching ---
def load_json(filepath, default=None):
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r') as f: return json.load(f)
        except: pass
    return default

def save_json(filepath, data):
    with open(filepath, 'w') as f: json.dump(data, f)

def get_library_map(force_refresh=False):
    """
    Returns a dictionary: { "FolderName": "/full/path/to/folder/in/library" }
    Scans RECURSIVELY to find books nested in Author/Series folders.
    """
    if not force_refresh:
        cached = load_json(CACHE_FILE)
        if cached is not None: return cached
            
    try:
        library_map = {}
        # Deep scan of the library
        for root, dirs, files in os.walk(LIBRARY_DIR):
            folder_name = os.path.basename(root)
            # We map the folder name to its full path
            # If duplicates exist, this takes the last one found (acceptable limitation)
            library_map[folder_name] = root
        
        save_json(CACHE_FILE, library_map)
        return library_map
    except:
        return {}

# --- Helper Functions ---
def sanitize_filename(name):
    if not name: return "Unknown"
    clean = name.replace("/", "-").replace("\\", "-")
    return re.sub(r'[<>:"|?*]', '', clean).strip()

def get_candidates(force_refresh=False):
    history = load_json(HISTORY_FILE, [])
    candidates = []
    
    if not os.path.exists(DOWNLOAD_DIR):
        return []

    # Get the Deep Map of the library
    library_map = get_library_map(force_refresh)

    for root, dirs, files in os.walk(DOWNLOAD_DIR):
        # Look for audio files
        audio_files = [f for f in files if f.lower().endswith(('.mp3', '.m4b', '.m4a', '.flac'))]
        
        if audio_files:
            full_path = root
            folder_name = os.path.basename(root)
            
            # --- STATUS LOGIC ---
            status = 0 # Default: New/White
            display_prefix = ""
            match_path = None
            
            # 1. Check if this exact download path was processed by us previously
            if full_path in history:
                status = 3 # Special "Hidden/Bottom" status for History
                display_prefix = "✅ (History) "

            # 2. Check the Library Map for a folder with the same name
            elif folder_name in library_map:
                lib_path = library_map[folder_name]
                match_path = lib_path
                
                # Check for metadata.json to determine Green vs Yellow
                if os.path.exists(os.path.join(lib_path, "metadata.json")):
                    status = 2 # Green (Properly Tidy)
                    display_prefix = "✅ "
                else:
                    status = 1 # Yellow (Exists, but maybe messy)
                    display_prefix = "🟨 "
            
            candidates.append({
                "label": f"{display_prefix}{folder_name}",
                "path": full_path,
                "type": "dir",
                "status": status,
                "match_path": match_path,
                "name": folder_name
            })

    # Sort Order: 
    # 0 (New/White) -> 1 (Yellow) -> 2 (Green) -> 3 (History)
    return sorted(candidates, key=lambda x: (x['status'], x['name']))

def fetch_metadata(query):
    try:
        params = {'q': query}
        r = requests.get(AUDNEXUS_API, params=params, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        st.error(f"Connection Error: {e}")
    return []

def tag_file(file_path, author, title, series, desc, cover_url, year, track_num, total_tracks):
    ext = os.path.splitext(file_path)[1].lower()
    try:
        if ext in ['.m4b', '.m4a']:
            audio = MP4(file_path)
            if audio.tags is None: audio.add_tags()
            audio.tags['\xa9nam'] = title
            audio.tags['\xa9ART'] = author
            audio.tags['\xa9alb'] = series if series else title
            audio.tags['desc'] = desc
            audio.tags['trkn'] = [(track_num, total_tracks)]
            if year: audio.tags['\xa9day'] = year
            if cover_url:
                try:
                    img_data = requests.get(cover_url, timeout=5).content
                    audio.tags['covr'] = [MP4Cover(img_data, imageformat=MP4Cover.FORMAT_JPEG)]
                except: pass
            audio.save()
            
        elif ext == '.mp3':
            try: audio = ID3(file_path) 
            except: audio = ID3()
            audio.add(TIT2(encoding=3, text=title))
            audio.add(TPE1(encoding=3, text=author))
            audio.add(TALB(encoding=3, text=series if series else title))
            audio.add(TRCK(encoding=3, text=f"{track_num}/{total_tracks}"))
            if desc:
