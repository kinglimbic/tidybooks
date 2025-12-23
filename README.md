What it does

1. Library & Download Scanning
scan_downloads_snapshot: Scans a /downloads directory for audio files (.mp3, .m4b, .m4a, .flac). It intelligently groups files:
Standard Logic: Treats subfolders as individual books.
Collection Logic: If files are in the root or a "collection" folder, it groups them by filename similarity (e.g., "Book A - 01.mp3" and "Book A - 02.mp3" get grouped together).
scan_library_now: Scans the destination /audiobooks directory to know what you already have.
Matching: It compares new downloads against the existing library and a history log (processed_log.json) to flag items as "New" (Untidy), "Match" (Duplicate/Upgrade), or "Done".
2. Metadata Search
The app connects to three external APIs to fetch book details (Title, Author, Narrator, Series, Cover Art, etc.):
Audnexus (Audible): Via direct ASIN lookup.
Apple Books (iTunes): Via search query.
Google Books: Via search query.
It includes regex helpers (extract_details_smart) to parse raw descriptions and titles to extract specific details like Series Name and Part Number.
3. File Processing & Tagging
process_selection: This is the core action function.
Renaming: Renames files to a clean format (e.g., 01 - Title.mp3).
Moving: Moves files into a structured directory hierarchy: /audiobooks/Author/Series/Title/.
Tagging: Uses the mutagen library to embed metadata (ID3 tags for MP3, MP4 tags for M4B/M4A) directly into the files.
Metadata File: Saves a metadata.json file in the book's folder.
Cleanup: Can delete the source files after processing.
4. User Interface (Streamlit)
The UI is divided into three columns:

Column 1 (The Queue): Tabs showing lists of books found in downloads.
Untidy: New items found automatically.
Built: Items manually grouped by the user.
Match: Items that seem to duplicate existing library books.
Done: History of processed items.
Column 2 (The Editor):
Shows the selected book.
Provides a search bar to find metadata.
Displays a form to manually edit metadata before importing.
The "Import" button triggers the processing.
Column 3 (The Explorer):
A manual file browser for the downloads folder.
Allows users to select specific files or folders and "Bundle" them into a single book entry manually (useful for messy folders).
5. Technical Details
State Management: Uses st.session_state heavily to manage navigation, selected items, and form data across re-runs.
Caching: Uses @st.cache_data to prevent re-scanning the filesystem constantly, improving performance.
Persistence: Saves history and cache to JSON files in /app/data.
