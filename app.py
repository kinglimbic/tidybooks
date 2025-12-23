import streamlit as st
import os
import shutil
import requests
import json
import re
from mutagen.mp4 import MP4, MP4Cover
from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, COMM
import time

# --- Configuration ---
# These match the container internal paths
DOWNLOAD_DIR = "/downloads"
LIBRARY_DIR = "/audiobooks"
AUDNEXUS_API = "https://api.audnexus.com/books"

# Update 1: Page Title
st.set_page_config(page_title="TidyBooks", layout="wide", page_icon="📚")

# --- Helper Functions ---
def sanitize_filename(name):
    """Removes illegal characters for file/folder names."""
    if not name: return "Unknown"
    clean = name.replace("/", "-").replace("\\", "-")
    return re.sub(r'[<>:"|?*]', '', clean).strip()

def get_candidates():
    """Scans download folder for audio files recursively."""
    candidates = []
    if not os.path.exists(DOWNLOAD_DIR):
        return []
        
    for root, dirs, files in os.walk(DOWNLOAD_DIR):
        for file in files:
            if file.lower().endswith(('.mp3', '.m4b', '.m4a', '.flac')):
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, DOWNLOAD_DIR)
                candidates.append((rel_path, full_path))
    return sorted(candidates, key=lambda x: x[0])

def fetch_metadata(query):
    """Searches Audnexus (Audible mirror)."""
    try:
        params = {'q': query}
        r = requests.get(AUDNEXUS_API, params=params)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        st.error(f"API Error: {e}")
    return []

def process_book(source_path, author, title, series, series_part, desc, cover_url, narrator, publish_year):
    """Copies, organizes, tags, and generates JSON for ABS."""
    
    clean_author = sanitize_filename(author)
    clean_title = sanitize_filename(title)
    clean_series = sanitize_filename(series)
    
    if clean_series:
        dest_folder = os.path.join(LIBRARY_DIR, clean_author, clean_series)
    else:
        dest_folder = os.path.join(LIBRARY_DIR, clean_author)
    
    os.makedirs(dest_folder, exist_ok=True)
    
    ext = os.path.splitext(source_path)[1]
    dest_filename = f"{clean_title}{ext}"
    dest_path = os.path.join(dest_folder, dest_filename)

    with st.spinner(f"Tidying up {clean_title}..."):
        shutil.copy2(source_path, dest_path)

    try:
        if ext.lower() in ['.m4b', '.m4a']:
            audio = MP4(dest_path)
            if audio.tags is None: audio.add_tags()
            audio.tags['\xa9nam'] = title
            audio.tags['\xa9ART'] = author
            audio.tags['\xa9alb'] = clean_series if clean_series else title
            audio.tags['desc'] = desc
            if publish_year: audio.tags['\xa9day'] = publish_year
            if cover_url:
                try:
                    img_data = requests.get(cover_url).content
                    audio.tags['covr'] = [MP4Cover(img_data, imageformat=MP4Cover.FORMAT_JPEG)]
                except: pass
            audio.save()
            
        elif ext.lower() == '.mp3':
            try: audio = ID3(dest_path) 
            except: audio = ID3()
            audio.add(TIT2(encoding=3, text=title))
            audio.add(TPE1(encoding=3, text=author))
            audio.add(TALB(encoding=3, text=clean_series if clean_series else title))
            if desc: audio.add(COMM(encoding=3, lang='eng', desc='Description', text=desc))
            if cover_url:
                try:
                    img_data = requests.get(cover_url).content
                    audio.add(APIC(3, 'image/jpeg', 3, 'Front Cover', img_data))
                except: pass
            audio.save(dest_path)

    except Exception as e:
        st.warning(f"Tagging warning: {e}")

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
        try:
            abs_metadata["series"] = [{"sequence": series_part, "name": series}]
        except:
            abs_metadata["series"] = [series]

    json_path = os.path.join(dest_folder, "metadata.json")
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(abs_metadata, f, indent=4)

    st.success(f"✅ TidyBooks: Imported {clean_title}")
    st.balloons()
    time.sleep(2)
    st.rerun()

# --- GUI Layout ---
# Update 2: Visible Header
st.title("🎧 TidyBooks")

col1, col2 = st.columns([1, 2])

with col1:
    st.subheader("📂 Untidy Queue")
    if st.button("Refresh List"):
        st.rerun()
        
    files = get_candidates()
    if not files:
        st.info("No audio files found in /downloads.")
        selected_file_tuple = None
    else:
        selected_rel_path = st.radio("Select file:", [f[0] for f in files], index=0)
        selected_file_full = next((f[1] for f in files if f[0] == selected_rel_path), None)

with col2:
    if files and selected_file_full:
        filename_only = os.path.basename(selected_file_full)
        st.subheader("✏️ Tidy Details")
        st.caption(f"File: `{filename_only}`")
        
        with st.expander("🔍 Search Database", expanded=True):
            col_search, col_btn = st.columns([3,1])
            with col_search:
                search_query = st.text_input("Title / Author", value=filename_only.split('.')[0].replace('_', ' '))
            with col_btn:
                st.write("##") 
                do_search = st.button("Search")
            
            found_meta = {}
            if do_search:
                results = fetch_metadata(search_query)
                if results:
                    st.session_state['search_results'] = results
                else:
                    st.warning("No matches found.")
            
            if 'search_results' in st.session_state and st.session_state['search_results']:
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
            submitted = st.form_submit_button("🚀 Make Tidy & Import", type="primary")
            
            if submitted:
                if new_author and new_title:
                    process_book(selected_file_full, new_author, new_title, new_series, new_part, new_desc, cover_img, new_narrator, new_year)
                else:
                    st.error("Author and Title are required!")