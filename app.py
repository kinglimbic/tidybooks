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
            
            # Skip unless all subfolders are hidden
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
        if ext in ['.m4b', '.m4a']:
            audio = MP4(file_path)
            if audio.tags is None: audio.add_tags()
            audio.tags['\xa9nam'] = title; audio.tags['\xa9ART'] = author
            audio.tags['\xa9alb'] = series if series else title; audio.tags['desc'] = desc
            audio.tags['trkn'] = [(track_num, total_tracks)]
            if year: audio.tags['\xa9day'] = year
            if cover_url:
                audio.tags['covr'] = [MP4Cover(requests.get(cover_url).content, imageformat=MP4Cover.FORMAT_JPEG)]
            audio.save()
        elif ext == '.mp3':
            try: audio = ID3(file_path) 
            except: audio = ID3()
            audio.add(TIT2(encoding=3, text=title)); audio.add(TPE1(encoding=3, text=author))
            audio.add(TALB(encoding=3, text=series if series else title))
            audio.add(TRCK(encoding=3, text=f"{track_num}/{total_tracks}"))
            if desc: audio.add(COMM(encoding=3, lang='eng', desc='Description', text=desc))
            if cover_url:
                audio.add(APIC(3, 'image/jpeg', 3, 'Front Cover', requests.get(cover_url).content))
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
        ext = os.path.splitext(src)[1]
        name = f"{str(i+1).zfill(pad)} - {clean_title}{ext}" if total > 1 else f"{clean_title}{ext}"
        dst = os.path.join(dest_base, name)
        
        if mode == "FIX" and os.path.abspath(src) != os.path.abspath(dst): shutil.move(src, dst)
        else: shutil.copy2(src, dst)
        tag_file(dst, author, title, series, desc, cover_url, publish_year, i+1, total)
        bar.progress((i+1)/total)

    if mode == "FIX": 
        try: shutil.rmtree(working_source_path) 
        except: pass

    abs_meta = {
        "title": title, "authors": [author], "series": [series] if series else [],
        "description": desc, "narrators": [narrator] if narrator else [],
        "publishYear": publish_year, "cover": cover_url
    }
    if series and series_part:
        try: abs_meta["series"] = [{"sequence": series_part, "name": series}]
        except: abs_meta["series"] = [series]

    with open(os.path.join(dest_base, "metadata.json"), 'w') as f: json.dump(abs_meta, f, indent=4)

    hist = load_json(HISTORY_FILE, [])
    if source_data['path'] not in hist:
        hist.append(source_data['path'])
        save_json(HISTORY_FILE, hist)
    
    st.success(f"✅ Done: {clean_title}")
    for key in default_keys: st.session_state[key] = ""
    time.sleep(1)
    st.rerun()

# --- MAIN UI ---
st.sidebar.title("🛠️ Tools")

# --- 1. Library Scanner ---
if st.sidebar.button("📉 Update Library Map"):
    with st.spinner("Scanning library..."):
        scan_library_now()
    st.success("Library updated!")
    st.rerun()

# --- 2. File Explorer (NEW) ---
st.sidebar.markdown("---")
with st.sidebar.expander("📂 File System Explorer", expanded=False):
    # Selector for Root
    root_options = {"Downloads": DOWNLOAD_DIR, "Audiobooks": LIBRARY_DIR}
    selected_root_label = st.selectbox("Volume:", list(root_options.keys()))
    new_root = root_options[selected_root_label]
    
    # Handle root switching
    if st.session_state['exp_root'] != new_root:
        st.session_state['exp_root'] = new_root
        st.session_state['exp_path'] = new_root

    current_path = st.session_state['exp_path']
    st.caption(f"📍 `{current_path}`")

    # Navigation: UP
    if current_path != new_root:
        if st.button("⬆️ Up Level"):
            st.session_state['exp_path'] = os.path.dirname(current_path)
            st.rerun()
    
    # Listing Content
    try:
        items = sorted(os.listdir(current_path))
        dirs = [i for i in items if os.path.isdir(os.path.join(current_path, i))]
        files = [i for i in items if not os.path.isdir(os.path.join(current_path, i))]

        if dirs:
            st.markdown("**Folders:**")
            for d in dirs:
                # Using columns for tighter layout
                if st.button(f"📁 {d}", key=f"dir_{d}"):
                    st.session_state['exp_path'] = os.path.join(current_path, d)
                    st.rerun()
        
        if files:
            st.markdown("**Files:**")
            for f in files:
                st.text(f"📄 {f}")
                
        if not dirs and not files:
            st.caption("(Empty Folder)")
            
    except Exception as e:
        st.error(f"Access Denied: {e}")

# --- MAIN PAGE ---
col1, col2 = st.columns([1, 2])

with col1:
    st.subheader("📂 Untidy Queue")
    items = get_candidates()
    if not items:
        st.info("Queue Empty.")
        selected_item = None
    else:
        label_map = {f"{x['label']}##{i}": x for i, x in enumerate(items)}
        key = st.radio("Select Book", list(label_map.keys()), format_func=lambda k: label_map[k]['label'].split('##')[0])
        selected_item = label_map[key]

with col2:
    if selected_item:
        st.subheader("✏️ Editor")
        st.caption(f"Path: `{selected_item['name']}`")
        
        clean_q = clean_search_query(selected_item['name'])
        q = st.text_input("Search", value=clean_q)
        
        def update_form_state():
            if 'result_selector' in st.session_state and 'search_results' in st.session_state:
                opts = {f"{b.get('authors')} - {b.get('title')}": b for b in st.session_state['search_results']}
                sel_key = st.session_state['result_selector']
                if sel_key in opts:
                    data = opts[sel_key]
                    st.session_state['form_auth'] = data.get('authors', '')
                    st.session_state['form_title'] = data.get('title', '')
                    st.session_state['form_narr'] = data.get('narrators', '')
                    st.session_state['form_series'] = data.get('seriesPrimary', '')
                    st.session_state['form_part'] = data.get('seriesPrimarySequence', '')
                    rd = data.get('releaseDate')
                    st.session_state['form_year'] = rd[:4] if rd else ''
                    st.session_state['form_desc'] = data.get('summary', '')
                    st.session_state['form_img'] = data.get('image', '')

        if st.button("Search"):
            with st.spinner("Searching..."):
                res = fetch_metadata(q)
                if res:
                    st.session_state['search_results'] = res
                    first = f"{res[0].get('authors')} - {res[0].get('title')}"
                    st.session_state['result_selector'] = first
                    update_form_state()
                else: st.warning("No matches found.")

        if 'search_results' in st.session_state:
            opts = [f"{b.get('authors')} - {b.get('title')}" for b in st.session_state['search_results']]
            if opts: st.selectbox("Results", opts, key='result_selector', on_change=update_form_state)

        with st.form("main"):
            c1, c2 = st.columns(2)
            auth = c1.text_input("Author", key='form_auth')
            titl = c1.text_input("Title", key='form_title')
            narr = c1.text_input("Narrator", key='form_narr')
            seri = c2.text_input("Series", key='form_series')
            part = c2.text_input("Part #", key='form_part')
            year = c2.text_input("Year", key='form_year')
            desc = st.text_area("Desc", key='form_desc')
            img = st.text_input("Cover URL", key='form_img')
            
            if img: st.image(img, width=100)
            
            lbl = "Make Tidy & Import"
            if selected_item['status'] == 1: lbl = "Fix Structure (Move)"
            
            if st.form_submit_button(lbl, type="primary"):
                if auth and titl:
                    process_selection(selected_item, auth, titl, seri, part, desc, img, narr, year)
                else: st.error("Author/Title Required")
