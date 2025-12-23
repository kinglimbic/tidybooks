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

# We are switching to Google Books (More reliable than Audnexus)
GOOGLE_BOOKS_API = "https://www.googleapis.com/books/v1/volumes"

os.makedirs(DATA_DIR, exist_ok=True)

st.set_page_config(page_title="TidyBooks", layout="wide", page_icon="📚")

# --- Initialize Session State ---
default_keys = ['form_auth', 'form_title', 'form_narr', 'form_series', 'form_part', 'form_year', 'form_desc', 'form_img']
for key in default_keys:
    if key not in st.session_state: st.session_state[key] = ""

# File Explorer State
if 'exp_path' not in st.session_state: st.session_state['exp_path'] = DOWNLOAD_DIR
if 'exp_root' not in st.session_state: st.session_state['exp_root'] = DOWNLOAD_DIR

# --- Persistence ---
def load_json(filepath, default=None):
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r') as f: return json.load(f)
        except: pass
    return default if default is not None else []

def save_json(filepath, data):
    with open(filepath, 'w') as f: json.dump(data, f)

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

# --- Library Scanning ---
def scan_library_now():
    library_items = []
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

def get_candidates():
    history = load_json(HISTORY_FILE, [])
    cached_lib = load_json(CACHE_FILE, None)
    library_items = cached_lib if (cached_lib and isinstance(cached_lib, list)) else []
    
    if not os.path.exists(DOWNLOAD_DIR): return []

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
        elif library_items:
            for lib_item in library_items:
                clean_lib = lib_item.get('clean')
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

# --- REPLACED: Google Books Search Logic ---
def fetch_metadata(query):
    try:
        params = {"q": query, "maxResults": 10, "langRestrict": "en"}
        r = requests.get(GOOGLE_BOOKS_API, params=params, timeout=10)
        
        # This will FORCE the code to tell us if the network failed
        r.raise_for_status() 
        
        data = r.json()
        results = []
        
        for item in data.get('items', []):
            info = item.get('volumeInfo', {})
            
            # Google handles images weirdly, try to get HTTPS thumbnail
            img_links = info.get('imageLinks', {})
            img = img_links.get('thumbnail', '') or img_links.get('smallThumbnail', '')
            img = img.replace('http:', 'https:')

            results.append({
                "title": info.get('title', ''),
                "authors": ", ".join(info.get('authors', [])),
                "narrators": "", # Google Books rarely has narrator data
                "seriesPrimary": "", # Google Books puts series in title often
                "seriesPrimarySequence": "",
                "summary": info.get('description', ''),
                "image": img,
                "releaseDate": info.get('publishedDate', '')
            })
            
        return results

    except Exception as e:
        # VISIBLE ERROR REPORTING
        st.error(f"❌ Connection Error: {e}")
        return []

def tag_file(file_path, author, title, series, desc, cover_url, year, track_num, total_tracks):
    ext = os.path.splitext(file_path)[1].lower()
    try:
        if ext in ['.m4b', '.m4a']:
            audio = MP4(file_path)
            if audio.tags is None: audio.add_tags()
            audio.tags['\xa9nam'] = title; audio.tags['\xa9ART'] = author
            audio.tags['\xa9alb'] = series if series else title; audio.tags['desc'] = desc
            audio.tags['trkn'] = [(track_num, total_tracks)]
            if year: audio.tags['\xa9day'] = year
            if cover_url:
                try: audio.tags['covr'] = [MP4Cover(requests.get(cover_url).content, imageformat=MP4Cover.FORMAT_JPEG)]
                except: pass
            audio.save()
        elif ext == '.mp3':
            try: audio = ID3(file_path) 
            except: audio = ID3()
            audio.add(TIT2(encoding=3, text=title)); audio.add(TPE1(encoding=3, text=author))
            audio.add(TALB(encoding=3, text=series if series else title))
            audio.add(TRCK(encoding=3, text=f"{track_num}/{total_tracks}"))
            if desc: audio.add(COMM(encoding=3, lang='eng', desc='Description', text=desc))
            if cover_url:
                try: audio.add(APIC(3, 'image/jpeg', 3, 'Front Cover', requests.get(cover_url).content))
                except: pass
            audio.save(file_path)
    except: pass

def process_selection(source_data, author, title, series, series_part, desc, cover_url, narrator, publish_year):
    mode = "COPY"
    working_source_path = source_data['path']
    if source_data['status'] == 1 and source_data['match_path']:
        mode = "FIX"
        working_source_path = source_data['match_path']

    clean_author = sanitize_filename(author, True)
    clean_title = sanitize_filename(title, True)
    clean_series = sanitize_filename(series, False)
    
    dest_base = os.path.join(LIBRARY_DIR, clean_author, clean_series, clean_title) if clean_series else os.path.join(LIBRARY_DIR, clean_author, clean_title)
    os.makedirs(dest_base, exist_ok=True)

    files = []
    for root, _, fs in os.walk(working_source_path):
        for f in fs:
            if f.lower().endswith(('.mp3', '.m4b', '.m4a', '.flac')):
                files.append(os.path.join(root, f))
    files.sort()
    
    total = len(files)
    pad = max(2, len(str(total)))
    
    bar = st.progress(0)
    for i, src in enumerate(files):
        ext =
