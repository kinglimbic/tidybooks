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
import difflib # For string similarity

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
if 'exp_root' not in st.session_state: st.session_state['exp_root'] = DOWNLOAD_DIR
if 'sync_selection' not in st.session_state: st.session_state['sync_selection'] = None
if 'current_selection_data' not in st.session_state: st.session_state['current_selection_data'] = None
if 'search_provider' not in st.session_state: st.session_state['search_provider'] = "Audible"
if 'last_jumped_path' not in st.session_state: st.session_state['last_jumped_path'] = None

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

def get_file_stem(filename):
    """
    Reduces a filename to its 'Core Identity' to group multi-part files.
    Ex: "Harry Potter 1 - Part 01.mp3" -> "harrypotter1"
    """
    name = os.path.splitext(filename)[0].lower()
    # Remove common separators
    name = re.sub(r'[_\-\.]', ' ', name)
    # Remove common 'part' indicators
    name = re.sub(r'\b(part|pt|cd|disc|disk|track|chapter)\s*\d+\b', '', name)
    # Remove standalone numbers at the end (often track numbers)
    name = re.sub(r'\s+\d+$', '', name)
    # Remove brackets
    name = re.sub(r'[\(\[].*?[\)\]]', '', name)
    return re.sub(r'\s+', ' ', name).strip()

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

# --- SMART GROUPING SCANNER ---
@st.cache_data(ttl=600, show_spinner="Scanning downloads...")
def scan_downloads_snapshot():
    if not os.path.exists(DOWNLOAD_DIR): return []
    
    candidates = []
    seen_ids = set() # To prevent duplicates

    for root, dirs, files in os.walk(DOWNLOAD_DIR):
        audio_files = [f for f in files if f.lower().endswith(('.mp3', '.m4b', '.m4a', '.flac'))]
        
        if audio_files:
            folder_name = os.path.basename(root)
            if is_junk_folder(folder_name): continue
            
            # Logic: Group files by similarity
            groups = {}
            
            for f in audio_files:
                # Get the "Stem" (Core Name)
                stem = get_file_stem(f)
                if not stem: stem = "unknown" # Fallback
                
                if stem not in groups: groups[stem] = []
                groups[stem].append(f)
            
            # Create a candidate entry for EACH group found in the folder
            for stem, file_list in groups.items():
                # We use a unique ID based on path + stem to track history
                unique_id = f"{root}|{stem}"
                
                # Determine display name
                # If the folder only has one group, use the Folder Name (cleaner)
                # If the folder has multiple groups (Mixed Collection), use the Stem
                display_name = folder_name if len(groups) == 1 else stem.title()
                
                # If we are splitting a folder, we must pass the specific file list
                full_paths = [os.path.join(root, f) for f in file_list]
                
                candidates.append({
                    "id": unique_id,
                    "path": root, # Base path is still the folder
                    "name": display_name,
                    "clean": sanitize_for_matching(display_name),
                    "file_list": full_paths, # Only operate on THESE files
                    "is_group": len(groups) > 1 # Flag to tell UI this is a split
                })

    return candidates

def get_candidates_with_status():
    raw_candidates = scan_downloads_snapshot()
    history = load_json(HISTORY_FILE, [])
    cached_lib = load_json(CACHE_FILE, None)
    library_items = cached_lib if (cached_lib and isinstance(cached_lib, list)) else []
    
    final_list = []
    for data in raw_candidates:
        unique_id = data['id']
        clean_dl = data['clean']
        
        status = 0 
        match_path = None
        
        # Check History using Unique ID (so we don't re-import the same split group)
        if unique_id in history:
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
        
        status_icon = "⚪"
        if status == 3: status_icon = "✅"
        elif status == 2: status_icon = "✅"
        elif status == 1: status_icon = "🟡"
        
        final_list.append({
            "path": data['path'], # Used for Explorer jump
            "unique_id": unique_id,
            "name": data['name'],
            "status_code": status,
            "State": status_icon, 
            "match_path": match_path,
            "file_list": data['file_list']
        })
    return final_list

# --- SEARCH ---
def fetch_audnexus(query):
    try:
        r = requests.get(AUDNEXUS_API, params={'q': query}, timeout=10)
        r.raise_for_status()
        data = r.json()
        results = []
        for b in data:
            results.append({
                "title": b.get('title'),
                "authors": ", ".join(b.get('authors', [])),
                "narrators": ", ".join(b.get('narrators', [])),
                "seriesPrimary": b.get('seriesPrimary', ''),
                "seriesPrimarySequence": b.get('seriesPrimarySequence', ''),
                "summary": b.get('summary', ''),
                "image": b.get('image', ''),
                "releaseDate": b.get('releaseDate', ''),
                "source": "Audible"
            })
        return results
    except Exception as e:
        st.error(f"Audible Error: {e}")
        return []

def fetch_itunes(query):
    try:
        r = requests.get(ITUNES_API, params={'term': query, 'media': 'audiobook', 'limit': 10}, timeout=10)
        r.raise_for_status()
        data = r.json()
        results = []
        for item in data.get('results', []):
            img = item.get('artworkUrl100', '').replace('100x100', '600x600')
            results.append({
                "title": item.get('collectionName'),
                "authors": item.get('artistName'),
                "narrators": "",
                "seriesPrimary": "",
                "seriesPrimarySequence": "",
                "summary": item.get('description', ''),
                "image": img,
                "releaseDate": item.get('releaseDate', ''),
                "source": "Apple"
            })
        return results
    except Exception as e:
        st.error(f"Apple Error: {e}")
        return []

def fetch_google(query):
    try:
        r = requests.get(GOOGLE_BOOKS_API, params={"q": query, "maxResults": 10, "langRestrict": "en"}, timeout=10)
        r.raise_for_status()
        data = r.json()
        results = []
        for item in data.get('items', []):
            info = item.get('volumeInfo', {})
            img = info.get('imageLinks', {}).get('thumbnail', '').replace('http:', 'https:')
            auth_list = info.get('authors', [])
            results.append({
                "title": info.get('title', ''),
                "authors": auth_list[0] if auth_list else "",
                "narrators": ", ".join(auth_list[1:]) if len(auth_list) > 1 else "",
                "seriesPrimary": "",
                "seriesPrimarySequence": "",
                "summary": info.get('description', ''),
                "image": img,
                "releaseDate": info.get('publishedDate', ''),
                "source": "Google"
            })
        return results
    except Exception as e:
        st.error(f"Google Error: {e}")
        return []

def fetch_metadata_router(query, provider):
    if provider == "Audible": return fetch_audnexus(query)
    elif provider == "Apple Books": return fetch_itunes(query)
    else: return fetch_google(query)

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
    # We use the specific list of files determined by the grouper
    files_to_process = source_data['file_list']
    
    # If fixing an existing messy import, we might move the whole folder
    # But for split groups, we are essentially "extracting" them
    if source_data['status_code'] == 1 and source_data['match_path']:
        mode = "FIX"
        # For fix, we assume we are moving the files found in the match path
        # But honestly, for split groups, FIX logic is complex. 
        # Safest is to treat split groups as COPY/MOVE individual files.
        pass 

    clean_author = sanitize_filename(author, True)
    clean_title = sanitize_filename(title, True)
    clean_series = sanitize_filename(series, False)
    
    dest_base = os.path.join(LIBRARY_DIR, clean_author, clean_series, clean_title) if clean_series else os.path.join(LIBRARY_DIR, clean_author, clean_title)
    os.makedirs(dest_base, exist_ok=True)

    files_to_process.sort()
    total = len(files_to_process)
    pad = max(2, len(str(total)))
    
    bar = st.progress(0)
    for i, src in enumerate(files_to_process):
        ext = os.path.splitext(src)[1]
        name = f"{str(i+1).zfill(pad)} - {clean_title}{ext}" if total > 1 else f"{clean_title}{ext}"
        dst = os.path.join(dest_base, name)
        
        # Always COPY first to be safe with split collections, unless we implement specific move logic
        shutil.copy2(src, dst)
        tag_file(dst, author, title, series, desc, cover_url, publish_year, i+1, total)
        bar.progress((i+1)/total)

    # Note: We do NOT delete source folders automatically for split groups 
    # because other books might still be in there.

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
    # Save the UNIQUE ID (Folder|Stem) so we don't import this specific book group again
    if source_data['unique_id'] not in hist:
        hist.append(source_data['unique_id'])
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
            
    if selected_root_label == "Audiobooks" and current_path != LIBRARY_DIR:
        st.markdown("#### 🛠️ Manual Actions")
        if st.button("✅ Force Mark as 'Imported'"):
            folder_name = os.path.basename(current_path)
            meta_path = os.path.join(current_path, "metadata.json")
            minimal_meta = {
                "title": folder_name,
                "authors": ["Manual Import"],
                "description": "Manually marked as imported."
            }
            with open(meta_path, 'w') as f: json.dump(minimal_meta, f, indent=4)
            st.success(f"Marked '{folder_name}' as imported!")
            scan_library_now()
            st.cache_data.clear()
            time.sleep(1)
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

all_items = get_candidates_with_status()
new_items = [x for x in all_items if x['status_code'] == 0]
existing_items = [x for x in all_items if x['status_code'] > 0]

if st.session_state['sync_selection']:
    sync_target = st.session_state['sync_selection']
    matching = [x for x in all_items if x['path'] == sync_target or sync_target.startswith(x['path'])]
    if matching:
        st.session_state['current_selection_data'] = matching[0]
        st.toast(f"Jumped to: {matching[0]['name']}")
        st.session_state['sync_selection'] = None

with col1:
    tab_new, tab_exist = st.tabs(["🆕 Untidy Queue", "📚 Already Imported"])
    
    with tab_new:
        if not new_items:
            st.info("Nothing new to import!")
        else:
            df_new = pd.DataFrame(new_items)
            sel_new = st.dataframe(
                df_new[['name']],
                column_config={"name": st.column_config.TextColumn("Book Name")},
                use_container_width=True,
                hide_index=True,
                height=500,
                on_select="rerun",
                selection_mode="single-row",
                key="grid_new"
            )
            if sel_new.selection.rows:
                st.session_state['current_selection_data'] = new_items[sel_new.selection.rows[0]]

    with tab_exist:
        if not existing_items:
            st.info("No imported items found yet.")
        else:
            df_exist = pd.DataFrame(existing_items)
            sel_exist = st.dataframe(
                df_exist[['State', 'name']],
                column_config={"State": st.column_config.TextColumn("State", width="small")},
                use_container_width=True,
                hide_index=True,
                height=500,
                on_select="rerun",
                selection_mode="single-row",
                key="grid_exist"
            )
            if sel_exist.selection.rows:
                st.session_state['current_selection_data'] = existing_items[sel_exist.selection.rows[0]]

# Sync 2: List -> Explorer
selected_item = st.session_state.get('current_selection_data')
if selected_item:
    if selected_item['path'] != st.session_state.get('last_jumped_path'):
        st.session_state['exp_path'] = os.path.dirname(selected_item['path']) if os.path.isfile(selected_item['path']) else selected_item['path']
        st.session_state['last_jumped_path'] = selected_item['path']
        if selected_item['path'].startswith(LIBRARY_DIR):
            st.session_state['exp_root'] = LIBRARY_DIR
        else:
            st.session_state['exp_root'] = DOWNLOAD_DIR
        st.rerun()

with col2:
    if selected_item:
        st.subheader("✏️ Editor")
        st.caption(f"Path: `{selected_item['path']}`")
        if len(selected_item['file_list']) > 1:
            st.caption(f"📚 Group contains {len(selected_item['file_list'])} files")
        
        if st.button("❌ Close Selection"):
            st.session_state['current_selection_data'] = None
            st.rerun()
            
        c_src, c_bar, c_btn = st.columns([1, 2, 1])
        with c_src:
            provider = st.selectbox("Source", ["Audible", "Apple Books", "Google Books"], key='search_provider')
        with c_bar:
            clean_q = clean_search_query(selected_item['name'])
            q = st.text_input("Search", value=clean_q, label_visibility="collapsed")
        with c_btn:
            do_search = st.button("Search")

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

        if do_search:
            with st.spinner(f"Searching {provider}..."):
                res = fetch_metadata_router(q, provider)
                if res:
                    st.session_state['search_results'] = res
                    first = f"{res[0].get('authors')} - {res[0].get('title')}"
                    st.session_state['result_selector'] = first
                    update_form_state()
                else: st.warning(f"No matches on {provider}.")

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
