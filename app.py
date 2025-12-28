import streamlit as st
import os
import shutil
import pandas as pd

# --- CONFIGURATION ---
DEFAULT_START_DIR = "/volume1/Zack/media/MAM - Audiobooks - Seeding"
LIBRARY_DESTINATION = "/volume1/Zack/media/audiobooks"

st.set_page_config(page_title="Audiobook Importer", layout="wide")

# --- SESSION STATE INITIALIZATION ---
if 'selected_files' not in st.session_state:
    st.session_state['selected_files'] = []
if 'all_files' not in st.session_state:
    st.session_state['all_files'] = []

st.title("🎧 Audiobook Importer (Docker/Web)")

# --- SIDEBAR: FOLDER SELECTION ---
st.sidebar.header("Source Configuration")
source_path = st.sidebar.text_input("Source Path", value=DEFAULT_START_DIR)

if st.sidebar.button("Refresh File List"):
    # Force reload of file list
    st.session_state['selected_files'] = [] 

# --- MAIN: FILE EXPLORER ---
if os.path.exists(source_path):
    try:
        # Get all visible folders/files
        files = sorted([f for f in os.listdir(source_path) if not f.startswith('.')])
        st.session_state['all_files'] = files
        
        col1, col2 = st.columns([1, 4])
        with col1:
            # SELECT ALL BUTTON LOGIC
            if st.button("Select All"):
                st.session_state['selected_files'] = files
            if st.button("Deselect All"):
                st.session_state['selected_files'] = []
        
        with col2:
            st.info(f"Found {len(files)} items in: `{source_path}`")

        # FILE SELECTION WIDGET
        # We use a multiselect for clarity, pre-populated by our "Select All" logic
        selected = st.multiselect(
            "Select items to import:", 
            options=files,
            default=st.session_state['selected_files'],
            key='file_selector'
        )
        
        # Sync selection back to session state
        st.session_state['selected_files'] = selected

    except Exception as e:
        st.error(f"Error reading source folder: {e}")
else:
    st.error(f"Source path does not exist: {source_path}")


# --- STEP 2: REVIEW & METADATA EDITOR ---
if st.session_state['selected_files']:
    st.divider()
    st.header("📝 Review & Edit Metadata")
    st.caption("Edit the Author, Series, or Title below. This determines the destination folder structure.")

    # Prepare data for the Editable Dataframe (Spreadsheet view)
    data_list = []
    for item in st.session_state['selected_files']:
        parts = item.split(' - ')
        author = parts[0] if len(parts) > 0 else "Unknown"
        series = parts[1] if len(parts) > 2 else ""
        title = parts[-1] if len(parts) > 1 else item
        
        data_list.append({
            "Original Folder": item,
            "Author": author,
            "Series": series,
            "Title": title
        })
    
    # Create editable dataframe
    df = pd.DataFrame(data_list)
    edited_df = st.data_editor(df, use_container_width=True, num_rows="fixed")

    st.divider()

    # --- STEP 3: IMPORT ACTION ---
    st.warning(f"Destination: `{LIBRARY_DESTINATION}`")
    
    col_btn, col_warn = st.columns([1, 3])
    
    if col_btn.button("🚀 START COPY", type="primary"):
        success_count = 0
        errors = []
        
        # Create progress bar
        progress_bar = st.progress(0)
        status_text = st.empty()

        # Ensure destination exists
        if not os.path.exists(LIBRARY_DESTINATION):
            os.makedirs(LIBRARY_DESTINATION, exist_ok=True)

        total_files = len(edited_df)
        
        for index, row in edited_df.iterrows():
            # Update Progress
            progress_bar.progress((index + 1) / total_files)
            status_text.text(f"Copying: {row['Title']}...")

            # 1. READ FROM EDITED DATAFRAME
            author = str(row['Author']).strip()
            series = str(row['Series']).strip()
            title = str(row['Title']).strip()
            src_name = row['Original Folder']
            
            src_path = os.path.join(source_path, src_name)

            # 2. CONSTRUCT DESTINATION
            if series:
                new_folder_name = f"{author} - {series} - {title}"
            else:
                new_folder_name = f"{author} - {title}"
            
            dest_path = os.path.join(LIBRARY_DESTINATION, new_folder_name)

            # 3. PERFORM COPY
            try:
                if os.path.exists(dest_path):
                    errors.append(f"SKIPPED (Exists): {new_folder_name}")
                else:
                    if os.path.isdir(src_path):
                        shutil.copytree(src_path, dest_path)
                    else:
                        os.makedirs(dest_path, exist_ok=True)
                        shutil.copy2(src_path, dest_path)
                    success_count += 1
            except Exception as e:
                errors.append(f"Error {new_folder_name}: {e}")

        # Final Report
        progress_bar.empty()
        status_text.empty()
        
        if success_count > 0:
            st.success(f"✅ Successfully imported {success_count} items!")
            
        if errors:
            st.error("Issues Encountered:")
            for err in errors:
                st.write(f"- {err}")
        
        # Reset selection on success
        if success_count == total_files:
            st.balloons()
