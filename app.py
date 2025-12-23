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
import difflib

# --- Configuration ---
DOWNLOAD_DIR = "/downloads"
LIBRARY_DIR = "/audiobooks"
DATA_DIR = "/app/data"
HISTORY_FILE = os.path.join(DATA_DIR, "processed_log.json")
CACHE_FILE = os.path.join(DATA_DIR, "library_map_cache.json")

# API ENDPOINTS
AUDNEXUS_API = "https://api.audnex.us/books"
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
if 'search_provider' not in st.session_state: st.session_state['search_provider'] = "Audible" # Default back to Audible
if 'last_jumped_path' not in st.session_state: st.session_state['last_jumped_path'] = None
if 'manual_books' not in st.session_state: st.session_state['manual_books'] = []
if 'last_synced_book_id' not in st.session_state: st.session_state['last_synced_book_id'] = None

# Grid Keys (Used to force-deselect other grids)
if 'grid_key_auto' not in st.session_state: st.session_state['grid_key_auto'] = 0
if 'grid_key_manual' not in st.session_state: st.session_state['grid_key_manual'] = 0
if 'grid_key_match' not in st.session_state: st.session_state['grid_key_match'] = 0
if 'grid_key_done' not in st.session_state: st.session_state['grid_key_done'] = 0

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
    text = re.sub(r'[^a-z0-9]', '', text) 
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
            
            is_root = os.path.abspath(root) == os.path.abspath(DOWNLOAD_DIR)

            # 1. Collection/Root Logic
            if "collection" in folder_name.lower() or is_root:
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

@st.cache_data(show_spinner=False)
def calculate_matches(all_candidates, library_items, history):
    final_list = []
    for data in all_candidates:
        unique_id = data['id']
        path_str = data['path']
        clean_dl = data['clean']
        status = 0 
        match_path = None
        
        if unique_id in history or path_str in history: status = 3
        elif library_items:
            for lib_item in library_items:
                clean_lib = lib_item.get('clean')
                if not clean_lib: continue
                if len(clean_lib) > 2 and (clean_lib in clean_dl or clean_dl in clean_lib):
                    match_path = lib_item['path']
                    status = 1
                    break
        
        status_icon = "⚪"
        if status == 3: status_icon = "✅"
        elif status == 1: status_icon = "⚠️"
        
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

def get_candidates_with_status():
    raw_candidates = scan_downloads_snapshot()
    manual_items = st.session_state.get('manual_books', [])
    
    history = load_json(HISTORY_FILE, [])
    cached_lib = load_json(CACHE_FILE, [])
    if not cached_lib: cached_lib = scan_library_now()
    
    # Process lists
    auto_processed = calculate_matches(raw_candidates, cached_lib, history)
    manual_processed = calculate_matches(manual_items, cached_lib, history)
    
    return auto_processed, manual_processed

# --- SEARCH ---
def extract_details_smart(title, desc, subtitle=""):
    narrator, series, part = "", "", ""
    full_text = f"{title} {subtitle} {desc}"
    
    narr_pat = r"(?:narrated|read)\s+by\s+([A-Za-z\s\.]+?)(?:[\.,\n\(-]|$)"
    match_narr = re.search(narr_pat, full_text, re.IGNORECASE)
    if match_narr: narrator = match_narr.group(1).strip()

    if ":" in title:
        split_title = title.split(":")
        if len(split_title[0]) > 3: 
            series = split_title[0].strip()
            part_pat = r"(?:Book|Vol|Part)\s*(\d+)"
            match_part = re.search(part_pat, full_text, re.IGNORECASE)
            if match_part: part = match_part.group(1)

    series_pat_b = r"\(([^)]+?)(?:,|#|;)\s*(?:Book|Vol|Part)?\s*(\d+)\)"
    match_series_b = re.search(series_pat_b, title, re.IGNORECASE)
    if match_series_b:
        series = match_series_b.group(1).strip()
        part = match_series_b.group(2).strip()
    return narrator, series, part

def fetch_audnexus_twostep(query):
    """
    Two-Step Search:
    1. Search Audible.com website to find the ASIN (e.g. B0xxxx)
    2. Use ASIN to query Audnexus API
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    
    # Step 1: Scrape ASIN
    asin = None
    try:
        search_url = "https://www.audible.com/search"
        r_search = requests.get(search_url, params={'keywords': query}, headers=headers, timeout=8)
        
        # Regex to find the first ASIN in the search results
        # Looks for: data-asin="B0xxxxxxxx"
        match = re.search(r'data-asin="(B0[A-Z0-9]{8})"', r_search.text)
        if match:
            asin = match.group(1)
    except:
        pass # Fail silently to step 2

    # Step 2: Fetch Metadata
    if asin:
        try:
            api_url = f"{AUDNEXUS_API}/{asin}"
            r_api = requests.get(api_url, headers=headers, timeout=8)
            if r_api.status_code == 200:
                b = r_api.json()
                # Return list (standard format)
                return [{
                    "title": b.get('title'),
                    "authors": ", ".join(b.get('authors', [])),
                    "narrators": ", ".join(b.get('narrators', [])),
                    "series": b.get('seriesPrimary', ''),
                    "part": b.get('seriesPrimarySequence', ''),
                    "summary": b.get('summary', ''),
                    "image": b.get('image', ''),
                    "releaseDate": b.get('releaseDate', ''),
                    "source": "Audible"
                }]
        except: pass
    
    return []

def fetch_itunes(query):
    try:
        r = requests.get(ITUNES_API, params={'term': query, 'media': 'audiobook', 'limit': 10}, timeout=5)
        if r.status_code == 200:
            results = []
            for item in r.json().get('results', []):
                img = item.get('artworkUrl100', '').replace('100x100', '600x600')
                raw_title = item.get('collectionName', '')
                raw_desc = item.get('description', '')
                s_narr, s_ser, s_part = extract_details_smart(raw_title, raw_desc)
                results.append({
                    "title": raw_title, "authors": item.get('artistName'),
                    "narrators": s_narr, "series": s_ser, "part": s_part, 
                    "summary": raw_desc, "image": img, "releaseDate": item.get('releaseDate', '')
                })
            return results
    except: pass
    return []

def fetch_google(query):
    try:
        r = requests.get(GOOGLE_BOOKS_API, params={"q": query, "maxResults": 10, "langRestrict": "en"}, timeout=5)
        if r.status_code == 200:
            results = []
            for item in r.json().get('items', []):
                info = item.get('volumeInfo', {})
                img = info.get('imageLinks', {}).get('thumbnail', '').replace('http:', 'https:')
                auths = info.get('authors', [])
                s_narr, s_ser, s_part = extract_details_smart(info.get('title', ''), info.get('description', ''), info.get('subtitle', ''))
                results.append({
                    "title": info.get('title'), "authors": auths[0] if auths else "",
                    "narrators": s_narr, "series": s_ser, "part": s_part, 
                    "summary": info.get('description', ''), "image": img, "releaseDate": info.get('publishedDate', '')
                })
            return results
    except: pass
    return []

def fetch_metadata_router(query, provider):
    if provider == "Audible": return fetch_audnexus_twostep(query)
    elif provider == "Apple Books": return fetch_itunes(query)
    return fetch_google(query)

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

def process_selection(source_data, author, title, series, series_part, desc, cover_url, narrator, publish_year, target_override=None):
    files_to_process = source_data['file_list']
    
    if target_override:
        dest_base = target_override
        try:
            for filename in os.listdir(dest_base):
                file_path = os.path.join(dest_base, filename)
                if os.path.isfile(file_path) or os.path.islink(file_path): os.unlink(file_path)
                elif os.path.isdir(file_path): shutil.rmtree(file_path)
        except: pass
    else:
        clean_author = sanitize_filename(author)
        clean_title = sanitize_filename(title)
        clean_series = sanitize_filename(series)
        dest_base = os.path.join(LIBRARY_DIR, clean_author, clean_series, clean_title) if clean_series else os.path.join(LIBRARY_DIR, clean_author, clean_title)
        os.makedirs(dest_base, exist_ok=True)

    files_to_process.sort()
    total = len(files_to_process)
    pad = max(2, len(str(total)))
    
    bar = st.progress(0)
    for i, src in enumerate(files_to_process):
        ext = os.path.splitext(src)[1]
        name = f"{str(i+1).zfill(pad)} - {sanitize_filename(title)}{ext}"
        dst = os.path.join(dest_base, name)
        shutil.copy2(src, dst)
        tag_file(dst, author, title, series, desc, cover_url, publish_year, i+1, total)
        bar.progress((i+1)/total)

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
    if source_data['unique_id'] not in hist: hist.append(source_data['unique_id'])
    if source_data['path'] not in hist: hist.append(source_data['path'])
    save_json(HISTORY_FILE, hist)
    
    if source_data.get('is_manual'):
        st.session_state['manual_books'] = [b for b in st.session_state['manual_books'] if b['id'] != source_data['unique_id']]

    st.success(f"✅ Success: {title}")
    st.cache_data.clear()
    st.session_state['current_selection_data'] = None
    time.sleep(1)
    st.rerun()

# --- MAIN LAYOUT ---
col1, col2, col3 = st.columns([1.5, 2, 1.5])

# Fetch Data
auto_processed, manual_processed = get_candidates_with_status()

# Auto Lists
new_items = [x for x in auto_processed if x['status_code'] == 0]
match_items = [x for x in auto_processed if x['status_code'] == 1]
existing_items = [x for x in auto_processed if x['status_code'] == 3]

# Manual List
built_items = [x for x in manual_processed]

# Sync: Explorer -> Queue
if st.session_state['sync_selection']:
    sync_target = st.session_state['sync_selection']
    all_known = auto_processed + manual_processed
    matching = [x for x in all_known if x['path'] == sync_target or sync_target.startswith(x['path'])]
    if matching:
        st.session_state['current_selection_data'] = matching[0]
        # Auto-switch to correct grid
        if matching[0] in manual_processed:
            st.session_state['grid_key_auto'] += 1
            st.session_state['grid_key_match'] += 1
            st.session_state['grid_key_done'] += 1
        else:
            st.session_state['grid_key_manual'] += 1
            st.session_state['grid_key_match'] += 1
            st.session_state['grid_key_done'] += 1
            
        st.toast(f"Matched: {matching[0]['name']}")
        st.session_state['sync_selection'] = None

# --- COL 1: QUEUE ---
with col1:
    tab_new, tab_built, tab_match, tab_exist = st.tabs(["🆕 Untidy", "🛠️ Built", "⚠️ Match", "📚 Done"])
    
    # Auto-Deselect Helper: Forces re-render of other grids when one is clicked
    def on_grid_select(grid_name):
        if grid_name == 'untidy': 
            st.session_state['grid_key_manual'] += 1
            st.session_state['grid_key_match'] += 1
            st.session_state['grid_key_done'] += 1
        elif grid_name == 'built':
            st.session_state['grid_key_auto'] += 1
            st.session_state['grid_key_match'] += 1
            st.session_state['grid_key_done'] += 1
        elif grid_name == 'match':
            st.session_state['grid_key_auto'] += 1
            st.session_state['grid_key_manual'] += 1
            st.session_state['grid_key_done'] += 1
        elif grid_name == 'done':
            st.session_state['grid_key_auto'] += 1
            st.session_state['grid_key_manual'] += 1
            st.session_state['grid_key_match'] += 1

    with tab_new:
        if not new_items: st.info("Empty")
        else:
            df = pd.DataFrame(new_items)
            sel = st.dataframe(
                df[['name']], column_config={"name": "Folder Name"},
                use_container_width=True, hide_index=True, height=600,
                on_select=lambda: on_grid_select('untidy'), selection_mode="single-row",
                key=f"grid_auto_{st.session_state['grid_key_auto']}"
            )
            if sel.selection.rows: st.session_state['current_selection_data'] = new_items[sel.selection.rows[0]]

    with tab_built:
        if not built_items: st.info("Use Explorer to bundle.")
        else:
            df = pd.DataFrame(built_items)
            sel = st.dataframe(
                df[['name']], column_config={"name": "Bundle Name"},
                use_container_width=True, hide_index=True, height=600,
                on_select=lambda: on_grid_select('built'), selection_mode="single-row",
                key=f"grid_manual_{st.session_state['grid_key_manual']}"
            )
            if sel.selection.rows: st.session_state['current_selection_data'] = built_items[sel.selection.rows[0]]

    with tab_match:
        if not match_items: st.info("No duplicates.")
        else:
            df = pd.DataFrame(match_items)
            sel = st.dataframe(
                df[['name']], column_config={"name": "Matches"},
                use_container_width=True, hide_index=True, height=600,
                on_select=lambda: on_grid_select('match'), selection_mode="single-row",
                key=f"grid_match_{st.session_state['grid_key_match']}"
            )
            if sel.selection.rows: st.session_state['current_selection_data'] = match_items[sel.selection.rows[0]]

    with tab_exist:
        if not existing_items: st.info("Empty.")
        else:
            df = pd.DataFrame(existing_items)
            sel = st.dataframe(
                df[['name']], column_config={"name": "History"},
                use_container_width=True, hide_index=True, height=600,
                on_select=lambda: on_grid_select('done'), selection_mode="single-row",
                key=f"grid_done_{st.session_state['grid_key_done']}"
            )
            if sel.selection.rows: st.session_state['current_selection_data'] = existing_items[sel.selection.rows[0]]

# Sync: Queue -> Explorer
selected_item = st.session_state.get('current_selection_data')
if selected_item:
    target_path = selected_item['path']
    if os.path.isfile(target_path): target_path = os.path.dirname(target_path)
    if st.session_state['last_synced_book_id'] != selected_item['unique_id']:
        st.session_state['exp_path'] = target_path
        st.session_state['last_synced_book_id'] = selected_item['unique_id']
        st.rerun()

# --- COL 2: EDITOR ---
with col2:
    if selected_item:
        st.subheader("✏️ Editor")
        
        if selected_item['status_code'] == 1:
            st.warning("⚠️ Match Found in Library")
            st.code(f"Target: {os.path.basename(selected_item['match_path'])}", language="text")
            target_override = selected_item['match_path']
        else:
            st.caption(f"Path: `{os.path.basename(selected_item['path'])}`")
            target_override = None

        if st.button("❌ Close"):
            st.session_state['current_selection_data'] = None
            st.rerun()
            
        c_src, c_bar, c_btn = st.columns([1, 2, 1])
        with c_src: provider = st.selectbox("Source", ["Audible", "Apple Books", "Google Books"], key='search_provider', label_visibility="collapsed")
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
                res = fetch_metadata_router(q, provider)
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
            
            lbl = "Import"
            if target_override: lbl = "Merge & Fix"
            
            if st.form_submit_button(lbl, type="primary"):
                if auth and titl:
                    process_selection(selected_item, auth, titl, seri, part, desc, img, narr, year, target_override)
                else: st.error("Author/Title Required")
    else:
        st.info("👈 Select a book.")

# --- COL 3: EXPLORER ---
with col3:
    st.subheader("📂 Explorer")
    curr_path = st.session_state['exp_path']
    col_u, col_p = st.columns([0.2, 0.8])
    with col_u:
        if st.button("⬆️"):
            st.session_state['exp_path'] = os.path.dirname(curr_path)
            st.rerun()
    with col_p: st.caption(f".../{os.path.basename(curr_path)}/")

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
            sel_files = st.dataframe(
                df_files[['icon', 'name']],
                column_config={"icon": st.column_config.TextColumn("", width="small")},
                hide_index=True, use_container_width=True, height=450,
                on_select="rerun", selection_mode="multi-row"
            )
            
            selected_rows = sel_files.selection.rows
            
            if selected_rows:
                selection_data = [file_list[i] for i in selected_rows]
                st.markdown("---")
                if len(selection_data) == 1 and selection_data[0]['type'] == 'dir':
                    if st.button(f"📂 Open '{selection_data[0]['name']}'"):
                        st.session_state['exp_path'] = selection_data[0]['path']
                        st.session_state['sync_selection'] = selection_data[0]['path']
                        st.rerun()
                with st.form("manual_bundle"):
                    new_name = st.text_input("New Book Title", value=selection_data[0]['name'])
                    if st.form_submit_button("✨ Bundle as Book"):
                        final_paths = []
                        for item in selection_data:
                            if item['type'] == 'dir':
                                for root, _, fs in os.walk(item['path']):
                                    for f in fs:
                                        if f.lower().endswith(('.mp3','.m4b','.m4a','.flac')):
                                            final_paths.append(os.path.join(root, f))
                            else: final_paths.append(item['path'])
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
                        else: st.error("No audio files.")
    except Exception as e: st.error(f"Error: {e}")
