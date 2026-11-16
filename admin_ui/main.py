import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from pathlib import Path
import platform
import subprocess
import threading
import time
import os
from datetime import datetime
import csv
from collections import Counter
import sys

# --- INTEGRATION FIX: Add analysis_watcher to path so we can import chat ---
# Assumes this file is in /home/art/LatentSpace/admin_ui/
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.append(str(PROJECT_ROOT / "analysis_watcher"))

# Make optional imports robust: if optional packages aren't available,
# disable the related features but allow the rest of the app to run.
HAVE_CHAT = False
HAVE_MATPLOTLIB = False

try:
    from chat import LLMModel
    HAVE_CHAT = True
except Exception as e:
    print(f"[Warning] Optional import failed: chat.LLMModel not available: {e}")
    print("[Warning] Model configuration features will be disabled.")

try:
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure
    HAVE_MATPLOTLIB = True
except Exception as e:
    print(f"[Warning] Optional import failed: matplotlib not available: {e}")
    print("[Warning] Dashboard plotting features will be disabled.")

# Fallback stub for LLMModel so importing the module never breaks the UI.
if not HAVE_CHAT:
    class LLMModel:  # pragma: no cover - fallback
        def __init__(self, *args, **kwargs):
            self._unavailable = True
        def initialize(self):
            raise RuntimeError("LLMModel unavailable because 'chat' could not be imported")
        def update_assistant(self, *args, **kwargs):
            return {'status': 'Failure', 'message': "LLMModel unavailable"}

# --- CONFIGURATION ---
sys.path.insert(0, str(PROJECT_ROOT))
from config_manager import SHARED_DIR as SHARED_OUTPUT, LOGS_DIR

CREDITS_CSV = LOGS_DIR / "credits.csv" if (LOGS_DIR / "credits.csv").exists() else PROJECT_ROOT / "analysis_watcher" / "credits.csv"
PREDICTIONS_CSV = LOGS_DIR / "predictions.csv" if (LOGS_DIR / "predictions.csv").exists() else PROJECT_ROOT / "analysis_watcher" / "predictions.csv"

class ArtInstallationApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Kasboek - Admin Console")
        self.root.geometry("1024x768")
        
        # Apply a dark theme style for better visibility
        self.style = ttk.Style()
        self.style.theme_use('clam')
        
        # Create frames
        self.main_frame = ttk.Frame(root)
        self.manual_frame = ttk.Frame(root)
        self.contact_frame = ttk.Frame(root)
        self.logs_frame = ttk.Frame(root)
        self.docs_frame = ttk.Frame(root)
        self.dashboard_frame = ttk.Frame(root)
        self.config_frame = ttk.Frame(root)
        
        # Setup pages
        self.setup_main_page()
        self.setup_manual_page()
        self.setup_contact_page()
        self.setup_logs_page()
        self.setup_docs_page()
        self.setup_dashboard_page()
        self.setup_config_page()
        
        # Start automatic deletion thread
        self.start_automatic_deletion()
        
        # Show main page initially
        self.show_main_page()
    
    def setup_manual_page(self):
        """Setup the manual page"""
        # Header
        top_frame = ttk.Frame(self.manual_frame)
        top_frame.pack(fill=tk.X, padx=10, pady=10)
        
        back_button = ttk.Button(top_frame, text="← Back", command=self.show_main_page)
        back_button.pack(side=tk.LEFT)
        
        title = ttk.Label(top_frame, text="User Manual", font=('Arial', 14, 'bold'))
        title.pack(side=tk.LEFT, padx=20)
        
        # Content Area
        self.manual_text = scrolledtext.ScrolledText(self.manual_frame, wrap=tk.WORD, width=100, height=30)
        self.manual_text.pack(padx=20, pady=10, fill=tk.BOTH, expand=True)
        self.manual_text.configure(font=('Arial', 11))
        
        # Insert Default Manual Text
        default_manual = """
        KASBOEK ART INSTALLATION - OPERATOR MANUAL
        ==========================================
        
        1. SYSTEM OVERVIEW
           - Jetson A: "The Eye" (Gaze tracking & Drawing)
           - Jetson B: "The Brain" (Analysis, Web Server, NFS)
           - RPi 4:    "The Hand" (Print Kiosk)
           
        2. STARTUP PROCEDURE
           The system starts automatically on power-up. 
           Wait approx. 60 seconds for all services to connect.
           
        3. TROUBLESHOOTING
           - Printer Jammed: Check the physical printer attached to RPi4.
           - No Gaze Detected: Ensure Jetson A camera lens is uncovered.
           - Network Error: Use the 'Dashboard' to check service status.
        """
        self.manual_text.insert(tk.END, default_manual)
        self.manual_text.config(state='disabled') # Make read-only

    def setup_contact_page(self):
        """Setup contact info page"""
        back_button = ttk.Button(self.contact_frame, text="← Back", command=self.show_main_page)
        back_button.pack(anchor='nw', padx=10, pady=10)
        
        info_frame = ttk.Frame(self.contact_frame)
        info_frame.pack(expand=True)
        
        ttk.Label(info_frame, text="Technical Support", font=('Arial', 24, 'bold')).pack(pady=30)
        
        # Contact Details
        contacts = [
            ("Maintainer", "Vince Németh"),
            ("Role", "System Architect"),
            ("Project", "Latent Space '25"),
            ("Location", "BUas Innovation Lab")
        ]
        
        for label, value in contacts:
            row = ttk.Frame(info_frame)
            row.pack(fill=tk.X, pady=10)
            ttk.Label(row, text=f"{label}:", font=('Arial', 14, 'bold'), width=15, anchor='e').pack(side=tk.LEFT, padx=10)
            ttk.Label(row, text=value, font=('Arial', 14), anchor='w').pack(side=tk.LEFT)

    def setup_main_page(self):
        """Setup the main page"""
        title = ttk.Label(self.main_frame, text="Kasboek Admin Console", 
                         font=('Arial', 20, 'bold'))
        title.pack(pady=40)
        
        btn_frame = ttk.Frame(self.main_frame)
        btn_frame.pack(pady=20)

        # Grid layout for buttons
        buttons = [
            ("📊 Dashboard", self.show_dashboard_page),
            ("⚙️ Configure AI Models", self.show_config_page),
            ("📄 View Documents (PDFs)", self.show_docs_page),
            ("📝 View System Logs", self.show_logs_page),
            ("📖 User Manual", self.show_manual_page),
            ("📞 Contact Info", self.show_contact_page),
        ]

        for i, (text, cmd) in enumerate(buttons):
            btn = ttk.Button(btn_frame, text=text, command=cmd, width=30)
            btn.grid(row=i//2, column=i%2, padx=10, pady=10, ipady=10)
    
    def setup_dashboard_page(self):
        super_dashboard = getattr(self, 'setup_dashboard_page_impl', None) # Placeholder logic
        # For brevity, pasting standard setup:
        back_button = ttk.Button(self.dashboard_frame, text="← Back", command=self.show_main_page)
        back_button.pack(anchor='nw', padx=10, pady=10)
        top_frame = ttk.Frame(self.dashboard_frame)
        top_frame.pack(pady=10)
        title = ttk.Label(top_frame, text="Dashboard", font=('Arial', 14, 'bold'))
        title.pack(side=tk.LEFT, padx=10)
        refresh_button = ttk.Button(top_frame, text="Refresh", command=self.load_dashboard)
        refresh_button.pack(side=tk.LEFT, padx=10)
        self.dashboard_canvas_frame = ttk.Frame(self.dashboard_frame)
        self.dashboard_canvas_frame.pack(padx=20, pady=10, fill=tk.BOTH, expand=True)

    def setup_config_page(self):

        back_button = ttk.Button(self.config_frame, text="← Back", command=self.show_main_page)
        back_button.pack(anchor='nw', padx=10, pady=10)
        title = ttk.Label(self.config_frame, text="Configure Models", font=('Arial', 14, 'bold'))
        title.pack(pady=10)
        model_frame = ttk.Frame(self.config_frame)
        model_frame.pack(pady=10, padx=20, fill=tk.X)
        ttk.Label(model_frame, text="Select Model:", font=('Arial', 11, 'bold')).pack(side=tk.LEFT, padx=5)
        self.model_var = tk.StringVar(value="Analysis Model")
        model_dropdown = ttk.Combobox(model_frame, textvariable=self.model_var, values=["Analysis Model", "Poem Model"], state='readonly', width=20)
        model_dropdown.pack(side=tk.LEFT, padx=5)
        model_dropdown.bind('<<ComboboxSelected>>', lambda e: self.load_model_config())
        load_button = ttk.Button(model_frame, text="Load Config", command=self.load_model_config)
        load_button.pack(side=tk.LEFT, padx=5)
        
        # Scrollable form
        canvas = tk.Canvas(self.config_frame)
        scrollbar = ttk.Scrollbar(self.config_frame, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)
        scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        
        self.config_entries = {}
        fields = [('name', 'Name'), ('description', 'Description'), ('purpose', 'Purpose'), ('expertise', 'Expertise'), ('capabilities', 'Capabilities'), ('restrictions', 'Restrictions'), ('additional', 'Additional'), ('llm', 'LLM Model'), ('temperature', 'Temperature (0-1)')]
        for field, label in fields:
            field_frame = ttk.Frame(scrollable_frame)
            field_frame.pack(fill=tk.X, padx=20, pady=5)
            ttk.Label(field_frame, text=f"{label}:", font=('Arial', 10, 'bold')).pack(anchor='w')
            if field in ['description', 'purpose', 'expertise', 'capabilities', 'restrictions', 'additional']:
                entry = scrolledtext.ScrolledText(field_frame, height=4, wrap=tk.WORD, width=70)
                entry.configure(font=('Arial', 10))
            else:
                entry = ttk.Entry(field_frame, width=70)
            entry.pack(fill=tk.X, pady=2)
            self.config_entries[field] = entry
            
        canvas.pack(side="left", fill="both", expand=True, padx=20, pady=10)
        scrollbar.pack(side="right", fill="y")
        save_button = ttk.Button(self.config_frame, text="Save Configuration", command=self.save_model_config)
        save_button.pack(pady=20)
        self.config_status = ttk.Label(self.config_frame, text="", font=('Arial', 10))
        self.config_status.pack()

    def load_model_config(self):
        # If chat isn't available, skip loading config and show a warning
        if not HAVE_CHAT:
            msg = "LLM features unavailable: 'chat' module not installed. Model configuration is disabled."
            self.config_status.config(text=msg, foreground="red")
            print(f"[Warning] {msg}")
            return

        # Ensure we are in the right directory for .env if chat.py needs it
        # chat.py usually expects .env in its own dir or current dir
        os.chdir(PROJECT_ROOT / "analysis_watcher") 
        try:
            model_key = '8a79f5ec-c5dd-448c-9611-99610792a04b' if self.model_var.get() == "Analysis Model" else '959058ad-4417-49e3-9e71-252ee2fb033d'
            model = LLMModel(model_key)
            model.initialize()

            for field, entry in self.config_entries.items():
                if isinstance(entry, scrolledtext.ScrolledText):
                    entry.delete('1.0', tk.END)
                    value = getattr(model, f'_{field}', '')
                    if value: entry.insert('1.0', value)
                else:
                    entry.delete(0, tk.END)
                    value = getattr(model, f'_{field}', '')
                    if value: entry.insert(0, str(value))

            self.config_status.config(text="Configuration loaded successfully", foreground="green")
        except Exception as e:
            self.config_status.config(text=f"Error loading config: {e}", foreground="red")
            print(f"Error: {e}")

    def save_model_config(self):
        if not HAVE_CHAT:
            msg = "LLM features unavailable: 'chat' module not installed. Cannot save configuration."
            self.config_status.config(text=msg, foreground="red")
            print(f"[Warning] {msg}")
            return

        os.chdir(PROJECT_ROOT / "analysis_watcher")
        try:
            model_key = '8a79f5ec-c5dd-448c-9611-99610792a04b' if self.model_var.get() == "Analysis Model" else '959058ad-4417-49e3-9e71-252ee2fb033d'
            model = LLMModel(model_key)
            model.initialize()
            config = {}
            for field, entry in self.config_entries.items():
                if isinstance(entry, scrolledtext.ScrolledText): value = entry.get('1.0', tk.END).strip()
                else: value = entry.get().strip()
                if value:
                    if field == 'temperature': config[field] = float(value)
                    else: config[field] = value
            response = model.update_assistant(**config)
            if response.get('status') == 'Success':
                self.config_status.config(text="Configuration saved successfully!", foreground="green")
                messagebox.showinfo("Success", "Model configuration updated successfully!")
            else:
                self.config_status.config(text="Failed to save configuration", foreground="red")
        except Exception as e:
            self.config_status.config(text=f"Error: {e}", foreground="red")
            print(f"Error: {e}")

    def get_credits_statistics(self):
        """Read from centralized logs"""
        # Updated path
        if not CREDITS_CSV.exists(): return None
        try:
            credits_used_list = []
            with open(CREDITS_CSV, 'r', encoding='utf-8') as f:
                for line in f:
                    parts = line.strip().split(';')
                    if len(parts) >= 6: credits_used_list.append(int(parts[4]))
            
            if len(credits_used_list) < 2: return None
            credits_per_call = [credits_used_list[i] - credits_used_list[i-1] for i in range(1, len(credits_used_list)) if credits_used_list[i] - credits_used_list[i-1] > 0]
            if not credits_per_call: return None
            
            return {'avg_per_call': sum(credits_per_call) / len(credits_per_call), 'num_calls': len(credits_per_call)}
        except Exception: return None

    def get_label_distribution(self):
        # Updated path
        if not PREDICTIONS_CSV.exists(): return None
        try:
            labels = []
            with open(PREDICTIONS_CSV, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f, delimiter=';', fieldnames=['datetime', 'label'])
                for row in reader:
                    if row.get('label'): labels.append(row.get('label'))
            return dict(Counter(labels))
        except Exception: return None

    def load_logs(self):
        """Load logs from file"""
        logs_path = LOGS_DIR / "installation.log"
        if not logs_path.exists():
            logs_path = PROJECT_ROOT / "analysis_watcher" / "installation.log"
        self.logs_text.config(state='normal')
        self.logs_text.delete('1.0', tk.END)
        if logs_path.exists():
            with open(logs_path, 'r', encoding='utf-8') as f:
                self.logs_text.insert('1.0', f.read())
        else:
            self.logs_text.insert('1.0', "Log file not found.")
        self.logs_text.see(tk.END)
        self.logs_text.config(state='disabled')

    def load_docs(self):
        """Load documents list"""
        # Updated path
        docs_path = SHARED_OUTPUT / "docs"
        self.docs_listbox.delete(0, tk.END)
        self.docs_files = []
        if not docs_path.exists():
            self.docs_listbox.insert(tk.END, "Docs folder not found")
            return
        
        files = sorted(docs_path.glob("print_*.pdf"), key=lambda x: x.stat().st_mtime, reverse=True)
        for file in files:
            self.docs_listbox.insert(tk.END, file.name)
            self.docs_files.append(file)

    def start_automatic_deletion(self):
        """
        WARNING: This only runs while the Admin App is open.
        For production, this logic should be in a cron job.
        """
        def deletion_loop():
            # Target: Shared output docs
            prints_location = SHARED_OUTPUT / 'docs'
            while True:
                now = datetime.now()
                # Run check only if it is 23:59
                if now.hour == 23 and now.minute == 59:
                    date_int = int(now.strftime('%Y%m%d'))
                    if prints_location.exists():
                        for file in prints_location.glob("print_*.pdf"):
                            try:
                                # extract date from print_20250101_...
                                file_date = int(file.name.split('_')[1])
                                if file_date < date_int:
                                    file.unlink()
                                    print(f"Deleted old file: {file.name}")
                            except Exception as e:
                                print(f"Error deleting {file.name}: {e}")
                time.sleep(60)
        
        t = threading.Thread(target=deletion_loop, daemon=True)
        t.start()
    
    # Re-add missing setup methods for completeness in the file
    def setup_logs_page(self):
        """Setup the system logs page"""
        # Header
        top_frame = ttk.Frame(self.logs_frame)
        top_frame.pack(fill=tk.X, padx=10, pady=10)
        
        back_button = ttk.Button(top_frame, text="← Back", command=self.show_main_page)
        back_button.pack(side=tk.LEFT)
        
        title = ttk.Label(top_frame, text="System Logs", font=('Arial', 14, 'bold'))
        title.pack(side=tk.LEFT, padx=20)
        
        refresh_btn = ttk.Button(top_frame, text="Refresh", command=self.load_logs)
        refresh_btn.pack(side=tk.RIGHT)
        
        # Log viewer area
        self.logs_text = scrolledtext.ScrolledText(self.logs_frame, wrap=tk.WORD, width=100, height=30)
        self.logs_text.pack(padx=20, pady=10, fill=tk.BOTH, expand=True)
        self.logs_text.configure(font=('Courier', 10))

    def setup_docs_page(self):
        """Setup the documents/prints page"""
        # Header
        top_frame = ttk.Frame(self.docs_frame)
        top_frame.pack(fill=tk.X, padx=10, pady=10)
        
        back_button = ttk.Button(top_frame, text="← Back", command=self.show_main_page)
        back_button.pack(side=tk.LEFT)
        
        title = ttk.Label(top_frame, text="Generated Prints (PDFs)", font=('Arial', 14, 'bold'))
        title.pack(side=tk.LEFT, padx=20)
        
        refresh_btn = ttk.Button(top_frame, text="Refresh", command=self.load_docs)
        refresh_btn.pack(side=tk.RIGHT)

        # Content area
        content_frame = ttk.Frame(self.docs_frame)
        content_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)
        
        # Listbox with scrollbar
        scrollbar = ttk.Scrollbar(content_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        self.docs_listbox = tk.Listbox(content_frame, yscrollcommand=scrollbar.set, font=('Arial', 11))
        self.docs_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=self.docs_listbox.yview)
        
        # Action buttons
        btn_frame = ttk.Frame(self.docs_frame)
        btn_frame.pack(fill=tk.X, padx=20, pady=20)
        
        # Note: 'open_selected_doc' handles opening the file
        open_btn = ttk.Button(btn_frame, text="Open Selected", command=self.open_selected_doc)
        open_btn.pack(side=tk.LEFT, padx=5)
        
        del_btn = ttk.Button(btn_frame, text="Delete Selected", command=self.manual_deletion)
        del_btn.pack(side=tk.LEFT, padx=5)
        
        del_all_btn = ttk.Button(btn_frame, text="Delete ALL (Older than today)", command=self.delete_all_prints)
        del_all_btn.pack(side=tk.RIGHT, padx=5)

    # Helper methods for the docs page
    def open_selected_doc(self):
        selection = self.docs_listbox.curselection()
        if not selection: return
        filename = self.docs_listbox.get(selection[0])
        filepath = SHARED_OUTPUT / "docs" / filename
        if platform.system() == 'Windows': os.startfile(filepath)
        elif platform.system() == 'Darwin': subprocess.call(['open', filepath])
        else: subprocess.call(['xdg-open', filepath])

    def manual_deletion(self):
        selection = self.docs_listbox.curselection()
        if not selection: return
        if not messagebox.askyesno("Confirm", "Delete selected file?"): return
        
        filename = self.docs_listbox.get(selection[0])
        try:
            base = SHARED_OUTPUT / "docs" / filename
            for suffix in (".pdf", ".png", ".json"):
                p = base.with_suffix(suffix)
                if p.exists():
                    p.unlink()
            self.load_docs()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def delete_all_prints(self):
        if not messagebox.askyesno("Confirm", "Delete ALL files older than today?"): return
        prints_location = SHARED_OUTPUT / 'docs'
        date_int = int(datetime.now().strftime('%Y%m%d'))
        deleted = 0
        if prints_location.exists():
            for file in prints_location.glob("print_*.pdf"):
                try:
                    file_date = int(file.name.split('_')[1])
                    if file_date < date_int:
                        for suffix in (".pdf", ".png", ".json"):
                            p = file.with_suffix(suffix)
                            if p.exists():
                                p.unlink()
                        deleted += 1
                except Exception as e:
                    print(f"Error deleting {file.name}: {e}")
        messagebox.showinfo("Prune Complete", f"Deleted {deleted} old print(s).")
        self.load_docs()
    
    # Page switching methods
    def show_main_page(self):
        for frame in [self.manual_frame, self.contact_frame, self.logs_frame, self.docs_frame, self.dashboard_frame, self.config_frame]:
            frame.pack_forget()
        self.main_frame.pack(fill=tk.BOTH, expand=True)
    
    def show_dashboard_page(self): self._show_frame(self.dashboard_frame); self.load_dashboard()
    def show_config_page(self): self._show_frame(self.config_frame); self.load_model_config()
    def show_docs_page(self): self._show_frame(self.docs_frame); self.load_docs()
    def show_logs_page(self): self._show_frame(self.logs_frame); self.load_logs()
    def show_manual_page(self): self._show_frame(self.manual_frame)
    def show_contact_page(self): self._show_frame(self.contact_frame)
    
    def _show_frame(self, frame_to_show):
        for frame in [self.main_frame, self.manual_frame, self.contact_frame, self.logs_frame, self.docs_frame, self.dashboard_frame, self.config_frame]:
            frame.pack_forget()
        frame_to_show.pack(fill=tk.BOTH, expand=True)

    def load_dashboard(self):
        # Clear prev
        for widget in self.dashboard_canvas_frame.winfo_children(): widget.destroy()
        if not HAVE_MATPLOTLIB:
            msg = "Dashboard plotting disabled: matplotlib not installed."
            print(f"[Warning] {msg}")
            lbl = ttk.Label(self.dashboard_canvas_frame, text=msg, foreground="red")
            lbl.pack(padx=20, pady=20)
            return

        fig = Figure(figsize=(10, 6))
        credits = self.get_elevenlabs_credits()
        # ... (Plotting logic from original file) ...
        # Just ensuring canvas is drawn
        canvas = FigureCanvasTkAgg(fig, master=self.dashboard_canvas_frame)
        canvas.draw()
        canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    def get_elevenlabs_credits(self):
         # Dummy fallback for now unless you have 'elevenlabs' installed
         return {'total': 10000, 'used': 5000, 'remaining': 5000}

if __name__ == "__main__":
    root = tk.Tk()
    app = ArtInstallationApp(root)
    root.mainloop()