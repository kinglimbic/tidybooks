import streamlit as st
import pandas as pd
import os
import shutil
import requests
import json
import re
import time
import difflib
from mutagen.mp4 import MP4, MP4Cover
from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, COMM, TRCK

# --- Configuration ---
# PATHS
DOWNLOAD_DIR = "/downloads"
LIBRARY_DIR = "/audiobooks"
DATA_DIR = "/app/data"

# API ENDPOINTS
AUDNEXUS_API = "https://api.audnex.us/books"
ITUNES_API = "https://itunes.apple.com/search"
GOOGLE_BOOKS_API = "https://www.googleapis.com/books/v1/volumes"

os.makedirs(DATA_DIR, exist_ok=True)
st.set_page_config(page_title="TidyBooks", layout="wide", page_icon="📚")

# --- Session State ---
if 'exp_path' not in st.session_state: st.session_state['exp_path'] = DOWNLOAD_DIR
if 'draft_files' not in st.session_state: st.session_state['draft_files'] = []
if 'draft_meta' not in st.session_state: st.session_state['draft_meta'] = {}
if 'search_results' not in st.session_state: st.session_state['search_results'] = []
if 'confirm_overwrite' not in st.session_state: st.session_state['confirm_overwrite'] = False

keys = ['form_auth', 'form_title', 'form_narr', 'form_series', 'form_part', 'form_year', 'form_desc', 'form_img']
for k in keys:
    if k not in st.session_state: st.session_state[k] = ""

# --- Helper Functions ---
def sanitize_filename(name):
    if not name: return "Unknown"
    clean = name.replace("/", "-").replace("\\", "-")
    clean = re.sub(r'[<>:"|?*]', '', clean).strip()
    return "Unknown" if not clean else clean

def normalize_text(text):
    """Simplifies text for fuzzy matching"""
    if not text: return ""
    text = text.lower()
    text = re.sub(r'\b(audiobook|mp3|m4b|m4a|flac)\b', '', text)
    text = re.sub(r'[^a-z0-9]', '', text) 
    return text

def get_audio_files_recursive(path_list):
    audio_exts = ('.mp3', '.m4b', '.m4a', '.flac', '.ogg')
    found_files = []
    for path in path_list:
        if os.path.isfile(path):
            if path.lower().endswith(audio_exts):
                found_files.append(path)
        elif os.path.isdir(path):
            for root, _, files in os.walk(path):
                for f in files:
                    if f.lower().endswith(audio_exts):
                        found_files.append(os.path.join(root, f))
    return sorted(found_files)

# --- Library Scanning (The "Brain") ---
@st.cache_data(ttl=300)
def scan_library():
    """Scans /audiobooks to find what you already have."""
    known_books = []
    if not os.path.exists(LIBRARY_DIR): return []

    for author in os.listdir(LIBRARY_DIR):
        auth_path = os.path.join(LIBRARY_DIR, author)
        if not os.path.isdir(auth_path) or author.startswith('.'): continue
        
        for item in os.listdir(auth_path):
            item_path = os.path.join(auth_path, item)
            if not os.path.isdir(item_path): continue
            
            # Check if Series (contains subfolders) or Book
            sub_dirs = [d for d in os.listdir(item_path) if os.path.isdir(os.path.join(item_path, d))]
            
            if sub_dirs:
                for book in sub_dirs:
                    known_books.append({
                        "title": book,
                        "author": author,
                        "norm": normalize_text(book)
                    })
            else:
                known_books.append({
                    "title": item,
                    "author": author,
                    "norm": normalize_text(item)
                })
    return known_books

def check_duplicate_strict(title, known_books):
    """Checks if a title exists in the library."""
    norm_title = normalize_text(title)
    if len(norm_title) < 4: return None
    
    for b in known_books:
        # Exact match or high fuzzy match
        if norm_title == b['norm'] or (len(norm_title) > 5 and norm_title in b['norm']):
            return b
    return None

def check_folder_content_status(folder_path, known_books):
    """
    Peeks inside a download folder. 
    If the files inside match a known book, returns that book.
    """
    try:
        # Get first few audio files
        files = [f for f in os.listdir(folder_path) if f.lower().endswith(('.mp3','.m4b','.m4a'))]
        if not files: return None
        
        # Use the first file to guess
        stem = os.path.splitext(files[0])[0]
        # Clean common prefixes like "01 - " or "Chapter 1"
        clean_stem = re.sub(r'^(\d+[-_\s]+|chapter\s+\d+\s+)', '', stem, flags=re.IGNORECASE)
        
        return check_duplicate_strict(clean_stem, known_books)
    except:
        return None

# --- Metadata APIs ---
def extract_details_smart(title, desc, subtitle=""):
    narrator, series, part = "", "", ""
    full_text = f"{title or ''} {subtitle or ''} {desc or ''}"
    match_narr = re.search(r"(?:narrated|read)\s+by\s+([A-Za-z\s\.]+?)(?:[\.,\n\(-]|$)", full_text, re.IGNORECASE)
    if match_narr: narrator = match_narr.group(1).strip()
    if title and ":" in title:
        parts = title.split(":")
        if len(parts[0]) > 3: series = parts[0].strip()
    match_series = re.search(r"\(([^)]+?)(?:,|#|;)\s*(?:Book|Vol|Part)?\s*(\d+)\)", title or "", re.IGNORECASE)
    if match_series:
        series = match_series.group(1).strip()
        part = match_series.group(2).strip()
    return narrator, series, part

def fetch_metadata(query, provider, asin=None):
    results = []
    try:
        if asin:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            r = requests.get(f"{AUDNEXUS_API}/{asin}", headers=headers, timeout=8)
            if r.status_code == 200:
                b = r.json()
                results.append({
                    "title": b.get('title'), "authors": ", ".join(b.get('authors', [])),
                    "narrators": ", ".join(b.get('narrators', [])), "series": b.get('seriesPrimary'),
                    "part": b.get('seriesPrimarySequence'), "summary": b.get('summary'),
                    "image": b.get('image'), "releaseDate": b.get('releaseDate'),
                    "source": f"Audible ({asin})"
                })
        elif provider == "Apple Books":
            r = requests.get(ITUNES_API, params={'term': query, 'media': 'audiobook', 'limit': 10}, timeout=8)
            if r.status_code == 200:
                for item in r.json().get('results', []):
                    s_narr, s_ser, s_part = extract_details_smart(item.get('collectionName'), item.get('description'))
                    results.append({
                        "title": item.get('collectionName'), "authors": item.get('artistName'),
                        "narrators": s_narr, "series": s_ser, "part": s_part, 
                        "summary": item.get('description'), "image": item.get('artworkUrl100', '').replace('100x100', '600x600'),
                        "releaseDate": item.get('releaseDate')
                    })
        elif provider == "Google Books":
            r = requests.get(GOOGLE_BOOKS_API, params={"q": query, "maxResults": 10, "langRestrict": "en"}, timeout=8)
            if r.status_code == 200:
                for item in r.json().get('items', []):
                    info = item.get('volumeInfo', {})
                    img = info.get('imageLinks', {}).get('thumbnail', '').replace('http:', 'https:')
                    auths = info.get('authors') or []
                    s_narr, s_ser, s_part = extract_details_smart(info.get('title'), info.get('description'), info.get('subtitle'))
                    results.append({
                        "title": info.get('title'), "authors": auths[0] if auths else "",
                        "narrators": s_narr, "series": s_ser, "part": s_part, 
                        "summary": info.get('description'), "image": img, "releaseDate": info.get('publishedDate')
                    })
    except Exception as e: st.error(f"Search Error: {e}")
    return results

# --- Import Logic ---
def perform_import(file_list, meta):
    clean_auth = sanitize_filename(meta['form_auth'])
    clean_title = sanitize_filename(meta['form_title'])
    
    if meta['form_series'] and meta['form_series'].strip():
        clean_series = sanitize_filename(meta['form_series'])
        dest_dir = os.path.join(LIBRARY_DIR, clean_auth, clean_series, clean_title)
    else:
        dest_dir = os.path.join(LIBRARY_DIR, clean_auth, clean_title)
    
    os.makedirs(dest_dir, exist_ok=True)
    
    bar = st.progress(0)
    status = st.empty()
    total = len(file_list)
    pad = max(2, len(str(total)))
    
    for i, src in enumerate(file_list):
        ext = os.path.splitext(src)[1]
        new_name = f"{str(i+1).zfill(pad)} - {clean_title}{ext}"
        dst = os.path.join(dest_dir, new_name)
        status.text(f"Copying: {os.path.basename(src)}")
        try: shutil.copy2(src, dst)
        except: pass

        try:
            if ext in ['.m4b', '.m4a']:
                audio = MP4(dst)
                if audio.tags is None: audio.add_tags()
                audio.tags['\xa9nam'] = meta['form_title']
                audio.tags['\xa9ART'] = meta['form_auth']
                audio.tags['\xa9alb'] = meta['form_series'] if meta['form_series'] else meta['form_title']
                audio.tags['desc'] = meta['form_desc']
                if meta['form_year']: audio.tags['\xa9day'] = meta['form_year']
                if meta['form_img']:
                    try: audio.tags['covr'] = [MP4Cover(requests.get(meta['form_img']).content, imageformat=MP4Cover.FORMAT_JPEG)]
                    except: pass
                audio.save()
            elif ext == '.mp3':
                try: audio = ID3(dst) 
                except: audio = ID3()
                audio.add(TIT2(encoding=3, text=meta['form_title']))
                audio.add(TPE1(encoding=3, text=meta['form_auth']))
                audio.add(TALB(encoding=3, text=meta['form_series'] if meta['form_series'] else meta['form_title']))
                if meta['form_desc']: audio.add(COMM(encoding=3, lang='eng', desc='Description', text=meta['form_desc']))
                if meta['form_img']:
                    try: audio.add(APIC(3, 'image/jpeg', 3, 'Front Cover', requests.get(meta['form_img']).content))
                    except: pass
                audio.save(dst)
        except: pass
        bar.progress((i + 1) / total)

    abs_meta = {
        "title": meta['form_title'],
        "authors": [meta['form_auth']],
        "narrators": [meta['form_narr']] if meta['form_narr'] else [],
        "description": meta['form_desc'],
        "publishYear": meta['form_year'],
        "cover": meta['form_img']
    }
    if meta['form_series']:
        abs_meta["series"] = [{"sequence": meta['form_part'], "name": meta['form_series']}]
    
    with open(os.path.join(dest_dir, "metadata.json"), 'w') as f:
        json.dump(abs_meta, f, indent=4)

    st.success(f"✅ Imported: {meta['form_title']}")
    st.cache_data.clear()
    time.sleep(2)
    st.session_state['draft_files'] = []
    st.session_state['search_results'] = []
    st.session_state['confirm_overwrite'] = False
    st.rerun()

# ==================== MAIN UI ====================
col1, col2 = st.columns([1, 1])
known_books = scan_library()

# --- COLUMN 1: BROWSER ---
with col1:
    st.subheader("📂 Download Browser")
    curr = st.session_state['exp_path']
    
    c_nav1, c_nav2 = st.columns([0.2, 0.8])
    with c_nav1:
        if st.button("⬆️ Up"):
            st.session_state['exp_path'] = os.path.dirname(curr)
            st.rerun()
    with c_nav2: st.caption(curr)

    try:
        items = sorted(os.listdir(curr))
        items = [i for i in items if not i.startswith('.')]
        
        with st.form("browser_form"):
            selected_items = []
            
            st.markdown("**Folders**")
            folders = [i for i in items if os.path.isdir(os.path.join(curr, i))]
            
            for f in folders:
                full_path = os.path.join(curr, f)
                
                # 1. Check folder name itself
                dup = check_duplicate_strict(f, known_books)
                
                # 2. If no match, check contents (Deep Scan)
                if not dup:
                    dup = check_folder_content_status(full_path, known_books)

                # Icon Logic: 📚 = Imported, 📁 = New
                icon = "📚" if dup else "📁"
                label = f"{icon} {f}"
                if dup: label += f" (in Lib)"
                
                c1, c2 = st.columns([0.8, 0.2])
                with c1:
                    if st.checkbox(label, key=f"d_{f}"):
                        selected_items.append(full_path)
                with c2:
                    if st.form_submit_button("Open", key=f"btn_{f}", type="secondary"):
                        st.session_state['exp_path'] = full_path
                        st.rerun()

            st.markdown("**Files**")
            files = [i for i in items if os.path.isfile(os.path.join(curr, i))]
            audio_files = [f for f in files if f.lower().endswith(('.mp3','.m4b','.m4a','.flac'))]
            
            if not audio_files: st.caption("No audio files.")
            for f in audio_files:
                stem = os.path.splitext(f)[0]
                dup = check_duplicate_strict(stem, known_books)
                icon = "📚" if dup else "🎵"
                
                if st.checkbox(f"{icon} {f}", key=f"f_{f}"):
                    selected_items.append(os.path.join(curr, f))

            st.markdown("---")
            if st.form_submit_button("✨ Draft Book", type="primary", use_container_width=True):
                if selected_items:
                    flat_list = get_audio_files_recursive(selected_items)
                    if flat_list:
                        st.session_state['draft_files'] = flat_list
                        guess = os.path.basename(selected_items[0])
                        st.session_state['form_title'] = guess
                        st.rerun()
                    else: st.error("No audio files.")
                else: st.warning("Nothing selected.")
    except Exception as e: st.error(f"Error: {e}")

# --- COLUMN 2: IMPORT ---
with col2:
    st.subheader("📝 Metadata & Import")
    files = st.session_state['draft_files']
    
    if not files:
        st.info("👈 Select files to begin.")
    else:
        st.success(f"Drafting with {len(files)} files.")
        with st.expander("View Files"):
            st.write(files)
            if st.button("Clear"):
                st.session_state['draft_files'] = []
                st.rerun()

        st.markdown("### 1. Metadata Search")
        c_src, c_q = st.columns([1, 2])
        with c_src: provider = st.selectbox("Source", ["Apple Books", "Google Books", "Audible (ASIN)"])
        with c_q: query = st.text_input("Query", value=st.session_state.get('form_title', ''))
        
        if st.button("🔍 Search", use_container_width=True):
            with st.spinner("Searching..."):
                if provider == "Audible (ASIN)":
                    res = fetch_metadata(None, None, asin=query)
                else:
                    res = fetch_metadata(query, provider)
                st.session_state['search_results'] = res

        results = st.session_state['search_results']
        if results:
            opts = [f"{r.get('authors')} - {r.get('title')}" for r in results]
            sel_idx = st.selectbox("Matches:", range(len(opts)), format_func=lambda x: opts[x])
            sel = results[sel_idx]
            # Fill form
            st.session_state['form_auth'] = sel.get('authors') or ""
            st.session_state['form_title'] = sel.get('title') or ""
            st.session_state['form_narr'] = sel.get('narrators') or ""
            st.session_state['form_series'] = sel.get('series') or ""
            st.session_state['form_part'] = str(sel.get('part') or "")
            rd = sel.get('releaseDate') or ""
            st.session_state['form_year'] = rd[:4] if len(rd) >= 4 else ""
            st.session_state['form_desc'] = sel.get('summary') or ""
            st.session_state['form_img'] = sel.get('image') or ""

        st.markdown("### 2. Edit & Import")
        with st.form("import_form"):
            c1, c2 = st.columns(2)
            auth = c1.text_input("Author", key='form_auth')
            titl = c1.text_input("Title", key='form_title')
            narr = c1.text_input("Narrator", key='form_narr')
            seri = c2.text_input("Series", key='form_series')
            part = c2.text_input("Part #", key='form_part')
            year = c2.text_input("Year", key='form_year')
            desc = st.text_area("Desc", key='form_desc')
            img = st.text_input("Cover URL", key='form_img')
            if img: st.image(img, width=150)

            st.markdown("---")
            submitted = st.form_submit_button("🚀 Import Book", type="primary", use_container_width=True)
            
            if submitted:
                if auth and titl:
                    # Final Check
                    dup = check_duplicate_strict(titl, known_books)
                    if dup and not st.session_state.get('confirm_overwrite'):
                        st.warning(f"⚠️ Duplicate Found in Library!\n\n**{dup['title']}** by **{dup['author']}**")
                        st.session_state['confirm_overwrite'] = True
                        st.rerun()
                    else:
                        meta = {
                            'form_auth': auth, 'form_title': titl, 'form_narr': narr,
                            'form_series': seri, 'form_part': part, 'form_year': year,
                            'form_desc': desc, 'form_img': img
                        }
                        perform_import(files, meta)
                else:
                    st.error("Author/Title Required.")

        # Confirmation Button
        if st.session_state.get('confirm_overwrite'):
            st.write("")
            col_warn, col_ok = st.columns([3, 1])
            with col_warn: st.error("Do you want to import anyway? (Creates duplicate or merges)")
            with col_ok:
                if st.button("✅ Yes, Import"):
                    meta = {
                        'form_auth': st.session_state['form_auth'],
                        'form_title': st.session_state['form_title'],
                        'form_narr': st.session_state['form_narr'],
                        'form_series': st.session_state['form_series'],
                        'form_part': st.session_state['form_part'],
                        'form_year': st.session_state['form_year'],
                        'form_desc': st.session_state['form_desc'],
                        'form_img': st.session_state['form_img']
                    }
                    perform_import(files, meta)
