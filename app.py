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
    Returns a dictionary: { "CleanFolderName": "/full/path/to/folder/in/library" }
    """
    if not force_refresh:
        cached = load_json(CACHE_FILE)
        if cached is not None: return cached
            
    try:
        library_map = {}
        # Recursive scan of the library
        for root, dirs, files in os.walk(LIBRARY_DIR):
            folder_name = os.path.basename(root)
            # Store both the raw name AND a cleaned version for fuzzy matching
            # We map the Clean Name to the Full Path
            clean_key = sanitize_for_matching(folder_name)
            if clean_key:
                library_map[clean_key] = root
        
        save_json(CACHE_FILE, library_map)
        return library_map
    except:
        return {}

# --- Helper Functions ---
def sanitize_for_matching(text):
    """
    Super aggressive cleaner for matching logic.
    Removes spaces, punctuation, common junk words.
    'Harry Potter - Book 1 (2000)' -> 'harrypotterbook1'
    """
    text = text.lower()
    text = re.sub(r'\b(audiobook|mp3|m4b|cd|disc|part|v|vol)\b', '', text)
    text = re.sub(r'[^a-z0-9]', '', text)
    return text

def sanitize_filename(name, default_to_unknown=False):
    if not name:
        return "Unknown" if default_to_unknown else ""
    clean = name.replace("/", "-").replace("\\", "-")
    clean = re.sub(r'[<>:"|?*]', '', clean).strip()
    if not clean and default_to_unknown:
        return "Unknown"
    return clean

def is_part_folder(folder_name):
    pattern = r"^(cd|disc|disk|part|vol|volume|chapter)\s*[\d\w]+$"
    simple_digit = r"^\d+$"
    if re.match(pattern, folder_name, re.IGNORECASE) or re.match(simple_digit, folder_name):
        return True
    return False

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
    library_map = get_library_map(force_refresh)
    
    if not os.path.exists(DOWNLOAD_DIR):
        return []

    candidate_map = {} 

    for root, dirs, files in os.walk(DOWNLOAD_DIR):
        audio_files = [f for f in files if f.lower().endswith(('.mp3', '.m4b', '.m4a', '.flac'))]
        
        if audio_files:
            folder_name = os.path.basename(root)
            parent_path = os.path.dirname(root)
            parent_name = os.path.basename(parent_path)
            
            if is_junk_folder(folder_name): continue

            # Skip subfolders unless they are hidden
            has_real_subfolders = False
            for d in dirs:
                if not d.startswith('.'): 
                    has_real_subfolders = True
                    break
            if has_real_subfolders: continue

            target_path = root
            target_name = folder_name
            
            if is_part_folder(folder_name):
                target_path = parent_path
                target_name = parent_name
                if os.path.abspath(target_path) == os.path.abspath(DOWNLOAD_DIR):
                    target_path = root
                    target_name = folder_name
            
            if target_path not in candidate_map:
                candidate_map[target_path] = {
                    "path": target_path,
                    "name": target_name
                }

    final_list = []
    
    for path, data in candidate_map.items():
        folder_name = data['name']
        full_path = data['path']
        
        status = 0 
        display_prefix = ""
        match_path = None
        
        # --- MATCHING LOGIC ---
        
        # 1. Exact History Match
        if full_path in history:
            status = 3
            display_prefix = "✅ (History) "
            
        else:
            # 2. Fuzzy Library Match
            # We clean the download folder name and check if it exists in our library map keys
            clean_download_name = sanitize_for_matching(folder_name)
            
            # Direct Key Match
            if clean_download_name in library_map:
                match_path = library_map[clean_download_name]
            
            # Partial Match (Slower, but catches "Title (2022)" vs "Title")
            if not match_path:
                for lib_key, lib_path in library_map.items():
                    if len(lib_key) > 5 and (lib_key in clean_download_name or clean_download_name in lib_key):
                        match_path = lib_path
                        break
            
            if match_path:
                if os.path.exists(os.path.join(match_path, "metadata.json")):
                    status = 2 
                    display_prefix = "✅ "
                else:
                    status = 1 
                    display_prefix = "🟨 "
        
        final_list.append({
            "label": f"{display_prefix}{folder_name}",
            "path": full_path,
            "type": "dir",
            "status": status,
            "match_path": match_path,
            "name": folder_name
        })

    return sorted(final_list, key=lambda x: (x['status'], x['name']))

def fetch_metadata(query):
    try:
        headers = {'User-Agent': 'TidyBooks/1.0'}
        params = {'q': query}
        r = requests.get(AUDNEXUS_API, params=params, headers=headers, timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        st.error(f"Search Error: {e}")
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
                    img_data = requests.get(cover_url, timeout=10).content
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
                    img_data = requests.get(cover_url, timeout=10).content
                    audio.add(APIC(3, 'image/jpeg', 3, 'Front Cover', img_data))
                except: pass
            audio.save(file_path)
    except Exception as e:
        print(f"Error tagging: {e}")

def process_selection(source_data, author, title, series, series_part, desc, cover_url, narrator, publish_year):
    mode = "COPY"
    working_source_path = source_data['path']
    
    if source_data['status'] == 1 and source_data['match_path']:
        mode = "FIX"
        working_source_path = source_data['match_path']

    clean_author = sanitize_filename(author, default_to_unknown=True)
    clean_title = sanitize_filename(title, default_to_unknown=True)
    clean_series = sanitize_filename(series, default_to_unknown=False)
    
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
            st.success(f"✅ **Properly Imported:** Found in Library.")
        elif selected_item['status'] == 1:
            st.warning(f"🟨 **Messy Copy Found:** A folder named '{folder_name}' (or similar) exists in Library.")
        elif selected_item['status'] == 3:
             st.success("✅ **In History:** Previously processed.")

        st.subheader("✏️ Book Details")
        st.caption(f"Folder: `{folder_name}`")
        
        with st.expander("🔍 Search Database", expanded=True):
            c_search, c_btn = st.columns([3,1])
            with c_search:
                clean_guess = clean_search_query(folder_name)
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
