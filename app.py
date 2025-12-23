import streamlit as st
import os
import shutil
import requests
import json
import re
import time
from mutagen.mp4 import MP4, MP4Cover
from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, COMM, TRCK

# --- Configuration ---
DOWNLOAD_DIR = "/downloads"
LIBRARY_DIR = "/audiobooks"
DATA_DIR = "/app/data"
HISTORY_FILE = os.path.join(DATA_DIR, "processed_log.json")
CACHE_FILE = os.path.join(DATA_DIR, "library_map_cache.json")
AUDNEXUS_API = "https://api.audnexus.com/books"

os.makedirs(DATA_DIR, exist_ok=True)

st.set_page_config(page_title="TidyBooks", layout="wide", page_icon="📚")

# --- Persistence ---
def load_json(filepath, default=None):
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r') as f: return json.load(f)
        except: pass
    return default if default is not None else []

def save_json(filepath, data):
    with open(filepath, 'w') as f: json.dump(data, f)

# --- Library Scanning ---
def get_library_map(force_refresh=False):
    """
    Scans the library to find all book folders.
    Returns list of dicts: [{'name': 'Book Title', 'path': '/path/to/Book Title'}]
    """
    if not force_refresh:
        cached = load_json(CACHE_FILE, None)
        # FIX: Check if cached data is a list. If it's a dict (old format), ignore it.
        if cached and isinstance(cached, list): 
            return cached
            
    library_items = []
    # Walk library to find leaf folders (folders that contain files, not just other folders)
    for root, dirs, files in os.walk(LIBRARY_DIR):
        has_audio = any(f.lower().endswith(('.mp3', '.m4b', '.m4a', '.flac')) for f in files)
        
        if has_audio:
            folder_name = os.path.basename(root)
            library_items.append({
                "name": folder_name,
                "path": root,
                "clean": sanitize_for_matching(folder_name)
            })
    
    save_json(CACHE_FILE, library_items)
    return library_items

# --- Helper Functions ---
def sanitize_for_matching(text):
    if not text: return ""
    text = text.lower()
    text = re.sub(r'\b(audiobook|mp3|m4b|cd|disc|part|v|vol|chapter)\b', '', text)
    text = re.sub(r'[^a-z]', '', text) 
    return text

def sanitize_filename(name, default_to_unknown=False):
    if not name: return "Unknown" if default_to_unknown else ""
    clean = name.replace("/", "-").replace("\\", "-")
    clean = re.sub(r'[<>:"|?*]', '', clean).strip()
    return "Unknown" if not clean and default_to_unknown else clean

def is_junk_folder(folder_name):
    junk = ['sample', 'samples', 'extra', 'extras', 'proof', 'interview', 'interviews', '.zab', 'artwork']
    return folder_name.lower() in junk

def clean_search_query(text):
    text = re.sub(r'[\(\[\{].*?[\)\]\}]', '', text)
    text = re.sub(r'\b(mp3|m4b|128k|64k|192k|aac)\b', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\b(cd|disc|part|vol|v)\s*\d+\b', '', text, flags=re.IGNORECASE)
    text = text.replace('.', ' ').replace('_', ' ').replace('-', ' ')
    return re.sub(r'\s+', ' ', text).strip()

def get_candidates(force_refresh=False):
    history = load_json(HISTORY_FILE, [])
    library_items = get_library_map(force_refresh)
    
    if not os.path.exists(DOWNLOAD_DIR):
        return []

    candidate_map = {} 

    for root, dirs, files in os.walk(DOWNLOAD_DIR):
        audio_files = [f for f in files if f.lower().endswith(('.mp3', '.m4b', '.m4a', '.flac'))]
        
        if audio_files:
            folder_name = os.path.basename(root)
            if is_junk_folder(folder_name): continue

            has_real_subfolders = any(not d.startswith('.') for d in dirs)
            if has_real_subfolders: continue

            target_path = root
            target_name = folder_name
            parent_path = os.path.dirname(root)
            
            if re.match(r'^(cd|disc|part|vol|chapter)?\s*\d+$', folder_name, re.IGNORECASE):
                 if os.path.abspath(parent_path) != os.path.abspath(DOWNLOAD_DIR):
                     target_path = parent_path
                     target_name = os.path.basename(parent_path)

            if target_path not in candidate_map:
                candidate_map[target_path] = {
                    "path": target_path,
                    "name": target_name,
                    "clean": sanitize_for_matching(target_name)
                }

    final_list = []
    
    for path, data in candidate_map.items():
        folder_name = data['name']
        clean_dl = data['clean']
        full_path = data['path']
        
        status = 0 
        match_path = None
        
        if full_path in history:
            status = 3
        else:
            for lib_item in library_items:
                clean_lib = lib_item['clean']
                if not clean_lib: continue
                
                if len(clean_lib) > 4 and (clean_lib in clean_dl or clean_dl in clean_lib):
                    match_path = lib_item['path']
                    if os.path.exists(os.path.join(match_path, "metadata.json")):
                        status = 2
                    else:
                        status = 1
                    break
        
        prefix = ""
        if status == 3: prefix = "✅ (History) "
        elif status == 2: prefix = "✅ "
        elif status == 1: prefix = "🟨 "
        
        final_list.append({
            "label": f"{prefix}{folder_name}",
            "path": full_path,
            "type": "dir",
            "status": status,
            "match_path": match_path,
            "name": folder_name
        })

    return sorted(final_list, key=lambda x: (1 if x['status'] >= 2 else 0, x['status'] == 2, x['name']))

def fetch_metadata(query):
    try:
        headers = {'User-Agent': 'TidyBooks/1.0'}
        params = {'q': query}
        r = requests.get(AUDNEXUS_API, params=params, headers=headers, timeout=15)
        if r.status_code == 200: return r.json()
    except: pass
    return []

def tag_file(file_path, author, title, series, desc, cover_url, year, track_num, total_tracks):
    ext = os.path.splitext(file_path)[1].lower()
    try:
        if ext in ['.m4b', '.m4
