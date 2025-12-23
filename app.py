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
if 'exp_path' not in st.session_state: st.session_state['exp_path'] = DOWNLOAD_DIR
if 'search_provider' not in st.session_state: st.session_state['search_provider'] = "Apple Books"
if 'last_processed' not in st.session_state: st.session_state['last_processed'] = None

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
def sanitize_filename(name):
    if not name: return "Unknown"
    clean = name.replace("/", "-").replace("\\", "-")
    return re.sub(r'[<>:"|?*]', '', clean).strip()

def clean_search_query(text):
    text = re.sub(r'[\(\[\{].*?[\)\]\}]', '', text)
    text = re.sub(r'\b(mp3|m4b|128k|64k|192k|aac)\b', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\b(cd|disc|part|vol|v)\s*\d+\b', '', text, flags=re.IGNORECASE)
    text = text.replace('.', ' ').replace('_', ' ').replace('-', ' ')
    return re.sub(r'\s+', ' ', text).strip()

def natural_keys(text):
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', text)]

# --- Caching & Scanning ---
def scan_library_now():
    """ Updates the list of what is already in the library """
    library_items = []
    for root, dirs, files in os.walk(LIBRARY_DIR):
        has_audio = any(f.lower().endswith(('.mp3', '.m4b', '.m4a', '.flac')) for f in files)
        if has_audio:
            library_items.append({"path": root, "name": os.path.basename(root)})
    save_json(CACHE_FILE, library_items)
    return library_items

@st.cache_data(ttl=600, show_spinner="Scanning downloads...")
def get_auto_queue():
    """ Standard Scanner: 1 Folder = 1 Book """
    if not os.path.exists(DOWNLOAD_DIR): return []
    candidates = []
    
    for root, dirs, files in os.walk(DOWNLOAD_DIR):
        audio_files = [f for f in files if f.lower().endswith(('.mp3', '.m4b', '.m4a', '.flac'))]
        if audio_files:
            # Skip if junk folder
            if any(x in root.lower() for x in ['sample', 'artwork', '.zab']): continue
            
            # Skip if parent folder has audio subfolders (don't double count)
            has_subfolders = any(not d.startswith('.') for d in dirs)
            if has_subfolders: continue

            # Standard: Folder Name is Book Name
            folder_name = os.path.basename(root)
            full_paths = [os.path.join(root, f) for f in audio_files]
            
            candidates.append({
                "type": "auto",
                "name": folder_name,
                "path": root,
                "file_list": full_paths,
                "count": len(full_paths)
            })
    
    # Sort Natural
    return sorted(candidates, key=lambda x: natural_keys(x['name']))

# --- APIs ---
def fetch_metadata(query, provider):
    results = []
    try:
        if provider == "Apple Books":
            r = requests.get(ITUNES_API, params={'term': query, 'media': 'audiobook', 'limit': 10}, timeout=5)
            if r.status_code == 200:
                for item in r.json().get('results', []):
                    img = item.get('artworkUrl100', '').replace('100x100', '600x600')
                    results.append({
                        "title": item.get('collectionName'),
                        "authors": item.get('artistName'),
                        "narrators": "",
                        "series": "", "part": "",
                        "year": item.get('releaseDate', '')[:4],
                        "desc": item.get('description', ''),
                        "img": img
                    })
        else: # Google
            r = requests.get(GOOGLE_BOOKS_API, params={"q": query, "maxResults": 10, "langRestrict": "en"}, timeout=5)
            if r.status_code == 200:
                for item in r.json().get('items', []):
                    info = item.get('volumeInfo', {})
                    img = info.get('imageLinks', {}).get('thumbnail', '').replace('http:', 'https:')
                    auths = info.get('authors', [])
                    results.append({
                        "title": info.get('title'),
                        "authors": auths[0] if auths else "",
                        "narrators": ", ".join(auths[1:]) if len(auths)>1 else "",
                        "series": "", "part": "",
                        "year": info.get('publishedDate', '')[:4],
                        "desc": info.get('description', ''),
                        "img": img
                    })
    except: pass
    return results

# --- Processing Engine ---
def process_book(file_list, meta):
    """ Moves files, Tags them, Creates Metadata.json """
    # 1. Prepare Destination
    clean_auth = sanitize_filename(meta['authors'])
    clean_title = sanitize_filename(meta['title'])
    clean_series = sanitize_filename(meta['series'])
    
    if clean_series:
        dest_dir = os.path.join(LIBRARY_DIR, clean_auth, clean_series, clean_title)
    else:
        dest_dir = os.path.join(LIBRARY_DIR, clean_auth, clean_title)
    
    os.makedirs(dest_dir, exist_ok=True)

    # 2. Move & Tag Files
    file_list.sort()
    total = len(file_list)
    
    # Progress Bar in Sidebar to not clutter UI
    prog = st.sidebar.progress(0, text="Processing...")
    
    for i, src in enumerate(file_list):
        ext = os.path.splitext(src)[1]
        
        # New Filename: "01 - Title.mp3" or just "Title.mp3"
        if total > 1:
            track_str = str(i+1).zfill(len(str(total)))
            new_name = f"{track_str} - {clean_title}{ext}"
        else:
            new_name = f"{clean_title}{ext}"
            
        dst = os.path.join(dest_dir, new_name)
        
        # COPY (Safety First)
        shutil.copy2(src, dst)
        
        # TAG
        try:
            if ext in ['.m4b', '.m4a']:
                audio = MP4(dst)
                if audio.tags is None: audio.add_tags()
                audio.tags['\xa9nam'] = meta['title']; audio.tags['\xa9ART'] = meta['authors']
                audio.tags['\xa9alb'] = meta['series'] if meta['series'] else meta['title']
                audio.tags['desc'] = meta['desc']; audio.tags['trkn'] = [(i+1, total)]
                if meta['year']: audio.tags['\xa9day'] = meta['year']
                if meta['img']: 
                    try: audio.tags['covr'] = [MP4Cover(requests.get(meta['img']).content, imageformat=MP4Cover.FORMAT_JPEG)]
                    except: pass
                audio.save()
            elif ext == '.mp3':
                try: audio = ID3(dst) 
                except: audio = ID3()
                audio.add(TIT2(encoding=3, text=meta['title']))
                audio.add(TPE1(encoding=3, text=meta['authors']))
                audio.add(TALB(encoding=3, text=meta['series'] if meta['series'] else meta['title']))
                audio.add(TRCK(encoding=3, text=f"{i+1}/{total}"))
                if meta['desc']: audio.add(COMM(encoding=3, lang='eng', desc='Description', text=meta['desc']))
                if meta['img']:
                    try: audio.add(APIC(3, 'image/jpeg', 3, 'Front Cover', requests.get(meta['img']).content))
                    except: pass
                audio.save(dst)
        except: pass
        
        prog.progress((i+1)/total)

    # 3. Create JSON
    json_data = {
        "title": meta['title'],
        "authors": [meta['authors']],
        "series": [meta['series']] if meta['series'] else [],
        "narrators": [meta['narrators']] if meta['narrators'] else [],
        "description": meta['desc'],
        "publishYear": meta['year'],
        "cover": meta['img']
    }
    
    # Handle Series Sequence
    if meta['series'] and meta['part']:
        json_data["series"] = [{"sequence": meta['part'], "name": meta['series']}]

    with open(os.path.join(dest_dir, "metadata.json"), 'w') as f:
        json.dump(json_data, f, indent=4)

    # 4. Update History (Log the SOURCE path to prevent re-scan)
    # If it came from a folder, log the folder. If files, log the parent folder.
    parent_folder = os.path.dirname(file_list[0])
    
    hist = load_json(HISTORY_FILE, [])
    if parent_folder not in hist:
        hist.append(parent_folder)
        save_json(HISTORY_FILE, hist)

    prog.empty()
    return clean_title

# --- UI LAYOUT ---
col_src, col_edit, col_lib = st.columns([1.2, 1.2, 0.8])

# ==========================================
# 1. LEFT COLUMN: SOURCES (Queue & Explorer)
# ==========================================
with col_src:
    tab_queue, tab_ex = st.tabs(["🕵️ Untidy Queue", "📂 File Explorer"])
    
    # --- TAB 1: AUTO QUEUE ---
    with tab_queue:
        # Filter history
        hist = load_json(HISTORY_FILE, [])
        all_found = get_auto_queue()
        # Filter out what's in history
        queue_items = [x for x in all_found if x['path'] not in hist]
        
        if not queue_items:
            st.info("Queue is empty! (Everything imported?)")
            selected_queue = None
        else:
            # Create DataFrame for display
            df_q = pd.DataFrame(queue_items)
            df_q['Status'] = "⚪ New"
            
            sel_q = st.dataframe(
                df_q[['Status', 'name']],
                column_config={"name": st.column_config.TextColumn("Folder Name")},
                use_container_width=True,
                hide_index=True,
                on_select="rerun",
                selection_mode="single-row",
                height=600
            )
            
            selected_queue = None
            if sel_q.selection.rows:
                idx = sel_q.selection.rows[0]
                selected_queue = queue_items[idx]

    # --- TAB 2: MANUAL EXPLORER ---
    with tab_ex:
        curr_path = st.session_state['exp_path']
        
        # Breadcrumbs / Nav
        c_up, c_path = st.columns([0.2, 0.8])
        with c_up:
            if st.button("⬆️", help="Go Up"):
                st.session_state['exp_path'] = os.path.dirname(curr_path)
                st.rerun()
        with c_path:
            st.caption(f".../{os.path.basename(curr_path)}/")

        # Get contents
        try:
            items = sorted(os.listdir(curr_path))
            # Build Dataframe for selection
            file_data = []
            for i in items:
                full = os.path.join(curr_path, i)
                is_dir = os.path.isdir(full)
                # Filter: Show Dirs and Audio Files only
                if is_dir or i.lower().endswith(('.mp3','.m4b','.m4a','.flac')):
                    file_data.append({
                        "icon": "📁" if is_dir else "🎵",
                        "name": i,
                        "path": full,
                        "type": "folder" if is_dir else "file"
                    })
            
            if file_data:
                df_ex = pd.DataFrame(file_data)
                
                # SELECTION TABLE
                sel_ex = st.dataframe(
                    df_ex[['icon', 'name']],
                    column_config={
                        "icon": st.column_config.TextColumn("", width="small"),
                        "name": st.column_config.TextColumn("Name"),
                    },
                    use_container_width=True,
                    hide_index=True,
                    on_select="rerun",
                    selection_mode="multi-row", # ENABLE MULTI-SELECT
                    height=550
                )
                
                selected_files_rows = sel_ex.selection.rows
                selected_explorer_items = [file_data[i] for i in selected_files_rows]
            else:
                st.info("Empty folder.")
                selected_explorer_items = []

        except Exception as e:
            st.error(f"Error: {e}")
            selected_explorer_items = []

# ==========================================
# 2. MIDDLE COLUMN: THE BUILDER
# ==========================================
with col_edit:
    st.subheader("🛠️ Book Builder")
    
    # --- DETERMINE ACTIVE SELECTION ---
    # Logic: If user selected something in Explorer, that takes priority.
    # Otherwise, use the Queue selection.
    
    active_files = []
    active_name = ""
    
    if selected_explorer_items:
        # EXPLORER MODE
        st.info(f"Selected {len(selected_explorer_items)} items from Explorer")
        
        # Check if user selected exactly ONE folder -> Offer to "Open" it
        if len(selected_explorer_items) == 1 and selected_explorer_items[0]['type'] == 'folder':
            target_folder = selected_explorer_items[0]['path']
            if st.button(f"📂 Open '{selected_explorer_items[0]['name']}'"):
                st.session_state['exp_path'] = target_folder
                st.rerun()
        
        # Build file list
        for item in selected_explorer_items:
            if item['type'] == 'folder':
                # Add all audio in folder
                for root, _, fs in os.walk(item['path']):
                    for f in fs:
                        if f.lower().endswith(('.mp3','.m4b','.m4a','.flac')):
                            active_files.append(os.path.join(root, f))
            else:
                active_files.append(item['path'])
        
        # Guess name from first item
        if active_files:
            active_name = os.path.splitext(os.path.basename(active_files[0]))[0]
            
    elif selected_queue:
        # QUEUE MODE
        st.info(f"Editing: {selected_queue['name']}")
        active_files = selected_queue['file_list']
        active_name = selected_queue['name']
    
    else:
        st.warning("👈 Select a book from the Queue or highlight files in Explorer.")
        st.stop()

    # --- EDITOR FORM ---
    
    # 1. Search Bar
    c_prov, c_search, c_go = st.columns([1, 2, 1])
    with c_prov:
        prov = st.selectbox("Source", ["Apple Books", "Google Books"], label_visibility="collapsed")
    with c_search:
        clean = clean_search_query(active_name)
        query = st.text_input("Search Meta", value=clean, label_visibility="collapsed")
    with c_go:
        search_trigger = st.button("🔍 Search")

    # 2. Results Handler
    if search_trigger:
        with st.spinner("Searching..."):
            results = fetch_metadata(query, prov)
            if results:
                st.session_state['search_res'] = results
                st.session_state['fill_idx'] = 0 # Default to first
            else:
                st.error("No matches.")

    # 3. Fill Form from Results
    meta_fill = {}
    if 'search_res' in st.session_state:
        res_list = st.session_state['search_res']
        res_names = [f"{r['authors']} - {r['title']}" for r in res_list]
        
        selected_fill = st.selectbox("Select Match:", res_names)
        # Find index
        idx = res_names.index(selected_fill)
        meta_fill = res_list[idx]

    # 4. THE FORM
    with st.form("book_editor"):
        c1, c2 = st.columns(2)
        f_auth = c1.text_input("Author", value=meta_fill.get('authors', ''))
        f_title = c1.text_input("Title", value=meta_fill.get('title', active_name))
        f_narr = c1.text_input("Narrator", value=meta_fill.get('narrators', ''))
        f_series = c2.text_input("Series", value=meta_fill.get('series', ''))
        f_part = c2.text_input("Part #", value=meta_fill.get('part', ''))
        f_year = c2.text_input("Year", value=meta_fill.get('year', ''))
        f_desc = st.text_area("Description", value=meta_fill.get('desc', ''))
        f_img = st.text_input("Cover URL", value=meta_fill.get('img', ''))
        
        if f_img: st.image(f_img, width=120)
        
        st.write(f"**Files to Process:** {len(active_files)}")
        
        if st.form_submit_button("🚀 Import Book", type="primary"):
            if not f_title or not f_auth:
                st.error("Title and Author are required.")
            else:
                # Bundle Metadata
                final_meta = {
                    "title": f_title, "authors": f_auth, "narrators": f_narr,
                    "series": f_series, "part": f_part, "year": f_year,
                    "desc": f_desc, "img": f_img
                }
                
                # PROCESS
                done_name = process_book(active_files, final_meta)
                
                # Feedback & Refresh
                st.success(f"Successfully Imported: {done_name}")
                st.session_state['last_processed'] = done_name
                
                # Force Cache Clear to update Queue and Library
                st.cache_data.clear()
                time.sleep(1.5)
                st.rerun()

# ==========================================
# 3. RIGHT COLUMN: LIBRARY STATUS
# ==========================================
with col_lib:
    st.subheader("📚 Library")
    
    # Force Rescan Button
    if st.button("🔄 Rescan All"):
        st.cache_data.clear()
        scan_library_now()
        st.rerun()
        
    # Get Current Library
    lib_items = load_json(CACHE_FILE, [])
    if not lib_items:
        # First run check
        lib_items = scan_library_now()
    
    if lib_items:
        df_lib = pd.DataFrame(lib_items)
        st.dataframe(
            df_lib[['name']], 
            column_config={"name": st.column_config.TextColumn("Imported Books")},
            hide_index=True,
            use_container_width=True,
            height=600
        )
    else:
        st.caption("Library is empty.")
