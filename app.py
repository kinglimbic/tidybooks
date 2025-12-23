import streamlit as st
import os
import shutil
import requests
import json
import re
from mutagen.mp4 import MP4, MP4Cover
from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, COMM, TRCK
import time

# --- Configuration ---
DOWNLOAD_DIR = "/downloads"
LIBRARY_DIR = "/audiobooks"
HISTORY_FILE = "processed_log.json"
AUDNEXUS_API = "https://api.audnexus.com/books"

st.set_page_config(page_title="TidyBooks", layout="wide", page_icon="📚")

# --- Helper Functions ---

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, 'r') as f:
            return json.load(f)
    return []

def save_to_history(path):
    history = load_history()
    if path not in history:
        history.append(path)
        with open(HISTORY_FILE, 'w') as f:
            json.dump(history, f)

def sanitize_filename(name):
    if not name: return "Unknown"
    clean = name.replace("/", "-").replace("\\", "-")
    return re.sub(r'[<>:"|?*]', '', clean).strip()

def get_candidates():
    """
    Scans for items and assigns status:
    Status 0: New/Untidy (White)
    Status 1: Exists in Library (Yellow)
    Status 2: Already Processed (Green)
    """
    history = load_history()
    candidates = []
    
    if not os.path.exists(DOWNLOAD_DIR):
        return []

    # Get list of folders currently in the Library (for the Yellow check)
    try:
        library_folders = [f for f in os.listdir(LIBRARY_DIR) if os.path.isdir(os.path.join(LIBRARY_DIR, f))]
    except:
        library_folders = []

    for item in os.listdir(DOWNLOAD_DIR):
        full_path = os.path.join(DOWNLOAD_DIR, item)
        is_dir = os.path.isdir(full_path)
        
        # Filter for audio content
        has_audio = False
        if is_dir:
            for root, _, files in os.walk(full_path):
                if any(f.lower().endswith(('.mp3', '.m4b', '.m4a', '.flac')) for f in files):
                    has_audio = True
                    break
        elif item.lower().endswith(('.mp3', '.m4b', '.m4a', '.flac')):
            has_audio = True

        if has_audio:
            # --- STATUS LOGIC ---
            status = 0 # Default: Untidy
            display_prefix = ""
            match_path = None # Where the "Messy" version exists

            # 1. Check Green (History)
            if full_path in history:
                status = 2
                display_prefix = "✅ "
            
            # 2. Check Yellow (Exists in Library but not in History)
            # We look for a folder in /audiobooks that matches the download name
            elif item in library_folders:
                status = 1
                display_prefix = "🟨 "
                match_path = os.path.join(LIBRARY_DIR, item)

            # Determine type
            type_str = "dir" if is_dir else "file"
            
            # Label for the UI
            label = f"{display_prefix}{item}"
            
            candidates.append({
                "label": label,
                "path": full_path,
                "type": type_str,
                "status": status,
                "match_path": match_path,
                "name": item
            })

    # Sort: Status 0 (New) -> Status 1 (Yellow) -> Status 2 (Green/Bottom)
    # Secondary Sort: Alphabetical
    return sorted(candidates, key=lambda x: (x['status'], x['name']))

def fetch_metadata(query):
    try:
        params = {'q': query}
        r = requests.get(AUDNEXUS_API, params=params)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        st.error(f"API Error: {e}")
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
                    img_data = requests.get(cover_url).content
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
            if desc: audio.add(COMM(encoding=3, lang='eng', desc='Description', text=desc))
            if cover_url:
                try:
                    img_data = requests.get(cover_url).content
                    audio.add(APIC(3, 'image/jpeg', 3, 'Front Cover', img_data))
                except: pass
            audio.save(file_path)
    except Exception as e:
        print(f"Error tagging: {e}")

def process_selection(source_data, author, title, series, series_part, desc, cover_url, narrator, publish_year):
    """
    Handles Copying OR Moving (Fix Mode).
    source_data contains: path, type, status, match_path
    """
    
    # --- DETERMINE MODE ---
    # If Status is Yellow (1), we FIX the existing library files (MOVE).
    # If Status is White (0), we IMPORT from downloads (COPY).
    mode = "COPY"
    working_source_path = source_data['path'] # Default to download folder
    
    if source_data['status'] == 1 and source_data['match_path']:
        mode = "FIX"
        working_source_path = source_data['match_path'] # Switch to library folder
        st.info(f"🟨 Fix Mode Detected: Reorganizing existing files from {working_source_path}")

    # 1. Setup Destination
    clean_author = sanitize_filename(author)
    clean_title = sanitize_filename(title)
    clean_series = sanitize_filename(series)
    
    if clean_series:
        dest_base_folder = os.path.join(LIBRARY_DIR, clean_author, clean_series, clean_title)
    else:
        dest_base_folder = os.path.join(LIBRARY_DIR, clean_author, clean_title)
    
    os.makedirs(dest_base_folder, exist_ok=True)

    # 2. Gather Files
    files_to_process = []
    if source_data['type'] == "dir":
        for root, _, files in os.walk(working_source_path):
            for file in files:
                if file.lower().endswith(('.mp3', '.m4b', '.m4a', '.flac')):
                    files_to_process.append(os.path.join(root, file))
        files_to_process.sort() 
    else:
        files_to_process.append(working_source_path)

    total_files = len(files_to_process)
    pad_length = len(str(total_files))
    if pad_length < 2: pad_length = 2 

    # 3. Process Batch
    progress_bar = st.progress(0)
    status_text = st.empty()

    for i, src_file in enumerate(files_to_process):
        status_text.text(f"Processing track {i+1} of {total_files}...")
        ext = os.path.splitext(src_file)[1]
        
        # Naming: 01 - Title.ext
        if total_files > 1:
            track_str = str(i+1).zfill(pad_length)
            new_filename = f"{track_str} - {clean_title}{ext}"
        else:
            new_filename = f"{clean_title}{ext}"
            
        dest_file_path = os.path.join(dest_base_folder, new_filename)
        
        # ACTION: COPY VS MOVE
        if mode == "FIX":
            # Avoid error if moving file to itself
            if os.path.abspath(src_file) != os.path.abspath(dest_file_path):
                shutil.move(src_file, dest_file_path)
        else:
            shutil.copy2(src_file, dest_file_path)
        
        # Tag
        tag_file(dest_file_path, author, title, series, desc, cover_url, publish_year, i+1, total_files)
        progress_bar.progress((i + 1) / total_files)

    # 4. Cleanup (Only in Fix Mode)
    if mode == "FIX" and source_data['type'] == "dir":
        # Remove the old messy folder if it is now empty
        try:
            shutil.rmtree(working_source_path)
        except Exception as e:
            st.warning(f"Could not delete old folder: {e}")

    # 5. Metadata JSON
    abs_metadata = {
        "title": title,
        "authors": [author],
        "series": [series] if series else [],
        "description": desc,
        "narrators": [narrator] if narrator else [],
        "publishYear": publish_year,
        "cover": cover_url
    }
    if series and series_part:
        try: abs_metadata["series"] = [{"sequence": series_part, "name": series}]
        except: abs_metadata["series"] = [series]

    with open(os.path.join(dest_base_folder, "metadata.json"), 'w', encoding='utf-8') as f:
        json.dump(abs_metadata, f, indent=4)

    # 6. Mark Download Path as Done (Even if we fixed the library copy, we mark the download as processed)
    save_to_history(source_data['path'])

    st.success(f"✅ TidyBooks: Successfully {'Fixed' if mode == 'FIX' else 'Imported'} {clean_title}")
    st.balloons()
    time.sleep(2)
    st.rerun()

# --- GUI Layout ---
st.title("🎧 TidyBooks")

col1, col2 = st.columns([1, 2])

with col1:
    st.subheader("📂 Untidy Queue")
    if st.button("Refresh List"):
        st.rerun()
        
    items = get_candidates()
    
    if not items:
        st.info("No items found.")
        selected_item = None
    else:
        # Display list with indicators
        selected_label = st.radio("Select Book:", [x['label'] for x in items], index=0)
        selected_item = next((x for x in items if x['label'] == selected_label), None)

with col2:
    if selected_item:
        folder_name = selected_item['name']
        
        # Header Dynamic Status
        if selected_item['status'] == 1:
            st.warning("⚠️ **Found in Library:** Files exist but may be unorganized. 'Make Tidy' will fix them in place (Move) instead of copying.")
        elif selected_item['status'] == 2:
            st.success("✅ **Already Processed:** This book is in your history.")

        st.subheader("✏️ Book Details")
        st.caption(f"Target: `{folder_name}`")
        
        with st.expander("🔍 Search Database", expanded=True):
            c_search, c_btn = st.columns(
