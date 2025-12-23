import streamlit as st
import pandas as pd
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
GOOGLE_BOOKS_API = "https://www.googleapis.com/books/v1/volumes"

os.makedirs(DATA_DIR, exist_ok=True)

st.set_page_config(page_title="TidyBooks", layout="wide", page_icon="📚")

# --- Initialize Session State ---
default_keys = ['form_auth', 'form_title', 'form_narr', 'form_series', 'form_part', 'form_year', 'form_desc', 'form_img']
for key in default_keys:
    if key not in st.session_state: st.session_state[key] = ""
if 'exp_path' not in st.session_state: st.session_state['exp_path'] = DOWNLOAD_DIR
if 'exp_root' not in st.session_state: st.session_state['exp_root'] = DOWNLOAD_DIR
if 'sync_selection' not in st.session_state: st.session_state['sync_selection'] = None
# Stores the currently selected item data regardless of which tab it came from
if 'current_selection_data' not in st.session_state: st.session_state['current_selection_data'] = None

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

# --- Cached Operations ---
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

@st.cache_data(ttl=600, show_spinner="Scanning downloads...")
def scan_downloads_snapshot():
    if not os.path.exists(DOWNLOAD_DIR): return []
    
    candidates = []
    seen_paths = set()

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

            if target_path not in seen_paths:
                seen_paths.add(target_path)
                candidates.append({
                    "path": target_path,
                    "name": target_name,
                    "clean": sanitize_for_matching(target_name)
                })
    return candidates

def get_candidates_with_status():
    raw_candidates = scan_downloads_snapshot()
    history = load_json(HISTORY_FILE, [])
    cached_lib = load_json(CACHE_FILE, None)
    library_items = cached_lib if (cached_lib and isinstance(cached_lib, list)) else []
    
    final_list = []
    for data in raw_candidates:
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
        
        # New Status Icons for separate tabs
        status_icon = "⚪"
        if status == 3: status_icon = "✅"
        elif status == 2: status_icon = "✅"
        elif status == 1: status_icon = "🟡"
        
        final_list.append({
            "path": full_path,
            "name": folder_name,
            "status_code": status,
            "State": status_icon, 
            "match_path": match_path,
        })

    return final_list

def fetch_metadata(query):
    try:
        params = {"q": query, "maxResults": 10, "langRestrict": "en"}
        r = requests.get(GOOGLE_BOOKS_API, params=params, timeout=10)
        r.raise_for_status() 
        data = r.json()
        results = []
        for item in data.get('items', []):
            info = item.get('volumeInfo', {})
            img_links = info.get('imageLinks', {})
            img = img_links.get('thumbnail', '') or img_links.get('smallThumbnail', '')
            img = img.replace('http:', 'https:')

            auth_list = info.get('authors', [])
            primary_author = auth_list[0] if auth_list else ""
            possible_narrators = ", ".join(auth_list[1:]) if len(auth_list) > 1 else ""

            results.append({
                "title": info.get('title', ''),
                "authors": primary_author,
                "narrators": possible_narrators, 
                "seriesPrimary": "", 
                "seriesPrimarySequence": "",
                "summary": info.get('description', ''),
                "image": img,
                "releaseDate": info.get('publishedDate', '')
            })
        return results
    except Exception as e:
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
    # If messy copy exists (status 1), we FIX (Move) instead of Copy
    if source_data['status_code'] == 1 and source_data['match_path']:
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
    st.cache_data.clear()
    st.session_state['current_selection_data'] = None
    time.sleep(1)
    st.rerun()

# --- MAIN UI ---
st.sidebar.title("🛠️ Tools")

if st.sidebar.button("🔄 Refresh Downloads"):
    st.cache_data.clear()
    st.success("Cache cleared!")
    st.rerun()

if st.sidebar.button("📉 Update Library Map"):
    with st.spinner("Scanning library..."):
        scan_library_now()
    st.success("Library updated!")
    st.rerun()

# --- FILE EXPLORER with SYNC ---
st.sidebar.markdown("---")
with st.sidebar.expander("📂 File System Explorer", expanded=False):
    root_options = {"Downloads": DOWNLOAD_DIR, "Audiobooks": LIBRARY_DIR}
    selected_root_label = st.selectbox("Volume:", list(root_options.keys()))
    new_root = root_options[selected_root_label]
    if st.session_state['exp_root'] != new_root:
        st.session_state['exp_root'] = new_root
        st.session_state['exp_path'] = new_root

    current_path = st.session_state['exp_path']
    st.caption(f"📍 `{current_path}`")

    if current_path != new_root:
        if st.button("⬆️ Up Level"):
            st.session_state['exp_path'] = os.path.dirname(current_path)
            st.rerun()
    
    try:
        items = sorted(os.listdir(current_path))
        dirs = [i for i in items if os.path.isdir(os.path.join(current_path, i))]
        files = [i for i in items if not os.path.isdir(os.path.join(current_path, i))]

        if dirs:
            st.markdown("**Folders:**")
            for d in dirs:
                if st.button(f"📁 {d}", key=f"dir_{d}"):
                    new_path = os.path.join(current_path, d)
                    st.session_state['exp_path'] = new_path
                    st.session_state['sync_selection'] = new_path
                    st.rerun()
        if files:
            st.markdown("**Files:**")
            for f in files: st.text(f"📄 {f}")
        if not dirs and not files: st.caption("(Empty Folder)")
    except Exception as e: st.error(f"Access Denied: {e}")

# --- MAIN PAGE ---
col1, col2 = st.columns([1, 2])

# Load Items
all_items = get_candidates_with_status()

# Separation Logic
# Status 0 = New/Untidy
# Status 1,2,3 = Imported/Existing
new_items = [x for x in all_items if x['status_code'] == 0]
existing_items = [x for x in all_items if x['status_code'] > 0]

# --- SYNC LOGIC (Pre-Filter) ---
# If Explorer was clicked, we try to find that path in EITHER list
if st.session_state['sync_selection']:
    sync_target = st.session_state['sync_selection']
    # Check if this item exists in all_items
    matching = [x for x in all_items if x['path'] == sync_target or sync_target.startswith(x['path'])]
    if matching:
        target_item = matching[0]
        st.session_state['current_selection_data'] = target_item
        st.toast(f"Jumped to: {target_item['name']}")
        st.session_state['sync_selection'] = None # Clear trigger

with col1:
    tab_new, tab_exist = st.tabs(["🆕 Untidy Queue", "📚 Already Imported"])
    
    # --- TAB 1: NEW ITEMS ---
    with tab_new:
        if not new_items:
            st.info("Nothing new to import!")
        else:
            df_new = pd.DataFrame(new_items)
            sel_new = st.dataframe(
                df_new[['name']],
                column_config={"name": st.column_config.TextColumn("Folder Name")},
                use_container_width=True,
                hide_index=True,
                height=500, # Fixed height so it doesn't collapse
                on_select="rerun",
                selection_mode="single-row",
                key="grid_new"
            )
            if sel_new.selection.rows:
                st.session_state['current_selection_data'] = new_items[sel_new.selection.rows[0]]

    # --- TAB 2: EXISTING ITEMS ---
    with tab_exist:
        if not existing_items:
            st.info("No imported items found yet.")
        else:
            df_exist = pd.DataFrame(existing_items)
            sel_exist = st.dataframe(
                df_exist[['State', 'name']],
                column_config={
                    "State": st.column_config.TextColumn("State", width="small"),
                    "name": st.column_config.TextColumn("Folder Name")
                },
                use_container_width=True,
                hide_index=True,
                height=500,
                on_select="rerun",
                selection_mode="single-row",
                key="grid_exist"
            )
            if sel_exist.selection.rows:
                st.session_state['current_selection_data'] = existing_items[sel_exist.selection.rows[0]]

# --- EDITOR PANE ---
selected_item = st.session_state.get('current_selection_data')

with col2:
    if selected_item:
        st.subheader("✏️ Editor")
        st.caption(f"Path: `{selected_item['name']}`")
        
        # Helper to clear selection
        if st.button("❌ Close Selection"):
            st.session_state['current_selection_data'] = None
            st.rerun()

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
            with st.spinner("Searching Google Books..."):
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
            if selected_item['status_code'] == 1: lbl = "Fix Structure (Move)"
            
            if st.form_submit_button(lbl, type="primary"):
                if auth and titl:
                    process_selection(selected_item, auth, titl, seri, part, desc, img, narr, year)
                else: st.error("Author/Title Required")
    else:
        st.info("Select a book from the left to edit.")
