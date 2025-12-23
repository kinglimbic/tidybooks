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
            if desc: audio.add(COMM(encoding=3, lang='eng', desc='Description', text=desc))
            if cover_url:
                try:
                    img_data = requests.get(cover_url, timeout=5).content
                    audio.add(APIC(3, 'image/jpeg', 3, 'Front Cover', img_data))
                except: pass
            audio.save(file_path)
    except Exception as e:
        print(f"Error tagging: {e}")

def process_selection(source_data, author, title, series, series_part, desc, cover_url, narrator, publish_year):
    mode = "COPY"
    working_source_path = source_data['path']
    
    # Logic: If Yellow (Status 1), we FIX (Move). 
    # If Green (Status 2), we usually leave it alone, but user can re-import if they want.
    if source_data['status'] == 1 and source_data['match_path']:
        mode = "FIX"
        working_source_path = source_data['match_path']

    clean_author = sanitize_filename(author)
    clean_title = sanitize_filename(title)
    clean_series = sanitize_filename(series)
    
    if clean_series:
        dest_base_folder = os.path.join(LIBRARY_DIR, clean_author, clean_series, clean_title)
    else:
        dest_base_folder = os.path.join(LIBRARY_DIR, clean_author, clean_title)
    
    os.makedirs(dest_base_folder, exist_ok=True)

    files_to_process = []
    for root, _, files in os.walk(working_source_path):
        for file in files:
            if file.lower().endswith(('.mp3', '.m4b', '.m4a', '.flac')):
                files_to_process.append(os.path.join(root, file))
    files_to_process.sort() 

    total_files = len(files_to_process)
    pad_length = len(str(total_files))
    if pad_length < 2: pad_length = 2 

    progress_bar = st.progress(0)
    status_text = st.empty()

    for i, src_file in enumerate(files_to_process):
        status_text.text(f"Processing track {i+1} of {total_files}...")
        ext = os.path.splitext(src_file)[1]
        
        if total_files > 1:
            track_str = str(i+1).zfill(pad_length)
            new_filename = f"{track_str} - {clean_title}{ext}"
        else:
            new_filename = f"{clean_title}{ext}"
            
        dest_file_path = os.path.join(dest_base_folder, new_filename)
        
        if mode == "FIX":
            if os.path.abspath(src_file) != os.path.abspath(dest_file_path):
                shutil.move(src_file, dest_file_path)
        else:
            shutil.copy2(src_file, dest_file_path)
        
        tag_file(dest_file_path, author, title, series, desc, cover_url, publish_year, i+1, total_files)
        progress_bar.progress((i + 1) / total_files)

    if mode == "FIX":
        try: shutil.rmtree(working_source_path)
        except: pass

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

    # Update History and Clear Cache
    history = load_json(HISTORY_FILE, [])
    if source_data['path'] not in history:
        history.append(source_data['path'])
        save_json(HISTORY_FILE, history)
        
    if os.path.exists(CACHE_FILE):
        os.remove(CACHE_FILE)

    st.success(f"✅ Done: {clean_title}")
    st.balloons()
    time.sleep(1)
    st.rerun()

# --- GUI Layout ---
st.title("🎧 TidyBooks")

col1, col2 = st.columns([1, 2])

with col1:
    st.subheader("📂 Untidy Queue")
    
    if st.button("🔄 Force Refresh Library"):
        st.cache_data.clear()
        if os.path.exists(CACHE_FILE): os.remove(CACHE_FILE)
        st.rerun()

    items = get_candidates()
    
    if not items:
        st.info("No audio folders found.")
        selected_item = None
    else:
        label_map = {f"{x['label']} (ID:{i})": x for i, x in enumerate(items)}
        selected_key = st.radio("Select Book:", list(label_map.keys()), index=0, format_func=lambda x: label_map[x]['label'])
        selected_item = label_map[selected_key]

with col2:
    if selected_item:
        folder_name = selected_item['name']
        
        if selected_item['status'] == 2:
            st.success(f"✅ **Properly Imported:** Found in Library with valid metadata.")
        elif selected_item['status'] == 1:
            st.warning(f"🟨 **Messy Copy Found:** A folder named '{folder_name}' exists in Library but lacks TidyBooks metadata.")
        elif selected_item['status'] == 3:
             st.success("✅ **In History:** You have processed this specific download before.")

        st.subheader("✏️ Book Details")
        st.caption(f"Folder: `{folder_name}`")
        
        with st.expander("🔍 Search Database", expanded=True):
            c_search, c_btn = st.columns([3,1])
            with c_search:
                clean_guess = folder_name.replace("_", " ").replace("-", " ")
                search_query = st.text_input("Search Title", value=clean_guess)
            with c_btn:
                st.write("##")
                do_search = st.button("Search")
            
            found_meta = {}
            if do_search:
                results = fetch_metadata(search_query)
                if results:
                    st.session_state['search_results'] = results
            
            if 'search_results' in st.session_state:
                options = {f"{b.get('authors')} - {b.get('title')}": b for b in st.session_state['search_results']}
                selected_meta_key = st.selectbox("Quick Fill:", options.keys())
                if selected_meta_key:
                    found_meta = options[selected_meta_key]

        with st.form("book_details"):
            f_auth = found_meta.get('authors', '')
            f_title = found_meta.get('title', '')
            f_series = found_meta.get('seriesPrimary', '')
            f_part = found_meta.get('seriesPrimarySequence', '')
            f_desc = found_meta.get('summary', '')
            f_img = found_meta.get('image', '')
            f_narr = found_meta.get('narrators', '')
            f_year = found_meta.get('releaseDate', '')[:4] if found_meta.get('releaseDate') else ''

            c1, c2 = st.columns(2)
            with c1:
                new_author = st.text_input("Author", value=f_auth)
                new_title = st.text_input("Title", value=f_title)
                new_narrator = st.text_input("Narrator", value=f_narr)
            with c2:
                new_series = st.text_input("Series Name", value=f_series)
                new_part = st.text_input("Series Part #", value=f_part)
                new_year = st.text_input("Year", value=f_year)
            
            new_desc = st.text_area("Description", value=f_desc, height=150)
            cover_img = st.text_input("Cover Image URL", value=f_img)

            if cover_img:
                st.image(cover_img, width=120)

            st.write("---")
            btn_label = "🚀 Make Tidy & Import"
            if selected_item['status'] == 1:
                btn_label = "🛠️ Fix Library Structure (Move)"
            
            submitted = st.form_submit_button(btn_label, type="primary")
            
            if submitted:
                if new_author and new_title:
                    process_selection(selected_item, new_author, new_title, new_series, new_part, new_desc, cover_img, new_narrator, new_year)
