import os
import shutil
import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter import ttk

# --- CONFIGURATION ---
# Defaulting to the specific paths you requested
DEFAULT_START_DIR = r"/volume1/Zack/media/MAM - Audiobooks - Seeding" 
LIBRARY_DESTINATION = r"/volume1/Zack/media/audiobooks"

class AudiobookImporter(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Audiobook Importer (Copy Only)")
        self.geometry("900x650")

        # Variables
        self.source_dir = tk.StringVar(value=DEFAULT_START_DIR)
        self.file_vars = {} # Dictionary to store file path -> checkbox variable

        # --- UI LAYOUT ---
        
        # 1. Top Area: Source Selection
        top_frame = tk.Frame(self)
        top_frame.pack(fill=tk.X, padx=10, pady=10)
        
        tk.Label(top_frame, text="Source (Seeding):").pack(side=tk.LEFT)
        tk.Entry(top_frame, textvariable=self.source_dir, width=60).pack(side=tk.LEFT, padx=5)
        tk.Button(top_frame, text="Browse...", command=self.browse_folder).pack(side=tk.LEFT)
        tk.Button(top_frame, text="Refresh List", command=self.load_files).pack(side=tk.LEFT, padx=5)

        # 2. Middle Area: File List
        # Using a Canvas + Frame to make the list scrollable
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

        # SELECT ALL / DESELECT ALL
        tk.Button(bottom_frame, text="Select All", command=self.select_all, width=12).pack(side=tk.LEFT, padx=5)
        tk.Button(bottom_frame, text="Deselect All", command=self.deselect_all, width=12).pack(side=tk.LEFT, padx=5)

        # IMPORT BUTTON
        tk.Button(bottom_frame, text="COPY TO LIBRARY", bg="#d9fdd3", font=("Arial", 10, "bold"), 
                 command=self.verify_and_import).pack(side=tk.RIGHT, padx=10)

        # Auto-load files on launch if the path exists
        if os.path.exists(self.source_dir.get()):
            self.load_files()

    def browse_folder(self):
        directory = filedialog.askdirectory(initialdir=self.source_dir.get())
        if directory:
            self.source_dir.set(directory)
            self.load_files()

    def load_files(self):
        # Clear existing checkboxes
        for widget in self.scrollable_frame.winfo_children():
            widget.destroy()
        self.file_vars = {}

        current_path = self.source_dir.get()
        
        try:
            # List directory content
            items = sorted(os.listdir(current_path))
            
            for item in items:
                # Skip hidden files (like .DS_Store or @eaDir on Synology)
                if item.startswith('.'):
                    continue
                    
                full_path = os.path.join(current_path, item)
                
                # Create Checkbox
                var = tk.IntVar()
                chk = tk.Checkbutton(self.scrollable_frame, text=item, variable=var, bg="white", anchor="w")
                chk.pack(fill=tk.X, padx=5, pady=2)
                
                self.file_vars[full_path] = var
                
        except Exception as e:
            messagebox.showerror("Error", f"Could not read directory:\n{e}")

    # --- FEATURE: SELECT ALL ---
    def select_all(self):
        for var in self.file_vars.values():
            var.set(1)

    def deselect_all(self):
        for var in self.file_vars.values():
            var.set(0)

    # --- FEATURE: VERIFICATION & SAFE COPY ---
    def verify_and_import(self):
        selected_files = [path for path, var in self.file_vars.items() if var.get() == 1]
        
        if not selected_files:
            messagebox.showwarning("No Selection", "Please select at least one folder/book to import.")
            return

        # Verification Message
        # Shows exactly where files are going
        msg = (
            f"You have selected {len(selected_files)} items to import.\n\n"
            f"DESTINATION FOLDER:\n{LIBRARY_DESTINATION}\n\n"
            "------------------------------------------------\n"
            "• This will perform a FULL COPY.\n"
            "• The original files in 'MAM - Audiobooks - Seeding' will be untouched.\n"
            "• Seeding will continue uninterrupted.\n\n"
            "Is the destination path correct?"
        )

        confirm = messagebox.askyesno("Verify Destination", msg)

        if confirm:
            self.run_copy_process(selected_files)

    def run_copy_process(self, files):
        success_count = 0
        errors = []

        # Create destination if it doesn't exist
        if not os.path.exists(LIBRARY_DESTINATION):
            try:
                os.makedirs(LIBRARY_DESTINATION)
            except OSError as e:
                messagebox.showerror("Error", f"Could not create destination folder:\n{e}")
                return

        for src_path in files:
            try:
                folder_name = os.path.basename(src_path)
                dest_path = os.path.join(LIBRARY_DESTINATION, folder_name)

                # Skip if destination already exists to prevent overwriting/mess
                if os.path.exists(dest_path):
                    errors.append(f"SKIPPED (Already exists): {folder_name}")
                    continue

                # PERFORM COPY (Not Move, Not Link)
                if os.path.isdir(src_path):
                    shutil.copytree(src_path, dest_path)
                else:
                    # If user selected a single file instead of a folder
                    shutil.copy2(src_path, dest_path)
                
                success_count += 1

            except Exception as e:
                errors.append(f"ERROR copying {folder_name}: {str(e)}")

        # Final Report
        report = f"Imported {success_count} items successfully."
        if errors:
            report += "\n\nIssues encountered:\n" + "\n".join(errors)
            messagebox.showwarning("Import Complete with Issues", report)
        else:
            messagebox.showinfo("Success", report)
            # Optional: Clear selections after success
            self.deselect_all()

if __name__ == "__main__":
    app = AudiobookImporter()
    app.mainloop()
