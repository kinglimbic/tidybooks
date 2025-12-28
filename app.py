import os
import shutil
import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter import ttk

# --- CONFIGURATION ---
DEFAULT_START_DIR = "/volume1/Zack/media/MAM - Audiobooks - Seeding" 
LIBRARY_DESTINATION = "/volume1/Zack/media/audiobooks"

class MetadataEditor(tk.Toplevel):
    """
    A popup window to edit metadata before importing.
    This ensures we use your MANUAL edits, not just the auto-detected ones.
    """
    def __init__(self, parent, selected_files):
        super().__init__(parent)
        self.title("Verify Metadata & Folder Structure")
        self.geometry("1000x600")
        
        # List to hold the entry widgets so we can read them later
        self.rows = [] 
        
        # --- SCROLLABLE AREA SETUP ---
        canvas = tk.Canvas(self)
        scrollbar = tk.Scrollbar(self, orient="vertical", command=canvas.yview)
        self.scroll_frame = tk.Frame(canvas)

        self.scroll_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=self.scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # --- BUILD EDITING ROWS ---
        for src_path in selected_files:
            folder_name = os.path.basename(src_path)
            
            # 1. Guess Metadata from Folder Name (Author - Series - Title)
            parts = folder_name.split(' - ')
            
            # Default values based on split
            val_author = parts[0] if len(parts) > 0 else "Unknown"
            val_series = parts[1] if len(parts) > 2 else "" # Assumes middle is series if 3 parts exist
            val_title = parts[-1] if len(parts) > 1 else folder_name

            # 2. Create UI Row
            row_frame = tk.LabelFrame(self.scroll_frame, text=f"Source: {folder_name}", padx=10, pady=5)
            row_frame.pack(fill=tk.X, padx=10, pady=5)

            # Author Entry
            tk.Label(row_frame, text="Author:").pack(side=tk.LEFT)
            entry_author = tk.Entry(row_frame, width=25)
            entry_author.insert(0, val_author)
            entry_author.pack(side=tk.LEFT, padx=5)

            # Series Entry
            tk.Label(row_frame, text="Series:").pack(side=tk.LEFT)
            entry_series = tk.Entry(row_frame, width=25)
            entry_series.insert(0, val_series)
            entry_series.pack(side=tk.LEFT, padx=5)

            # Title Entry
            tk.Label(row_frame, text="Title:").pack(side=tk.LEFT)
            entry_title = tk.Entry(row_frame, width=35)
            entry_title.insert(0, val_title)
            entry_title.pack(side=tk.LEFT, padx=5)

            # Save references to these specific widgets
            self.rows.append({
                "src": src_path,
                "author_widget": entry_author,
                "series_widget": entry_series,
                "title_widget": entry_title
            })

        # --- BOTTOM BUTTONS ---
        btn_frame = tk.Frame(self, pady=10)
        btn_frame.pack(fill=tk.X, side=tk.BOTTOM)
        
        tk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side=tk.LEFT, padx=20)
        tk.Button(btn_frame, text="CONFIRM IMPORT", bg="#d9fdd3", font=("Arial", 10, "bold"), 
                  command=self.run_import).pack(side=tk.RIGHT, padx=20)

    def run_import(self):
        success_count = 0
        errors = []

        # Create destination root if missing
        if not os.path.exists(LIBRARY_DESTINATION):
            os.makedirs(LIBRARY_DESTINATION, exist_ok=True)

        for row in self.rows:
            # --- CRITICAL FIX ---
            # We read the values using .get() RIGHT NOW. 
            # This captures exactly what is currently in the text box.
            author = row['author_widget'].get().strip()
            series = row['series_widget'].get().strip()
            title = row['title_widget'].get().strip()
            src_path = row['src']

            # Build new folder name based on inputs
            if series:
                new_folder_name = f"{author} - {series} - {title}"
            else:
                new_folder_name = f"{author} - {title}"

            dest_path = os.path.join(LIBRARY_DESTINATION, new_folder_name)

            try:
                if os.path.exists(dest_path):
                     errors.append(f"SKIPPED (Exists): {new_folder_name}")
                else:
                    # Perform Copy
                    if os.path.isdir(src_path):
                        shutil.copytree(src_path, dest_path)
                    else:
                        os.makedirs(dest_path, exist_ok=True)
                        shutil.copy2(src_path, dest_path)
                    
                    success_count += 1

            except Exception as e:
                errors.append(f"Error on {new_folder_name}: {e}")

        # Summary
        msg = f"Imported {success_count} items."
        if errors:
            msg += "\n\nErrors:\n" + "\n".join(errors)
            messagebox.showwarning("Result", msg)
        else:
            messagebox.showinfo("Success", msg)
            self.destroy() # Close the editor


class AudiobookImporter(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Audiobook Importer (Copy Only)")
        self.geometry("900x650")

        # Variables
        self.source_dir = tk.StringVar(value=DEFAULT_START_DIR)
        self.file_vars = {} 

        # --- UI LAYOUT ---
        
        # 1. Top Area: Source Selection
        top_frame = tk.Frame(self)
        top_frame.pack(fill=tk.X, padx=10, pady=10)
        
        tk.Label(top_frame, text="Source (Seeding):").pack(side=tk.LEFT)
        tk.Entry(top_frame, textvariable=self.source_dir, width=60).pack(side=tk.LEFT, padx=5)
        tk.Button(top_frame, text="Browse...", command=self.browse_folder).pack(side=tk.LEFT)
        tk.Button(top_frame, text="Refresh List", command=self.load_files).pack(side=tk.LEFT, padx=5)

        # 2. Middle Area: File List
        list_frame = tk.Frame(self, bd=2, relief=tk.SUNKEN)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        self.canvas = tk.Canvas(list_frame, bg="white")
        self.scrollbar = tk.Scrollbar(list_frame, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = tk.Frame(self.canvas, bg="white")

        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )

        self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # 3. Bottom Area: Buttons
        bottom_frame = tk.Frame(self)
        bottom_frame.pack(fill=tk.X, padx=10, pady=15)

        tk.Button(bottom_frame, text="Select All", command=self.select_all, width=12).pack(side=tk.LEFT, padx=5)
        tk.Button(bottom_frame, text="Deselect All", command=self.deselect_all, width=12).pack(side=tk.LEFT, padx=5)

        # UPDATED BUTTON: Calls open_editor instead of import directly
        tk.Button(bottom_frame, text="REVIEW & COPY", bg="#d9fdd3", font=("Arial", 10, "bold"), 
                 command=self.open_editor).pack(side=tk.RIGHT, padx=10)

        if os.path.exists(self.source_dir.get()):
            self.load_files()

    def browse_folder(self):
        directory = filedialog.askdirectory(initialdir=self.source_dir.get())
        if directory:
            self.source_dir.set(directory)
            self.load_files()

    def load_files(self):
        for widget in self.scrollable_frame.winfo_children():
            widget.destroy()
        self.file_vars = {}

        current_path = self.source_dir.get()
        try:
            items = sorted(os.listdir(current_path))
            for item in items:
                if item.startswith('.'): continue
                full_path = os.path.join(current_path, item)
                
                var = tk.IntVar()
                chk = tk.Checkbutton(self.scrollable_frame, text=item, variable=var, bg="white", anchor="w")
                chk.pack(fill=tk.X, padx=5, pady=2)
                self.file_vars[full_path
