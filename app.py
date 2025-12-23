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

# API ENDPOINTS
ITUNES_API = "https://itunes.apple.com/search"
GOOGLE_BOOKS_API = "https://www.googleapis.com/books/v1/volumes"

os.makedirs(DATA_DIR, exist_ok=True)

st.set_page_config(page_title="TidyBooks", layout="wide", page_icon="📚")

# --- Initialize Session State ---
default_keys = ['form_auth', 'form_title', 'form_narr', 'form_series', 'form_part', 'form_year', 'form_desc', 'form_img']
for key in default_keys:
    if key not in st.session_state: st.session_state[key] = ""

if 'exp_path' not in st.session_state: st.session_state['exp_path'] = DOWNLOAD_DIR
if 'sync_selection' not in st.session_state: st.session_state['sync_selection'] = None
if 'current_selection_data' not in st.session_state: st.session_state['current_selection_data'] = None
if 'search_provider' not in st.session_state: st.session_state['search_provider'] = "Apple Books"
if 'last_jumped_path' not in st.session_state: st.session_state['last_jumped_path'] = None
# Stores manually created bundles
if 'manual_books' not in st.session_state: st.session_state['manual_books'] = []

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

def sanitize_filename(name):
    if not name: return "Unknown"
    clean = name.replace("/", "-").replace("\\", "-")
    clean = re.sub(r'[<>:"|?*]', '', clean).strip()
    return "Unknown" if not clean else clean

def is_junk_folder(folder_name):
    junk = ['sample', 'samples', 'extra', 'extras', 'proof', 'interview', 'interviews', '.zab', 'artwork']
    return folder_name.lower() in junk

def clean_search_query(text):
    text = re.sub(r'[\(\[\{].*?[\)\]\}]', '', text)
    text = re.sub(r'\b(mp3|m4b|128k|64k|192k|aac)\b', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\b(cd|disc|part|vol|v)\s*\d+\b', '', text, flags=re.IGNORECASE)
    text = text.replace('.', ' ').replace('_', ' ').replace('-', ' ')
    return re.sub(r'\s+', ' ', text).strip()

def get_file_stem(filename):
    name = os.path.splitext(filename)[0].lower()
    name = re.sub(r'[\(\[\{].*?[\)\]\}]', ' ', name)
    name = re.sub(r'\b(part|pt|cd|disc|disk|track|chapter|vol|volume)\s*\d+\b', ' ', name)
    name = re.sub(r'[_\-\.]', ' ', name)
    name = re.sub(r'\b\d+\b', ' ', name)
    return re.sub(r'\s+', ' ', name).strip()

def natural_keys(text):
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', text)]

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
    seen_ids = set() 

    for root, dirs, files in os.walk(DOWNLOAD_DIR):
        audio_files = [f for f in files if f.lower().endswith(('.mp3', '.m4b', '.m4a', '.flac'))]
        
        if audio_files:
            folder_name = os.path.basename(root)
            if is_junk_folder(folder_name): continue
            
            # 1. Collection Logic
            if "collection" in folder_name.lower():
                groups = {}
                for f in audio_files:
                    stem = get_file_stem(f)
                    if not stem: stem = "unknown"
                    if stem not in groups: groups[stem] = []
                    groups[stem].append(f)
                
                for stem, file_list in groups.items():
                    unique_id = f"{root}|{stem}"
                    display_name = stem.title()
                    full_paths = [os.path.join(root, f) for f in file_list]
                    candidates.append({
                        "id": unique_id, "path": root, "name": display_name,
                        "clean": sanitize_for_matching(display_name),
                        "file_list": full_paths, "is_group": True 
                    })
            # 2. Standard Logic
            else:
                has_real_subfolders = any(not d.startswith('.') for d in dirs)
                if has_real_subfolders: continue 

                target_path = root
                target_name = folder_name
                parent_path = os.path.dirname(root)
                
                if re.match(r'^(cd|disc|part|vol|chapter)?\s*\d+$', folder_name, re.IGNORECASE):
                     if os.path.abspath(parent_path) != os.path.abspath(DOWNLOAD_DIR):
                         target_path = parent_path
                         target_name = os.path.basename(parent_path)

                unique_id = f"{target_path}|FOLDER"
                
                if unique_id not in seen_ids:
                    seen_ids.add(unique_id)
                    all_paths = [os.path.join(root, f) for f in audio_files]
                    candidates.append({
                        "id": unique_id, "path": target_path, "name": target_name,
                        "clean": sanitize_for_matching(target_name),
                        "file_list": all_paths, "is_group": False
                    })
    return candidates

def get_candidates_with_status():
    raw_candidates = scan_downloads_snapshot()
    manual_items = st.session_state.get('manual_books', [])
    all_candidates = manual_items + raw_candidates
    
    history = load_json(HISTORY_FILE, [])
    cached_lib = load_json(CACHE_FILE, None)
    library_items = cached_lib if (cached_lib and isinstance(cached_lib, list)) else []
    
    final_list = []
    
    for data in all_candidates:
        unique_id = data['id']
        clean_dl = data['clean']
        status = 0 
        match_path = None
        
        if unique_id in history:
            status = 3
        elif library_items:
            for lib_item in library_items:
                clean_lib = lib_item.get('clean')
                if not clean_lib: continue
                if len(clean_lib) > 4 and (clean_lib in clean_dl or clean_dl in clean_lib):
                    match_path = lib_item['path']
                    if os.path.exists(os.path.join(match_path, "metadata.json")): status = 2
                    else: status = 1
                    break
        
        status_icon = "⚪"
        if status == 3: status_icon = "✅"
        elif status == 2: status_icon = "✅"
        elif status == 1: status_icon = "🟡"
        
        display_name = data['name']
        if data.get('is_manual'): display_name = f"🛠️ {display_name}"
        
        final_list.append({
            "path": data['path'], "unique_id": unique_id,
            "name": display_name, "raw_name": data['name'],
            "status_code": status, "State": status_icon, 
            "match_path": match_path, "file_list": data['file_list'],
            "is_manual": data.get('is_manual', False)
        })

    return sorted(final_list, key=lambda x: (x['status_code'] > 0, natural_keys(x['raw_name'])))

# --- SEARCH ---
def fetch_metadata(query, provider):
    results = []
    try:
        if provider == "Apple Books":
            r = requests.get(ITUNES_API, params={'term': query, 'media': 'audiobook', 'limit': 10}, timeout=5)
            if r.status_code == 200:
                for item in r.json().get('results', []):
                    img = item.get('artworkUrl100', '').replace('100x100', '600x600')
                    results.append({
                        "title": item.get('collectionName'), "authors": item.get('artistName'),
                        "narrators": "", "series": "", "part": "", "summary": item.get('description', ''),
                        "image": img, "releaseDate": item.get('releaseDate', '')
                    })
        else: # Google
            r = requests.get(GOOGLE_BOOKS_API, params={"q": query, "maxResults": 10, "langRestrict": "en"}, timeout=5)
            if r.status_code == 200:
                for item in r.json().get('items', []):
                    info = item.get('volumeInfo', {})
                    img = info.get('imageLinks', {}).get('thumbnail', '').replace('http:', 'https:')
                    auths = info.get('authors', [])
                    results.append({
                        "title": info.get('title'), "authors": auths[0] if auths else "",
                        "narrators": ", ".join(auths[1:]) if len(auths)>1 else "",
                        "series": "", "part": "", "summary": info.get('description', ''),
                        "image": img, "releaseDate": info.get('publishedDate', '')
                    })
    except: pass
    return results

# --- PROCESSING ---
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
    files_to_process = source_data['file_list']
    
    if source_data['status_code'] == 1 and source_data['match_path']:
        mode = "FIX"
        working_source_path = source_data['match_path']
        files_to_process = []
        for root, _, fs in os.walk(working_source_path):
            for f in fs:
                if f.lower().endswith(('.mp3', '.m4b', '.m4a', '.flac')):
                    files_to_process.append(os.path.join(root, f))

    clean_author = sanitize_filename(author)
    clean_title = sanitize_filename(title)
    clean_series = sanitize_filename(series)
    
    dest_base = os.path.join(LIBRARY_DIR, clean_author, clean_series, clean_title) if clean_series else os.path.join(LIBRARY_DIR, clean_author, clean_title)
    os.makedirs(dest_base, exist_ok=True)

    files_to_process.sort()
    total = len(files_to_process)
    
    bar = st.progress(0)
    for i, src in enumerate(files_to_process):
        ext = os.path.splitext(src)[1]
        name = f"{str(i+1).zfill(max(2, len(str(total))))} - {clean_title}{ext}"
        dst = os.path.join(dest_base, name)
        
        if mode == "FIX" and os.path.abspath(src) != os.path.abspath(dst): shutil.move(src, dst)
        else: shutil.copy2(src, dst)
        tag_file(dst, author, title, series, desc, cover_url, publish_year, i+1, total)
        bar.progress((i+1)/total)

    if mode == "FIX" and source_data.get('match_path'): 
        try: shutil.rmtree(source_data['match_path']) 
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
    if source_data['unique_id'] not in hist:
        hist.append(source_data['unique_id'])
        save_json(HISTORY_FILE, hist)
    
    if source_data.get('is_manual'):
        st.session_state['manual_books'] = [b for b in st.session_state['manual_books'] if b['id'] != source_data['unique_id']]

    st.success(f"✅ Done: {clean_title}")
    st.cache_data.clear()
    st.session_state['current_selection_data'] = None
    time.sleep(1)
    st.rerun()

# --- MAIN LAYOUT (3 Columns) ---
# Column 1: Queue | Column 2: Editor | Column 3: Explorer
col1, col2, col3 = st.columns([1.5, 2, 1.5])

# ==================== COLUMN 1: QUEUE & LISTS ====================
all_items = get_candidates_with_status()
new_items = [x for x in all_items if x['status_code'] == 0]
existing_items = [x for x in all_items if x['status_code'] > 0]

# Sync: Explorer -> Queue
if st.session_state['sync_selection']:
    sync_target = st.session_state['sync_selection']
    matching = [x for x in all_items if x['path'] == sync_target or sync_target.startswith(x['path'])]
    if matching:
        st.session_state['current_selection_data'] = matching[0]
        st.toast(f"Matched: {matching[0]['name']}")
        st.session_state['sync_selection'] = None

with col1:
    tab_new, tab_exist = st.tabs(["🆕 Untidy Queue", "📚 Imported"])
    with tab_new:
        if not new_items:
            st.info("Queue Empty.")
        else:
            df_new = pd.DataFrame(new_items)
            sel_new = st.dataframe(
                df_new[['name']],
                column_config={"name": st.column_config.TextColumn("Book Name")},
                use_container_width=True, hide_index=True, height=600,
                on_select="rerun", selection_mode="single-row", key="grid_new"
            )
            if sel_new.selection.rows:
                st.session_state['current_selection_data'] = new_items[sel_new.selection.rows[0]]

    with tab_exist:
        if not existing_items:
            st.info("Empty.")
        else:
            df_exist = pd.DataFrame(existing_items)
            sel_exist = st.dataframe(
                df_exist[['State', 'name']],
                column_config={"State": st.column_config.TextColumn("St", width="small")},
                use_container_width=True, hide_index=True, height=600,
                on_select="rerun", selection_mode="single-row", key="grid_exist"
            )
            if sel_exist.selection.rows:
                st.session_state['current_selection_data'] = existing_items[sel_exist.selection.rows[0]]

# Sync: Queue -> Explorer
selected_item = st.session_state.get('current_selection_data')
if selected_item:
    if selected_item['path'] != st.session_state.get('last_jumped_path'):
        st.session_state['exp_path'] = os.path.dirname(selected_item['path']) if os.path.isfile(selected_item['path']) else selected_item['path']
        st.session_state['last_jumped_path'] = selected_item['path']
        st.rerun()

# ==================== COLUMN 2: EDITOR ====================
with col2:
    if selected_item:
        st.subheader("✏️ Editor")
        st.caption(f"Path: `{selected_item['path']}`")
        if st.button("❌ Close Editor"):
            st.session_state['current_selection_data'] = None
            st.rerun()
            
        c_src, c_bar, c_btn = st.columns([1, 2, 1])
        with c_src: provider = st.selectbox("Source", ["Apple Books", "Google Books"], key='search_provider', label_visibility="collapsed")
        with c_bar: 
            clean_q = clean_search_query(selected_item['name'])
            q = st.text_input("Search", value=clean_q, label_visibility="collapsed")
        with c_btn: do_search = st.button("Search")

        def update_form_state():
            if 'result_selector' in st.session_state and 'search_results' in st.session_state:
                opts = {f"{b.get('authors')} - {b.get('title')}": b for b in st.session_state['search_results']}
                sel_key = st.session_state['result_selector']
                if sel_key in opts:
                    data = opts[sel_key]
                    st.session_state['form_auth'] = data.get('authors', '')
                    st.session_state['form_title'] = data.get('title', '')
                    st.session_state['form_narr'] = data.get('narrators', '')
                    st.session_state['form_series'] = data.get('series', '')
                    st.session_state['form_part'] = data.get('part', '')
                    rd = data.get('releaseDate')
                    st.session_state['form_year'] = rd[:4] if rd else ''
                    st.session_state['form_desc'] = data.get('summary', '')
                    st.session_state['form_img'] = data.get('image', '')

        if do_search:
            with st.spinner(f"Searching {provider}..."):
                res = fetch_metadata(q, provider)
                if res:
                    st.session_state['search_results'] = res
                    first = f"{res[0].get('authors')} - {res[0].get('title')}"
                    st.session_state['result_selector'] = first
                    update_form_state()
                else: st.warning(f"No matches.")

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
        st.info("👈 Select a book to edit.")

# ==================== COLUMN 3: EXPLORER & BUILDER ====================
with col3:
    st.subheader("📂 Explorer & Builder")
    curr_path = st.session_state['exp_path']
    
    # Breadcrumb / Up
    col_u, col_p = st.columns([0.2, 0.8])
    with col_u:
        if st.button("⬆️"):
            st.session_state['exp_path'] = os.path.dirname(curr_path)
            st.rerun()
    with col_p:
        st.caption(f".../{os.path.basename(curr_path)}/")

    try:
        items = sorted(os.listdir(curr_path))
        file_list = []
        for i in items:
            full = os.path.join(curr_path, i)
            is_dir = os.path.isdir(full)
            if is_dir or i.lower().endswith(('.mp3','.m4b','.m4a','.flac')):
                file_list.append({"icon": "📁" if is_dir else "🎵", "name": i, "path": full, "type": "dir" if is_dir else "file"})
        
        if file_list:
            df_files = pd.DataFrame(file_list)
            
            # --- MULTI SELECT TABLE ---
            sel_files = st.dataframe(
                df_files[['icon', 'name']],
                column_config={"icon": st.column_config.TextColumn("", width="small")},
                hide_index=True, use_container_width=True, height=450,
                on_select="rerun", selection_mode="multi-row"
            )
            
            # Handle Selections
            selected_rows = sel_files.selection.rows
            
            if selected_rows:
                # Get the actual items
                selection_data = [file_list[i] for i in selected_rows]
                st.markdown("---")
                st.caption(f"Selected: {len(selection_data)} items")
                
                # Logic 1: Navigation (Single Folder)
                if len(selection_data) == 1 and selection_data[0]['type'] == 'dir':
                    if st.button(f"📂 Open '{selection_data[0]['name']}'"):
                        st.session_state['exp_path'] = selection_data[0]['path']
                        # Trigger sync to Queue if it matches a book
                        st.session_state['sync_selection'] = selection_data[0]['path']
                        st.rerun()
                        
                # Logic 2: Manual Bundle
                with st.form("manual_bundle"):
                    new_name = st.text_input("New Book Title", value=selection_data[0]['name'])
                    if st.form_submit_button("✨ Bundle as Book"):
                        # Collect all files (recurse if folder selected)
                        final_paths = []
                        for item in selection_data:
                            if item['type'] == 'dir':
                                for root, _, fs in os.walk(item['path']):
                                    for f in fs:
                                        if f.lower().endswith(('.mp3','.m4b','.m4a','.flac')):
                                            final_paths.append(os.path.join(root, f))
                            else:
                                final_paths.append(item['path'])
                        
                        if final_paths:
                            entry = {
                                "id": f"MANUAL|{time.time()}", "path": curr_path,
                                "name": new_name, "clean": sanitize_for_matching(new_name),
                                "file_list": final_paths, "is_group": True, "is_manual": True
                            }
                            st.session_state['manual_books'].insert(0, entry)
                            st.success("Added to Queue!")
                            time.sleep(0.5)
                            st.rerun()
                        else:
                            st.error("No audio files found in selection.")
            
    except Exception as e: st.error(f"Error: {e}")
