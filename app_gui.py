import contextlib
import ctypes
import io
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
import zipfile
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox
import base64
import tempfile

from gui_helpers import (
    QueueWriter,
    apply_app_icon,
    handle_from_profile_url,
    launch_chromium_browser,
    normalized_profile_url,
    safe_folder_name,
    set_windows_app_user_model_id,
)
from gui_resources import BUILD_ARCHIVE_CODE, IMPORT_COMMENTS_CODE


# Explicit optional imports so PyInstaller sees Playwright dependencies.
# The embedded archive builder imports Playwright dynamically, so without this
# PyInstaller may miss it.
try:
    from playwright.sync_api import sync_playwright as _playwright_sync_import_check
except Exception:
    _playwright_sync_import_check = None


if getattr(sys, "frozen", False):
    APP_ROOT = Path(sys.executable).resolve().parent
else:
    APP_ROOT = Path(__file__).resolve().parent

OUTPUT_ROOT = APP_ROOT / "output"
GLOBAL_CONFIG_PATH = APP_ROOT / "config.json"

YT_DLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
COMMENT_SCRAPER_ZIP_URL = "https://github.com/volt72/tiktok-comment-scrapper/archive/refs/heads/master.zip"
COMMENT_SCRAPER_DIR = APP_ROOT / "tiktok-comment-scrapper-master"





def handle_from_profile_url(url):
    url = str(url or "").strip()
    m = re.search(r"tiktok\.com/@([^/?#]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"@([A-Za-z0-9._-]+)", url)
    if m:
        return m.group(1)
    cleaned = url.strip().strip("@").strip()
    return cleaned or "unknown"


def normalized_profile_url(url):
    handle = handle_from_profile_url(url)
    if handle == "unknown":
        return ""
    return f"https://www.tiktok.com/@{handle}"


def safe_folder_name(name):
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "").strip())
    return name or "unknown"

def launch_chromium_browser(playwright, **kwargs):
    """Launch Chromium, preferring installed Chrome/Edge for smaller packaged builds."""
    errors = []
    for channel in ("chrome", "msedge"):
        try:
            return playwright.chromium.launch(channel=channel, **kwargs)
        except Exception as e:
            errors.append(f"{channel}: {e}")
    try:
        return playwright.chromium.launch(**kwargs)
    except Exception as e:
        errors.append(f"bundled chromium: {e}")
    raise RuntimeError("Could not launch Chrome/Edge/Chromium. " + " | ".join(errors[-3:]))

class QueueWriter(io.TextIOBase):
    def __init__(self, q):
        self.q = q
        self.buf = ""

    def write(self, s):
        if not s:
            return 0
        self.buf += str(s)
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.q.put(line)
        return len(s)

    def flush(self):
        if self.buf:
            self.q.put(self.buf)
            self.buf = ""

class App(tk.Tk):
    def __init__(self):
        set_windows_app_user_model_id()
        super().__init__()
        apply_app_icon(self)
        self.title("TikTok Archive Builder")
        self.geometry("1500x900")
        self.minsize(1400, 800)

        self.proc = None
        self.worker = None
        self.q = queue.Queue()
        self.current_project_dir = None

        self.profile_var = tk.StringVar()
        self.cookie_var = tk.StringVar(value="firefox")
        self.comments_enabled_var = tk.BooleanVar(value=True)
        self.comment_delay_var = tk.StringVar(value="2")
        self.refresh_links_var = tk.BooleanVar(value=True)
        self.refresh_metadata_var = tk.BooleanVar(value=False)
        self.profile_auto_refresh_var = tk.BooleanVar(value=False)
        self.profile_force_refresh_var = tk.BooleanVar(value=False)
        self.profile_max_age_var = tk.StringVar(value="24")
        self.dependency_action_var = tk.StringVar(value="All dependencies")
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_text_var = tk.StringVar(value="Idle")
        self.task_counter_var = tk.StringVar(value="")
        self.current_step_var = tk.StringVar(value="No task running")
        self.progress_running = False
        self.last_progress_percent = 0.0
        self.download_progress_current = 0
        self.download_progress_total = 0
        self.status_var = tk.StringVar(value="Ready. Hover over a button to see what it does.")

        self._build_ui()
        self.load_config()
        self.after(100, self._drain_log_queue)

    def add_hint(self, widget, text):
        widget.bind("<Enter>", lambda event: self.status_var.set(text))
        widget.bind("<Leave>", lambda event: self.status_var.set("Ready. Hover over a button to see what it does."))

    def make_button(self, parent, text, command, hint, **pack_kwargs):
        btn = ttk.Button(parent, text=text, command=command)
        btn.pack(**pack_kwargs)
        self.add_hint(btn, hint)
        return btn

    def apply_theme(self):
        self.configure(bg="#0f1117")
        self.option_add("*Font", ("Segoe UI", 9))

        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        bg = "#0f1117"
        sidebar = "#11151d"
        card = "#151a23"
        card_2 = "#1a202b"
        border = "#2a3140"
        text = "#f3f4f6"
        muted = "#a7adba"
        accent = "#ff2f62"
        accent_dark = "#7f1d35"

        style.configure(".", background=bg, foreground=text, fieldbackground=card, bordercolor=border, lightcolor=border, darkcolor=border)
        style.configure("TFrame", background=bg)
        style.configure("Sidebar.TFrame", background=sidebar)
        style.configure("Card.TFrame", background=card)
        style.configure("TLabel", background=bg, foreground=text)
        style.configure("Muted.TLabel", background=bg, foreground=muted)
        style.configure("Sidebar.TLabel", background=sidebar, foreground=text)
        style.configure("Brand.TLabel", background=sidebar, foreground=text, font=("Segoe UI", 13, "bold"))
        style.configure("BrandAccent.TLabel", background=sidebar, foreground=accent, font=("Segoe UI", 13, "bold"))
        style.configure("Section.TLabel", background=card, foreground=text, font=("Segoe UI", 13, "bold"))
        style.configure("Card.TLabelframe", background=card, foreground=text, bordercolor=border, relief="solid")
        style.configure("Card.TLabelframe.Label", background=card, foreground=text, font=("Segoe UI", 11, "bold"))
        style.configure("TEntry", fieldbackground="#0f131b", foreground=text, insertcolor=text, bordercolor=border, padding=8)
        style.map("TEntry", fieldbackground=[("focus", "#121824")], bordercolor=[("focus", accent)])
        style.configure("TCombobox", fieldbackground="#0f131b", background="#0f131b", foreground=text, arrowcolor=text, bordercolor=border, padding=6)
        style.map("TCombobox", fieldbackground=[("readonly", "#0f131b")], foreground=[("readonly", text)], bordercolor=[("focus", accent)])
        style.configure("TCheckbutton", background=card, foreground=text, focuscolor=card)
        style.map("TCheckbutton", background=[("active", card)], foreground=[("active", text)])

        style.configure("TButton", background=card_2, foreground=text, bordercolor=border, focusthickness=0, padding=(12, 8), relief="flat")
        style.map("TButton", background=[("active", "#242b38"), ("pressed", "#10141c")], foreground=[("active", text)])

        style.configure("Accent.TButton", background=accent, foreground="#ffffff", bordercolor=accent, padding=(14, 9), relief="flat", font=("Segoe UI", 9, "bold"))
        style.map("Accent.TButton", background=[("active", "#ff4775"), ("pressed", "#d91f4f")], foreground=[("active", "#ffffff")])

        style.configure("Danger.TButton", background=accent_dark, foreground="#ffffff", bordercolor=accent_dark, padding=(14, 9), relief="flat")
        style.map("Danger.TButton", background=[("active", "#9f2544"), ("pressed", "#5f1528")])

        style.configure("Horizontal.TProgressbar", troughcolor="#10151f", background=accent, bordercolor=border, lightcolor=accent, darkcolor=accent)

        return {
            "bg": bg,
            "sidebar": sidebar,
            "card": card,
            "card_2": card_2,
            "border": border,
            "text": text,
            "muted": muted,
            "accent": accent,
        }

    def make_nav_item(self, parent, text, active=False):
        bg = "#381827" if active else "#11151d"
        fg = "#ff4b75" if active else "#d7dbe3"
        item = tk.Label(
            parent,
            text=text,
            bg=bg,
            fg=fg,
            anchor="w",
            padx=18,
            pady=12,
            font=("Segoe UI", 10, "bold" if active else "normal")
        )
        item.pack(fill="x", padx=14, pady=4)
        return item

    def make_dark_button(self, parent, text, command, hint, accent=False, danger=False, **pack_kwargs):
        style_name = "Accent.TButton" if accent else "Danger.TButton" if danger else "TButton"
        btn = ttk.Button(parent, text=text, command=command, style=style_name)
        btn.pack(**pack_kwargs)
        self.add_hint(btn, hint)
        return btn

    def _build_ui(self):
        colors = self.apply_theme()

        root = ttk.Frame(self, style="TFrame")
        root.pack(fill="both", expand=True)

        sidebar = ttk.Frame(root, style="Sidebar.TFrame", width=230)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        brand = ttk.Frame(sidebar, style="Sidebar.TFrame")
        brand.pack(fill="x", padx=20, pady=(18, 20))

        tk.Label(
            brand,
            text="▣",
            bg=colors["sidebar"],
            fg="#ffffff",
            font=("Segoe UI", 34, "bold"),
            anchor="w"
        ).pack(anchor="w")

        ttk.Label(brand, text="TIKTOK ARCHIVE", style="Brand.TLabel").pack(anchor="w", pady=(4, 0))
        ttk.Label(brand, text="BUILDER", style="BrandAccent.TLabel").pack(anchor="w")

        self.make_nav_item(sidebar, "⌂   Dashboard", active=True)
        about_item = self.make_nav_item(sidebar, "ⓘ   About")
        about_item.bind("<Button-1>", lambda event: self.show_about())
        about_item.configure(cursor="hand2")

        status_card = tk.Frame(sidebar, bg="#151a23", highlightbackground="#2a3140", highlightthickness=1)
        status_card.pack(side="bottom", fill="x", padx=18, pady=18)
        tk.Label(status_card, text="●  Status", bg="#151a23", fg="#ff4b75", anchor="w", font=("Segoe UI", 9, "bold")).pack(fill="x", padx=14, pady=(12, 4))
        tk.Label(status_card, textvariable=self.status_var, bg="#151a23", fg="#f3f4f6", anchor="w", wraplength=180).pack(fill="x", padx=14, pady=(0, 12))

        main = ttk.Frame(root, style="TFrame")
        main.pack(side="left", fill="both", expand=True, padx=18, pady=18)

        top = ttk.LabelFrame(main, text="Archive Configuration", style="Card.TLabelframe", padding=14)
        top.pack(fill="x", pady=(0, 10))

        ttk.Label(top, text="Profile URL", style="TLabel").grid(row=0, column=0, sticky="w", pady=8)
        ttk.Entry(top, textvariable=self.profile_var).grid(row=0, column=1, columnspan=5, sticky="ew", padx=(18, 0), pady=8)

        ttk.Label(top, text="Cookies", style="TLabel").grid(row=1, column=0, sticky="w", pady=8)
        ttk.Combobox(top, textvariable=self.cookie_var, values=["firefox", "chrome", "edge"], width=24, state="readonly").grid(row=1, column=1, sticky="w", padx=(18, 0), pady=8)

        options_row_1 = ttk.Frame(top, style="Card.TFrame")
        options_row_1.grid(row=1, column=2, columnspan=4, sticky="w", padx=(22, 0), pady=8)

        ttk.Checkbutton(options_row_1, text="Refresh links", variable=self.refresh_links_var).pack(side="left", padx=(0, 28))
        ttk.Checkbutton(options_row_1, text="Refresh metadata", variable=self.refresh_metadata_var).pack(side="left", padx=(0, 28))
        ttk.Checkbutton(options_row_1, text="Comments enabled", variable=self.comments_enabled_var).pack(side="left", padx=(0, 0))

        ttk.Label(top, text="Comment delay (seconds)", style="TLabel").grid(row=2, column=0, sticky="w", pady=8)
        ttk.Entry(top, textvariable=self.comment_delay_var, width=10).grid(row=2, column=1, sticky="w", padx=(18, 0), pady=8)

        options_row_2 = ttk.Frame(top, style="Card.TFrame")
        options_row_2.grid(row=2, column=2, columnspan=4, sticky="w", padx=(22, 0), pady=8)

        ttk.Checkbutton(options_row_2, text="Profile auto refresh", variable=self.profile_auto_refresh_var).pack(side="left", padx=(0, 28))
        ttk.Checkbutton(options_row_2, text="Force profile refresh", variable=self.profile_force_refresh_var).pack(side="left", padx=(0, 28))
        ttk.Label(options_row_2, text="Profile max age (hrs)", style="TLabel").pack(side="left", padx=(0, 8))
        ttk.Entry(options_row_2, textvariable=self.profile_max_age_var, width=8).pack(side="left")

        ttk.Label(top, text="Archive folder", style="TLabel").grid(row=3, column=0, sticky="w", pady=8)
        self.project_label_var = tk.StringVar(value="output\\unknown")

        archive_display = tk.Label(
            top,
            textvariable=self.project_label_var,
            bg="#0f131b",
            fg="#a7adba",
            anchor="w",
            padx=10,
            pady=8,
            relief="solid",
            bd=1,
            highlightthickness=1,
            highlightbackground="#2a3140"
        )
        archive_display.grid(row=3, column=1, columnspan=5, sticky="ew", padx=(18, 0), pady=8)
        archive_display.bind("<Button-1>", lambda event: self.open_project_folder())
        archive_display.configure(cursor="hand2")
        self.add_hint(archive_display, "Shows the active output folder. Click to open it.")

        top.columnconfigure(0, weight=0)
        top.columnconfigure(1, weight=1)
        top.columnconfigure(2, weight=0)
        top.columnconfigure(3, weight=0)
        top.columnconfigure(4, weight=0)
        top.columnconfigure(5, weight=0)

        dep = ttk.LabelFrame(main, text="Dependencies", style="Card.TLabelframe", padding=14)
        dep.pack(fill="x", pady=(0, 10))

        ttk.Label(dep, text="Action", style="TLabel").pack(side="left", padx=(0, 12))
        dep_dropdown = ttk.Combobox(
            dep,
            textvariable=self.dependency_action_var,
            values=["All dependencies", "yt-dlp.exe", "Comment scraper", "Check status"],
            width=30,
            state="readonly"
        )
        dep_dropdown.pack(side="left", padx=5)
        self.add_hint(dep_dropdown, "Choose which dependency action to run.")

        dep_btn = ttk.Button(dep, text="▶  Run Dependency Action", command=self.run_dependency_action, style="Accent.TButton")
        dep_btn.pack(side="left", padx=14)
        self.add_hint(dep_btn, "Runs the selected dependency action, such as downloading yt-dlp or the comment scraper.")

        buttons = ttk.Frame(main, style="TFrame")
        buttons.pack(fill="x", pady=(0, 10))

        toolbar_buttons = [
            (
                "⚡  Run Full Update",
                self.run_full_pipeline,
                "Accent.TButton",
                "Runs the full update: refresh links, add new videos, scrape/import comments, and rebuild HTML."
            ),
            (
                "↻  Check New Videos",
                self.check_new_videos,
                "TButton",
                "Checks the profile for new or removed videos, updates links, fetches missing metadata, and downloads new videos."
            ),
            (
                "💬  Update Comments",
                self.update_comments,
                "TButton",
                "Re-checks comments for each video, merges any new comments into existing JSON, then imports and rebuilds the archive."
            ),
            (
                "▣  Build Archive Only",
                lambda: self.run_embedded_step("Build Archive Only", self.run_build_archive_only_embedded),
                "TButton",
                "Rebuilds the archive HTML using existing metadata, videos, profile data, and comments."
            ),
            (
                "☁  Import Comments",
                lambda: self.run_embedded_step("Import Comments", self.run_import_comments_embedded),
                "TButton",
                "Imports scraped comment JSON files from import_comments into the archive comment folder."
            ),
            (
                "✓  Sanity Check",
                self.run_sanity_check,
                "TButton",
                "Compares links.txt against archive_out/index.html and local media folders, then prints the results to the log."
            ),
            (
                "▱  Open Archive",
                self.open_archive,
                "TButton",
                "Opens archive_out/index.html for this profile in your browser."
            ),
            (
                "■  Stop",
                self.stop_process,
                "Danger.TButton",
                "Stops the currently running process."
            ),
        ]

        for i, (label, command, style_name, hint) in enumerate(toolbar_buttons):
            btn = ttk.Button(buttons, text=label, command=command, style=style_name)
            btn.grid(row=0, column=i, sticky="ew", padx=5)
            self.add_hint(btn, hint)
            buttons.columnconfigure(i, weight=1, uniform="toolbar")

        progress_frame = ttk.LabelFrame(main, text="Progress", style="Card.TLabelframe", padding=10)
        progress_frame.pack(fill="x", pady=(0, 10))

        top_progress_row = ttk.Frame(progress_frame, style="Card.TFrame")
        top_progress_row.pack(fill="x")
        ttk.Label(top_progress_row, textvariable=self.current_step_var, style="TLabel").pack(side="left", anchor="w")
        ttk.Label(top_progress_row, textvariable=self.task_counter_var, style="Muted.TLabel").pack(side="right", anchor="e")
        progress_row = ttk.Frame(progress_frame, style="Card.TFrame")
        progress_row.pack(fill="x", pady=(6, 2))

        self.progress_bar = ttk.Progressbar(
            progress_row,
            mode="determinate",
            maximum=100,
            variable=self.progress_var
        )
        self.progress_bar.pack(side="left", fill="x", expand=True)

        self.progress_percent_var = tk.StringVar(value="0%")
        ttk.Label(progress_row, textvariable=self.progress_percent_var, style="TLabel", width=5).pack(side="left", padx=(10, 0))
        ttk.Label(progress_frame, textvariable=self.progress_text_var, style="Muted.TLabel").pack(anchor="w")

        log_frame = ttk.LabelFrame(main, text="Live Log", style="Card.TLabelframe", padding=14)
        log_frame.pack(fill="both", expand=True)

        log_tools = ttk.Frame(log_frame, style="Card.TFrame")
        log_tools.pack(fill="x", pady=(0, 8))
        ttk.Label(log_tools, text="Terminal output", style="Muted.TLabel").pack(side="left")
        clear_btn = ttk.Button(log_tools, text="Clear Log", command=lambda: self.clear_log())
        clear_btn.pack(side="right")
        self.add_hint(clear_btn, "Clears the visible live log text.")

        self.log = tk.Text(
            log_frame,
            wrap="word",
            state="disabled",
            height=22,
            bg="#0b0f16",
            fg="#d7dbe3",
            insertbackground="#f3f4f6",
            selectbackground="#ff2f62",
            relief="flat",
            padx=12,
            pady=12,
            font=("Consolas", 9)
        )
        self.log.pack(side="left", fill="both", expand=True)

        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        scroll.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=scroll.set)


    def project_dir_for_current_profile(self):
        handle = safe_folder_name(handle_from_profile_url(self.profile_var.get()))
        return OUTPUT_ROOT / handle

    def ensure_project_dir(self):
        project_dir = self.project_dir_for_current_profile()
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "import_comments").mkdir(exist_ok=True)
        self.current_project_dir = project_dir
        self.project_label_var.set(f"output\\{project_dir.name}")
        return project_dir

    def start_progress(self, title, percent=0):
        def ui():
            self.progress_running = True
            self.last_progress_percent = float(percent)
            self.download_progress_current = 0
            self.download_progress_total = 0
            self.current_step_var.set(title)
            self.progress_var.set(percent)
            self.progress_percent_var.set(f"{int(percent)}%")
            self.progress_text_var.set(f"{int(percent)}% - Running...")
            self.task_counter_var.set("")
        self.after(0, ui)

    def set_progress(self, percent, text=None):
        percent = max(0, min(100, float(percent)))
        def ui():
            self.last_progress_percent = percent
            self.progress_var.set(percent)
            self.progress_percent_var.set(f"{int(percent)}%")
            if text is not None:
                self.progress_text_var.set(f"{int(percent)}% - {text}")
            self.task_counter_var.set("")
        self.after(0, ui)

    def stop_progress(self, text="Done", percent=100):
        percent = max(0, min(100, float(percent)))
        def ui():
            self.progress_running = False
            self.last_progress_percent = percent
            self.download_progress_current = 0
            self.download_progress_total = 0
            self.progress_var.set(percent)
            self.progress_percent_var.set(f"{int(percent)}%")
            self.progress_text_var.set(f"{int(percent)}% - {text}")
            self.task_counter_var.set("")
        self.after(0, ui)

    def set_progress_text(self, text):
        def ui():
            current = int(float(self.progress_var.get()))
            self.progress_text_var.set(f"{current}% - {text}")
        self.after(0, ui)

    def set_task_status(self, text):
        def ui():
            self.task_counter_var.set(text or "")
        self.after(0, ui)

    def set_progress_from_log(self, percent, text=None, counter=None, step=None, allow_decrease=False):
        percent = max(0, min(100, float(percent)))
        if not allow_decrease:
            percent = max(float(self.last_progress_percent), percent)
        self.last_progress_percent = percent
        self.progress_var.set(percent)
        self.progress_percent_var.set(f"{int(percent)}%")
        if step:
            self.current_step_var.set(step)
        if counter is not None:
            self.task_counter_var.set(counter)
        if text:
            self.progress_text_var.set(f"{int(percent)}% - {text[:160]}")

    def update_progress_from_log_line(self, line_text):
        text = str(line_text or "").strip()
        if not text:
            return False

        entry = re.search(r"Preparing HTML entry\s+(\d+)\/(\d+)", text, re.I)
        if entry:
            cur = int(entry.group(1))
            total = max(1, int(entry.group(2)))
            pct = 55 + (cur / total) * 37
            self.set_progress_from_log(pct, f"Preparing HTML entries {cur}/{total}", f"Entry {cur}/{total}", "Preparing HTML")
            return True

        dl = re.search(r"Downloading video\s+(\d+)\/(\d+)", text, re.I)
        if dl:
            cur = int(dl.group(1))
            total = max(1, int(dl.group(2)))
            self.download_progress_current = cur
            self.download_progress_total = total
            pct = 30 + ((cur - 1) / total) * 16
            self.set_progress_from_log(pct, f"Starting media download {cur}/{total}", f"Download {cur}/{total}", "Downloading Media")
            return True

        comment = re.search(r"\[(\d+)\/(\d+)\]\s+(?:Scraping|Checking) comments", text, re.I)
        if comment:
            cur = int(comment.group(1))
            total = max(1, int(comment.group(2)))
            pct = 10 + (cur / total) * 70
            self.set_progress_from_log(pct, f"Comments {cur}/{total}", f"Comment {cur}/{total}", "Updating Comments")
            return True

        milestones = [
            ("Checking links from:", 5, "Checking profile links", "Checking Links"),
            ("No link changes found.", 10, "Links checked", "Checking Links"),
            ("Updated links.txt", 10, "Links updated", "Checking Links"),
            ("Reusing existing metadata", 14, "Metadata ready", "Metadata"),
            ("Fetching metadata/thumbnails", 16, "Fetching metadata/thumbnails", "Metadata"),
            ("Using cached profile.json", 24, "Profile ready", "Profile"),
            ("Saved fresh profile.json.", 24, "Profile refreshed", "Profile"),
            ("Checking root folder for existing videos", 28, "Checking local media", "Local Media"),
            ("Downloading ", 30, "Downloading missing media", "Downloading Media"),
            ("Refreshing root and archive video lists", 46, "Refreshing media lists", "Media Lists"),
            ("Video/audio lists refreshed.", 49, "Media lists refreshed", "Media Lists"),
            ("Preparing HTML entries for", 52, "Preparing HTML entries", "Preparing HTML"),
            ("Indexing downloaded comment images once", 53, "Indexing comment images", "Preparing HTML"),
            ("Indexed ", 55, "Comment images indexed", "Preparing HTML"),
            ("No missing video links found.", 94, "Missing media list cleared", "Finalizing"),
            ("Wrote ", 94, "Missing media list written", "Finalizing"),
            ("Built ", 98, "Archive HTML written", "Finalizing"),
        ]
        for needle, pct, label, step in milestones:
            if needle in text:
                if needle == "Downloading " and "missing videos" not in text and "missing media" not in text:
                    continue
                if needle == "Wrote " and "missing video link" not in text:
                    continue
                self.set_progress_from_log(pct, label, step=step)
                return True
        return False

    def mapped_ytdlp_progress(self, raw_pct):
        current_step = self.current_step_var.get().lower()
        raw_pct = max(0, min(100, float(raw_pct)))

        if "downloading media" in current_step and self.download_progress_total:
            total = max(1, int(self.download_progress_total))
            cur = max(1, min(total, int(self.download_progress_current or 1)))
            phase_start = 30.0
            phase_span = 16.0
            return phase_start + (((cur - 1) + (raw_pct / 100.0)) / total) * phase_span

        if "checking links" in current_step:
            return 5.0 + (raw_pct / 100.0) * 5.0

        if "metadata" in current_step:
            return 16.0 + (raw_pct / 100.0) * 8.0

        if "profile" in current_step:
            return 24.0

        if "preparing html" in current_step:
            return max(float(self.last_progress_percent), 52.0)

        return None

    def show_about(self):
        messagebox.showinfo(
            "About TikTok Archive Builder",
            "TikTok Archive Builder\n\n"
            "A local archive tool for TikTok profiles.\n\n"
            "Credits / external tools:\n"
            "yt-dlp:\n"
            "https://github.com/yt-dlp/yt-dlp\n\n"
            "TikTok Comment Scraper:\n"
            "https://github.com/volt72/tiktok-comment-scrapper"
        )

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def log_line(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _drain_log_queue(self):
        try:
            while True:
                line = self.q.get_nowait()
                self.log_line(line)

                if self.progress_running and line and not line.startswith("==="):
                    line_text = str(line)
                    handled_progress = self.update_progress_from_log_line(line_text)

                    # Task count lines look like [12/989] or Downloading video 3/35.
                    cm = re.search(r"\[(\d+)\/(\d+)\]", line_text) or re.search(r"(?:Downloading video|Scraping comments|Checking comments)\s+(\d+)\/(\d+)", line_text, re.I)
                    if cm:
                        self.task_counter_var.set(f"Task {cm.group(1)}/{cm.group(2)}")

                    # yt-dlp progress lines look like:
                    # [download]  42.3% of ...
                    m = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%", line_text)
                    if m:
                        pct = float(m.group(1))
                        mapped = self.mapped_ytdlp_progress(pct)
                        if mapped is not None:
                            self.set_progress_from_log(mapped, f"yt-dlp current file {pct:.1f}%")
                        elif not handled_progress:
                            self.progress_text_var.set(line_text[:160])
                    elif not handled_progress:
                        self.progress_text_var.set(line_text[:160])
        except queue.Empty:
            pass
        self.after(100, self._drain_log_queue)

    def default_config(self):
        return {
            "profile_url": self.profile_var.get().strip(),
            "refresh": {
                "links": bool(self.refresh_links_var.get()),
                "metadata": bool(self.refresh_metadata_var.get()),
                "profile": True
            },
            "download": {
                "use_cookies": self.cookie_var.get().strip() or "firefox",
                "format": "mp4"
            },
            "comments": {
                "enabled": bool(self.comments_enabled_var.get()),
                "delay_seconds": int(float(self.comment_delay_var.get() or "2"))
            },
            "profile": {
                "auto_refresh": bool(self.profile_auto_refresh_var.get()),
                "max_age_hours": float(self.profile_max_age_var.get() or "24"),
                "force_refresh": bool(self.profile_force_refresh_var.get())
            }
        }

    def apply_config_to_ui(self, cfg):
        self.profile_var.set(cfg.get("profile_url", ""))
        self.cookie_var.set(cfg.get("download", {}).get("use_cookies", "firefox"))
        self.comments_enabled_var.set(bool(cfg.get("comments", {}).get("enabled", True)))
        self.comment_delay_var.set(str(cfg.get("comments", {}).get("delay_seconds", 2)))
        self.refresh_links_var.set(bool(cfg.get("refresh", {}).get("links", True)))
        self.refresh_metadata_var.set(bool(cfg.get("refresh", {}).get("metadata", False)))
        profile_cfg = cfg.get("profile", {})
        self.profile_auto_refresh_var.set(bool(profile_cfg.get("auto_refresh", False)))
        self.profile_force_refresh_var.set(bool(profile_cfg.get("force_refresh", False)))
        self.profile_max_age_var.set(str(profile_cfg.get("max_age_hours", 24)))
        self.project_label_var.set(f"Archive folder: output\\{safe_folder_name(handle_from_profile_url(self.profile_var.get()))}")

    def load_config(self):
        cfg = None
        if GLOBAL_CONFIG_PATH.exists():
            try:
                cfg = json.loads(GLOBAL_CONFIG_PATH.read_text(encoding="utf-8"))
            except Exception:
                cfg = None

        if cfg:
            handle = safe_folder_name(handle_from_profile_url(cfg.get("profile_url", "")))
            project_config = OUTPUT_ROOT / handle / "config.json"
            if project_config.exists():
                try:
                    cfg = json.loads(project_config.read_text(encoding="utf-8"))
                    self.log_line(f"Loaded existing project config: {project_config}")
                except Exception:
                    self.log_line("Could not read project config. Using root config.")
            else:
                self.log_line("Loaded root config.json")
        else:
            cfg = self.default_config()
            GLOBAL_CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            self.log_line("Created default root config.json")

        self.apply_config_to_ui(cfg)

    def save_config(self):
        try:
            project_dir = self.ensure_project_dir()
            cfg = self.default_config()
            cfg["profile_url"] = normalized_profile_url(cfg["profile_url"]) if cfg.get("profile_url") else ""

            GLOBAL_CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            (project_dir / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")

            self.profile_var.set(cfg["profile_url"])
            self.q.put(f"Saved config.json to root and {project_dir}")
            return project_dir
        except Exception as e:
            messagebox.showerror("Save Error", str(e))
            return None

    def run_dependency_action(self):
        choice = self.dependency_action_var.get()
        if choice == "All dependencies":
            self.download_dependencies()
        elif choice == "yt-dlp.exe":
            self.run_threaded_task(self.download_yt_dlp)
        elif choice == "Comment scraper":
            self.run_threaded_task(self.download_comment_scraper)
        elif choice == "Check status":
            self.check_dependencies()

    def download_file(self, url, destination):
        self.q.put(f"Downloading: {url}")
        self.q.put(f"Saving to: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(url, destination)
        self.q.put(f"Downloaded: {destination.name}")

    def download_yt_dlp(self):
        self.status_var.set("Downloading yt-dlp.exe")
        self.set_progress_text("Downloading yt-dlp.exe")
        self.download_file(YT_DLP_URL, APP_ROOT / "yt-dlp.exe")
        self.status_var.set("Downloaded yt-dlp.exe")

    def download_comment_scraper(self):
        self.status_var.set("Downloading comment scraper")
        self.set_progress_text("Downloading comment scraper")
        temp_zip = APP_ROOT / "_comment_scraper_master.zip"
        temp_extract = APP_ROOT / "_comment_scraper_extract"
        if temp_zip.exists():
            temp_zip.unlink()
        if temp_extract.exists():
            shutil.rmtree(temp_extract)

        self.download_file(COMMENT_SCRAPER_ZIP_URL, temp_zip)
        self.q.put("Extracting comment scraper...")

        with zipfile.ZipFile(temp_zip, "r") as z:
            z.extractall(temp_extract)

        extracted_dirs = [p for p in temp_extract.iterdir() if p.is_dir()]
        if not extracted_dirs:
            raise RuntimeError("Could not find extracted comment scraper folder.")

        if COMMENT_SCRAPER_DIR.exists():
            shutil.rmtree(COMMENT_SCRAPER_DIR)

        shutil.move(str(extracted_dirs[0]), str(COMMENT_SCRAPER_DIR))
        temp_zip.unlink(missing_ok=True)
        shutil.rmtree(temp_extract, ignore_errors=True)

        self.q.put(f"Comment scraper ready: {COMMENT_SCRAPER_DIR}")
        self.status_var.set("Downloaded comment scraper")

    def download_dependencies(self):
        self.run_threaded_task(self._download_dependencies_worker)

    def _download_dependencies_worker(self):
        self.start_progress("Download Dependencies", 0)
        self.q.put("\n=== Download Dependencies ===")
        self.set_progress(10, "Downloading yt-dlp.exe")
        self.download_yt_dlp()
        self.set_progress(50, "Downloading comment scraper")
        self.download_comment_scraper()
        self.set_progress(95, "Finishing dependency setup")
        self.q.put("All dependencies downloaded.")
        self.status_var.set("Dependencies downloaded")
        self.stop_progress("Dependencies downloaded", 100)

    def check_dependencies(self):
        yt = APP_ROOT / "yt-dlp.exe"
        scraper = COMMENT_SCRAPER_DIR / "main.py"
        self.log_line("\n=== Dependency Status ===")
        self.log_line(f"yt-dlp.exe: {'FOUND' if yt.exists() else 'MISSING'}")
        self.log_line(f"comment scraper main.py: {'FOUND' if scraper.exists() else 'MISSING'}")
        self.log_line(f"Output root: {OUTPUT_ROOT}")

    def missing_dependencies(self, need_comments=False):
        missing = []
        if not (APP_ROOT / "yt-dlp.exe").exists():
            missing.append("yt-dlp.exe")
        if need_comments and not (COMMENT_SCRAPER_DIR / "main.py").exists():
            missing.append("tiktok-comment-scrapper-master\\main.py")
        return missing

    def block_if_missing_dependencies(self, need_comments=False):
        missing = self.missing_dependencies(need_comments=need_comments)
        if missing:
            msg = (
                "Missing required dependencies:\n\n"
                + "\n".join(f"- {item}" for item in missing)
                + "\n\nUse the Dependency dropdown and click Run Dependency Action first."
            )
            self.q.put(msg)
            messagebox.showerror("Missing Dependencies", msg)
            self.status_var.set("Missing dependencies")
            return True
        return False

    def env_for_commands(self):
        env = os.environ.copy()
        env["PATH"] = str(APP_ROOT) + os.pathsep + env.get("PATH", "")
        return env

    def hidden_subprocess_kwargs(self):
        kwargs = {}
        if sys.platform.startswith("win"):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
            kwargs["startupinfo"] = startupinfo
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        return kwargs

    def run_command(self, title, cmd, cwd=None):
        project_dir = cwd or self.ensure_project_dir()
        self.status_var.set(title)
        self.start_progress(title, 5 if "Build" in title or "Archive" in title else 0)
        self.q.put(f"\n=== {title} ===")
        self.q.put("Command: " + " ".join(map(str, cmd)))
        self.q.put(f"Working folder: {project_dir}")

        try:
            self.proc = subprocess.Popen(
                cmd,
                cwd=project_dir,
                env=self.env_for_commands(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                **self.hidden_subprocess_kwargs()
            )
            for line in self.proc.stdout:
                clean_line = line.rstrip()
                self.q.put(clean_line)
                if clean_line:
                    self.set_progress_text(clean_line[:160])
            code = self.proc.wait()
            self.proc = None
            if code != 0:
                self.q.put(f"FAILED: {title} exited with code {code}")
                self.status_var.set(f"Failed: {title}")
                self.stop_progress(f"Failed: {title}", 0)
                return False
            self.q.put(f"Done: {title}")
            self.status_var.set(f"Done: {title}")
            self.stop_progress(f"Done: {title}", 100)
            return True
        except Exception as e:
            self.proc = None
            self.q.put(f"ERROR: {e}")
            self.status_var.set("Error")
            self.stop_progress("Error", 0)
            return False

    def run_threaded_task(self, func):
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Busy", "A task is already running.")
            return

        def wrapper():
            try:
                func()
            except Exception:
                self.q.put(traceback.format_exc())
                self.status_var.set("Error")

        self.worker = threading.Thread(target=wrapper, daemon=True)
        self.worker.start()

    def patch_embedded_subprocess(self):
        import subprocess as _subprocess

        old_popen = _subprocess.Popen
        old_run = _subprocess.run

        def apply_hidden_kwargs(kwargs):
            if sys.platform.startswith("win"):
                if "startupinfo" not in kwargs:
                    startupinfo = _subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= _subprocess.STARTF_USESHOWWINDOW
                    startupinfo.wShowWindow = 0
                    kwargs["startupinfo"] = startupinfo
                kwargs["creationflags"] = kwargs.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
            return kwargs

        def hidden_popen(*args, **kwargs):
            kwargs = apply_hidden_kwargs(kwargs)
            return old_popen(*args, **kwargs)

        def logged_run(*args, **kwargs):
            # If the embedded builder already provided stdout/stderr, preserve that behavior.
            # Otherwise stream output live so the GUI log/progress bar updates while yt-dlp runs.
            has_custom_output = ("stdout" in kwargs) or ("stderr" in kwargs) or ("capture_output" in kwargs)
            check = kwargs.pop("check", False)

            kwargs = apply_hidden_kwargs(kwargs)

            if has_custom_output:
                result = old_run(*args, check=False, **kwargs)
            else:
                popen_kwargs = dict(kwargs)
                popen_kwargs["stdout"] = _subprocess.PIPE
                popen_kwargs["stderr"] = _subprocess.STDOUT
                popen_kwargs["stdin"] = _subprocess.DEVNULL
                popen_kwargs["text"] = True
                popen_kwargs["encoding"] = "utf-8"
                popen_kwargs["errors"] = "replace"

                proc = old_popen(*args, **popen_kwargs)
                output_lines = []

                if proc.stdout:
                    for line in proc.stdout:
                        clean = line.rstrip()
                        output_lines.append(clean)
                        if clean:
                            print(clean)

                returncode = proc.wait()

                result = _subprocess.CompletedProcess(
                    args[0] if args else kwargs.get("args"),
                    returncode,
                    stdout="\n".join(output_lines),
                    stderr=None
                )

            if check and result.returncode != 0:
                raise _subprocess.CalledProcessError(
                    result.returncode,
                    args[0] if args else kwargs.get("args"),
                    output=getattr(result, "stdout", None),
                    stderr=getattr(result, "stderr", None)
                )

            return result

        _subprocess.Popen = hidden_popen
        _subprocess.run = logged_run
        return _subprocess, old_popen, old_run

    def run_embedded_code(self, title, code, fake_filename):
        project_dir = self.ensure_project_dir()
        self.status_var.set(title)
        self.start_progress(title, 5 if "Build" in title or "Archive" in title else 0)
        self.q.put(f"\n=== {title} ===")
        self.q.put(f"Working folder: {project_dir}")
        writer = QueueWriter(self.q)
        old_cwd = Path.cwd()
        old_path = os.environ.get("PATH", "")
        patched_subprocess = None
        old_popen = None
        old_run = None
        try:
            os.chdir(project_dir)
            os.environ["PATH"] = str(APP_ROOT) + os.pathsep + old_path

            patched_subprocess, old_popen, old_run = self.patch_embedded_subprocess()

            env = {"__name__": "__main__", "__file__": str(project_dir / fake_filename)}
            env["input"] = lambda *args, **kwargs: ""
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                try:
                    exec(compile(code, str(project_dir / fake_filename), "exec"), env, env)
                except SystemExit as e:
                    if e.code not in (0, None):
                        raise
            writer.flush()
            self.q.put(f"Done: {title}")
            self.status_var.set(f"Done: {title}")
            self.stop_progress(f"Done: {title}", 100)
            return True
        except Exception:
            writer.flush()
            self.q.put(traceback.format_exc())
            self.status_var.set(f"Failed: {title}")
            self.stop_progress(f"Failed: {title}", 0)
            return False
        finally:
            if patched_subprocess is not None:
                patched_subprocess.Popen = old_popen
                patched_subprocess.run = old_run
            os.environ["PATH"] = old_path
            os.chdir(old_cwd)

    def run_build_archive_embedded(self):
        if self.block_if_missing_dependencies(need_comments=False):
            return False
        self.save_config()
        return self.run_embedded_code("Build Archive", BUILD_ARCHIVE_CODE, "_embedded_build_archive.py")

    def run_build_archive_only_embedded(self):
        if self.block_if_missing_dependencies(need_comments=False):
            return False
        self.save_config()
        old_mode = os.environ.get("TIKTOK_ARCHIVE_BUILD_ONLY")
        os.environ["TIKTOK_ARCHIVE_BUILD_ONLY"] = "1"
        try:
            return self.run_embedded_code("Build Archive Only", BUILD_ARCHIVE_CODE, "_embedded_build_archive.py")
        finally:
            if old_mode is None:
                os.environ.pop("TIKTOK_ARCHIVE_BUILD_ONLY", None)
            else:
                os.environ["TIKTOK_ARCHIVE_BUILD_ONLY"] = old_mode

    def run_import_comments_embedded(self):
        self.save_config()
        return self.run_embedded_code("Import Comments", IMPORT_COMMENTS_CODE, "_embedded_import_comments.py")

    def run_embedded_step(self, title, func):
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Busy", "A task is already running.")
            return
        self.worker = threading.Thread(target=func, daemon=True)
        self.worker.start()

    def download_initial_videos(self):
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Busy", "A task is already running.")
            return
        if self.block_if_missing_dependencies(need_comments=False):
            return
        self.save_config()
        self.worker = threading.Thread(target=self._download_initial_videos_worker, daemon=True)
        self.worker.start()

    def _download_initial_videos_worker(self):
        project_dir = self.ensure_project_dir()
        cfg = json.loads((project_dir / "config.json").read_text(encoding="utf-8"))
        url = cfg.get("profile_url", "")
        browser = cfg.get("download", {}).get("use_cookies", "firefox")

        if not url:
            self.q.put("profile_url missing in config.json")
            return

        with open(project_dir / "links.txt", "w", encoding="utf-8") as f:
            self.q.put("Creating links.txt...")
            subprocess.run(
                ["yt-dlp", url, "--cookies-from-browser", browser, "--flat-playlist", "--print", "%(webpage_url)s"],
                cwd=project_dir,
                env=self.env_for_commands(),
                stdout=f,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
                **self.hidden_subprocess_kwargs()
            )

        self.run_command(
            "Download Initial Videos",
            ["yt-dlp.exe", url, "--cookies-from-browser", browser, "-f", "mp4/b", "-S", "codec:h264,ext:mp4", "-o", "%(title)s [%(id)s].%(ext)s", "--no-overwrites"],
            cwd=project_dir
        )

    def run_comment_scraper(self):
        if self.block_if_missing_dependencies(need_comments=True):
            return False

        project_dir = self.ensure_project_dir()
        scraper = COMMENT_SCRAPER_DIR / "main.py"

        links = project_dir / "links.txt"
        if not links.exists():
            self.q.put("links.txt missing. Build archive or download initial videos first.")
            return False

        ids = []
        for line in links.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = re.search(r"/video/(\d+)", line)
            if m:
                ids.append(m.group(1))

        (project_dir / "ids.txt").write_text("\n".join(ids) + "\n", encoding="utf-8")
        self.q.put(f"Created ids.txt with {len(ids)} IDs")

        (project_dir / "import_comments").mkdir(exist_ok=True)

        try:
            cfg = json.loads((project_dir / "config.json").read_text(encoding="utf-8"))
            delay = int(float(cfg.get("comments", {}).get("delay_seconds", 2)))
        except Exception:
            delay = 2

        total = len(ids)
        for i, vid in enumerate(ids, start=1):
            if total:
                pct = 40 + ((i - 1) / total) * 30
                self.set_progress(pct, f"Scraping comments {i}/{total}")
                self.set_task_status(f"Comment {i}/{total}")
            self.q.put(f"[{i}/{len(ids)}] Scraping comments for {vid}")
            ok = self.run_command(
                "Scrape Comment",
                ["python", str(scraper), "--aweme_id=" + vid, "--output=import_comments"],
                cwd=project_dir
            )
            if not ok:
                return False
            time.sleep(delay)

        self.set_progress(70, "Comment scraping complete")
        return True

    def check_new_videos(self):
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Busy", "A task is already running.")
            return

        if self.block_if_missing_dependencies(need_comments=False):
            return

        self.save_config()
        self.worker = threading.Thread(target=self._check_new_videos_worker, daemon=True)
        self.worker.start()

    def _check_new_videos_worker(self):
        self.q.put("\n=== Check For New Videos Started ===")
        self.start_progress("Check For New Videos", 0)

        # The embedded builder already contains the logic to:
        # - refresh links
        # - compare IDs
        # - write deletedvids.txt
        # - fetch missing metadata only
        # - download missing videos
        if not self.run_build_archive_embedded():
            self.stop_progress("Check failed", 0)
            return

        self.q.put("=== Check For New Videos Complete ===")
        self.current_step_var.set("Check for new videos complete")
        self.stop_progress("Check complete", 100)

    def video_ids_from_links_file(self, links_path):
        ids = []
        if not links_path.exists():
            return ids
        for line in links_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = re.search(r"/(?:video|photo)/(\d+)", line) or re.search(r"(?:video_id|item_id)=(\d+)", line)
            if m:
                vid = m.group(1)
                if vid not in ids:
                    ids.append(vid)
        return ids

    def link_map_from_links_file(self, links_path):
        links = {}
        if not links_path.exists():
            return links
        for line in links_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = re.search(r"/(?:video|photo)/(\d+)", line) or re.search(r"(?:video_id|item_id)=(\d+)", line)
            if m:
                links[m.group(1)] = line.strip()
        return links

    def read_comment_payload(self, path):
        if not path.exists():
            return {"id": path.stem, "comments": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(data, list):
                return {"id": path.stem, "comments": data}
            if isinstance(data, dict):
                comments = data.get("comments", [])
                if isinstance(comments, list):
                    return {"id": data.get("id", path.stem), "comments": comments}
        except Exception:
            pass
        return {"id": path.stem, "comments": []}

    def comment_identity(self, comment):
        if not isinstance(comment, dict):
            return str(comment)

        # Prefer stable IDs if the scraper provides them.
        for key in [
            "cid", "comment_id", "id", "reply_id", "aweme_id",
            "create_time", "time"
        ]:
            value = comment.get(key)
            if value:
                user = (
                    comment.get("user")
                    or comment.get("username")
                    or comment.get("nickname")
                    or comment.get("unique_id")
                    or ""
                )
                text = comment.get("text") or comment.get("comment") or ""
                return f"{key}:{value}|{user}|{text}"

        # Fallback for scrapers that only provide basic fields.
        user = (
            comment.get("user")
            or comment.get("username")
            or comment.get("nickname")
            or comment.get("unique_id")
            or ""
        )
        text = comment.get("text") or comment.get("comment") or ""
        image = comment.get("image") or comment.get("image_url") or ""
        return f"{user}|{text}|{image}"

    def merge_comment_lists(self, old_comments, new_comments):
        merged = []
        seen = set()

        for comment in old_comments + new_comments:
            key = self.comment_identity(comment)
            if key not in seen:
                merged.append(comment)
                seen.add(key)

        return merged

    def merge_comment_file(self, vid, new_file, import_dir, archive_comments_dir):
        import_file = import_dir / f"{vid}.json"
        archive_file = archive_comments_dir / f"{vid}.json"

        existing_comments = []

        archive_payload = self.read_comment_payload(archive_file)
        existing_comments.extend(archive_payload.get("comments", []))

        import_payload = self.read_comment_payload(import_file)
        existing_comments.extend(import_payload.get("comments", []))

        new_payload = self.read_comment_payload(new_file)
        new_comments = new_payload.get("comments", [])

        merged_comments = self.merge_comment_lists(existing_comments, new_comments)

        merged_payload = {
            "id": vid,
            "comments": merged_comments
        }

        import_file.write_text(
            json.dumps(merged_payload, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        return len(existing_comments), len(new_comments), len(merged_comments)


    def collect_comment_ids(self, comment_list):
        ids = set()
        if not isinstance(comment_list, list):
            return ids

        for comment in comment_list:
            if not isinstance(comment, dict):
                continue

            # Your comment JSON uses comment_id for both top-level comments and replies.
            cid = comment.get("comment_id") or comment.get("cid") or comment.get("id") or comment.get("reply_id")
            if cid:
                ids.add(str(cid))

            replies = comment.get("replies") or comment.get("reply_comment") or comment.get("reply_list") or []
            if isinstance(replies, list):
                ids.update(self.collect_comment_ids(replies))

        return ids

    def existing_comment_ids_for_video(self, vid, archive_comments_dir, import_dir):
        ids = set()
        for folder in (archive_comments_dir, import_dir):
            path = folder / f"{vid}.json"
            if not path.exists():
                continue

            payload = self.read_comment_payload(path)
            ids.update(self.collect_comment_ids(payload.get("comments", [])))

        return ids

    def find_scraped_comment_file(self, folder, vid):
        direct = folder / f"{vid}.json"
        if direct.exists():
            return direct

        matches = list(folder.glob(f"*{vid}*.json"))
        if matches:
            return matches[0]

        json_files = list(folder.glob("*.json"))
        if len(json_files) == 1:
            return json_files[0]

        return None

    def update_comments(self):
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Busy", "A task is already running.")
            return

        if self.block_if_missing_dependencies(need_comments=True):
            return

        self.save_config()
        self.worker = threading.Thread(target=self._update_comments_worker, daemon=True)
        self.worker.start()

    def _update_comments_worker(self):
        self.q.put("\n=== Update Comments Started ===")
        self.start_progress("Update Comments", 0)

        project_dir = self.ensure_project_dir()
        links_path = project_dir / "links.txt"

        if not links_path.exists():
            self.q.put("links.txt was not found. Run Check New Videos or Build Archive first.")
            self.stop_progress("Missing links.txt", 0)
            return

        ids = self.video_ids_from_links_file(links_path)
        if not ids:
            self.q.put("No video IDs found in links.txt.")
            self.stop_progress("No video IDs found", 0)
            return

        link_map = self.link_map_from_links_file(links_path)
        updated_comment_links = []

        scraper = COMMENT_SCRAPER_DIR / "main.py"
        if not scraper.exists():
            self.q.put("Comment scraper missing. Use Dependency dropdown to download it.")
            self.stop_progress("Missing comment scraper", 0)
            return

        archive_comments_dir = project_dir / "archive_out" / "comments"
        import_dir = project_dir / "import_comments"
        temp_dir = project_dir / "_comment_update_temp"

        archive_comments_dir.mkdir(parents=True, exist_ok=True)
        import_dir.mkdir(parents=True, exist_ok=True)

        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        temp_dir.mkdir(parents=True, exist_ok=True)

        try:
            cfg = json.loads((project_dir / "config.json").read_text(encoding="utf-8"))
            delay = int(float(cfg.get("comments", {}).get("delay_seconds", 2)))
        except Exception:
            delay = 2

        self.q.put(f"Total videos in links.txt: {len(ids)}")
        self.q.put("Re-checking comments for each video and merging only new ones.")

        total_added = 0
        total_skipped = 0
        total = len(ids)

        for i, vid in enumerate(ids, start=1):
            pct = 5 + ((i - 1) / max(total, 1)) * 75
            self.set_progress(pct, f"Checking comments {i}/{total}")
            self.set_task_status(f"Comment {i}/{total}")
            self.q.put(f"[{i}/{total}] Checking comments for {vid}")

            existing_ids = self.existing_comment_ids_for_video(vid, archive_comments_dir, import_dir)
            if existing_ids:
                total_skipped += 1
                self.q.put(f"Skipping {vid}: found {len(existing_ids)} existing comment IDs in JSON")
                continue

            per_video_temp = temp_dir / vid
            if per_video_temp.exists():
                shutil.rmtree(per_video_temp)
            per_video_temp.mkdir(parents=True, exist_ok=True)

            ok = self.run_command(
                "Scrape Comments For Merge",
                ["python", str(scraper), "--aweme_id=" + vid, "--output=" + str(per_video_temp)],
                cwd=project_dir
            )
            if not ok:
                self.stop_progress("Comment update failed", pct)
                return

            scraped_file = self.find_scraped_comment_file(per_video_temp, vid)
            if not scraped_file:
                self.q.put(f"No scraped comment file found for {vid}. Skipping merge for this video.")
                time.sleep(delay)
                continue

            before_count, scraped_count, merged_count = self.merge_comment_file(
                vid,
                scraped_file,
                import_dir,
                archive_comments_dir
            )

            added = max(0, merged_count - before_count)
            total_added += added

            if added > 0:
                updated_comment_links.append(link_map.get(vid, f"https://www.tiktok.com/video/{vid}"))

            self.q.put(
                f"Merged {vid}: existing={before_count}, scraped={scraped_count}, "
                f"merged={merged_count}, new_added={added}"
            )

            time.sleep(delay)

        shutil.rmtree(temp_dir, ignore_errors=True)

        self.set_progress(85, "Importing merged comments")
        if not self.run_import_comments_embedded():
            self.stop_progress("Comment import failed", 85)
            return

        self.set_progress(95, "Rebuilding archive with merged comments")
        if not self.run_build_archive_embedded():
            self.stop_progress("Archive rebuild failed", 95)
            return

        self.q.put(f"=== Update Comments Complete. Skipped existing videos: {total_skipped}. New comments added: {total_added} ===")
        if updated_comment_links:
            self.q.put("Videos with new comments added:")
            for link in updated_comment_links:
                self.q.put(f"- {link}")
        else:
            self.q.put("No videos had new comments added.")
        self.stop_progress("Comments updated", 100)

    def run_full_pipeline(self):
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Busy", "A task is already running.")
            return

        comments_needed = bool(self.comments_enabled_var.get())
        if self.block_if_missing_dependencies(need_comments=comments_needed):
            return

        self.save_config()
        self.worker = threading.Thread(target=self._pipeline_worker, daemon=True)
        self.worker.start()

    def _pipeline_worker(self):
        self.q.put("\n=== Full Update Pipeline Started ===")
        self.start_progress("Full Update Pipeline", 0)

        self.set_progress(5, "Starting archive build")
        if not self.run_build_archive_embedded():
            self.stop_progress("Pipeline failed", 0)
            return
        self.set_progress(35, "Archive build complete")

        project_dir = self.ensure_project_dir()
        try:
            cfg = json.loads((project_dir / "config.json").read_text(encoding="utf-8"))
            comments_enabled = bool(cfg.get("comments", {}).get("enabled", True))
        except Exception:
            comments_enabled = True

        if comments_enabled:
            self.set_progress(40, "Scraping comments")
            if not self.run_comment_scraper():
                self.stop_progress("Pipeline failed", 40)
                return

            self.set_progress(70, "Importing comments")
            if not self.run_import_comments_embedded():
                self.stop_progress("Pipeline failed", 70)
                return

            self.set_progress(85, "Rebuilding archive with comments")
            if not self.run_build_archive_embedded():
                self.stop_progress("Pipeline failed", 85)
                return
        else:
            self.q.put("Comments disabled in config. Skipping comment scraping/import.")
            self.set_progress(80, "Comments skipped")

        self.q.put("=== Full Update Pipeline Complete ===")
        self.status_var.set("Full update complete")
        self.current_step_var.set("Full update complete")
        self.stop_progress("Full update complete", 100)

    def stop_process(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.q.put("Stop requested. Terminated current process.")
            self.status_var.set("Stopped")
            self.stop_progress("Stopped", 0)
        else:
            self.q.put("No running process to stop.")


    def profile_url_from_config_file(self, project_dir):
        """Return a TikTok profile URL from the saved config or from links.txt."""
        for cfg_path in [project_dir / "config.json", Path("config.json")]:
            try:
                if cfg_path.exists():
                    cfg = json.loads(cfg_path.read_text(encoding="utf-8", errors="ignore"))
                    raw = str(cfg.get("profile_url", "") or cfg.get("profile", {}).get("url", "")).strip()
                    if raw:
                        if raw.startswith("http"):
                            return raw
                        handle = raw.lstrip("@").strip()
                        if handle:
                            return f"https://www.tiktok.com/@{handle}"
            except Exception:
                pass

        links_path = project_dir / "links.txt"
        if links_path.exists():
            for line in links_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                m = re.search(r"https?://(?:www\.)?tiktok\.com/@([^/?#]+)", line)
                if m:
                    return f"https://www.tiktok.com/@{m.group(1)}"
        return ""

    def scrape_profile_ids_for_sanity(self, profile_url):
        """Use Playwright to collect visible post IDs from profile anchor hrefs.

        This is separate from links.txt so we can diagnose profile count mismatches.
        """
        ids = []
        links = {}
        if not profile_url:
            return ids, links, "No profile URL found in config or links.txt."

        try:
            from playwright.sync_api import sync_playwright
        except Exception as e:
            return ids, links, f"Playwright not available for profile sanity scrape: {e}"

        try:
            with sync_playwright() as p:
                browser = launch_chromium_browser(p, headless=True)
                context = browser.new_context(
                    viewport={"width": 1400, "height": 1200},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                )
                page = context.new_page()
                page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(5000)

                for sel in ['button:has-text("Not now")', 'button:has-text("Close")', '[aria-label="Close"]', 'button:has-text("Decline")']:
                    try:
                        loc = page.locator(sel)
                        if loc.count():
                            loc.first.click(timeout=1200)
                            page.wait_for_timeout(500)
                    except Exception:
                        pass

                stable_rounds = 0
                last_count = 0
                for _ in range(45):
                    hrefs = page.eval_on_selector_all("a[href]", "els => els.map(a => a.href)")
                    for href in hrefs:
                        m = re.search(r"/(?:video|photo)/(\d+)", href) or re.search(r"(?:video_id|item_id)=(\d+)", href)
                        if not m:
                            continue
                        vid = m.group(1)
                        if vid not in links:
                            links[vid] = href.split("?")[0]
                            ids.append(vid)
                    if len(ids) == last_count:
                        stable_rounds += 1
                    else:
                        stable_rounds = 0
                        last_count = len(ids)
                    if stable_rounds >= 6:
                        break
                    page.mouse.wheel(0, 3500)
                    page.wait_for_timeout(900)

                browser.close()
            return ids, links, ""
        except Exception as e:
            return ids, links, f"Profile sanity scrape failed: {e}"

    def run_sanity_check(self):
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Busy", "A task is already running.")
            return
        self.worker = threading.Thread(target=self.sanity_check_archive, daemon=True)
        self.worker.start()

    def sanity_check_archive(self):
        """Compare links.txt IDs against generated HTML/media folders and print a full report."""
        project_dir = self.ensure_project_dir()
        links_path = project_dir / "links.txt"
        index_path = project_dir / "archive_out" / "index.html"
        videos_dir = project_dir / "archive_out" / "videos"
        slideshows_dir = project_dir / "archive_out" / "slideshows"
        missing_path = project_dir / "missing_videos.txt"
        report_path = project_dir / "sanity_report.txt"

        self.q.put("\n=== Archive Sanity Check ===")

        if not links_path.exists():
            self.q.put("links.txt not found.")
            return

        raw_lines = [x.strip() for x in links_path.read_text(encoding="utf-8", errors="ignore").splitlines() if x.strip()]
        link_ids = self.video_ids_from_links_file(links_path)
        link_map = self.link_map_from_links_file(links_path)
        link_set = set(link_ids)

        # Compare against visible profile page IDs too. This explains cases where
        # profile.json says 992 but links.txt only has 989.
        profile_json_path = project_dir / "profile" / "profile.json"
        profile_report_ids_path = project_dir / "profile_scrape_ids.txt"
        profile_missing_links_path = project_dir / "profile_ids_not_in_links.txt"
        profile_video_count = ""
        profile_scrape_ids = []
        profile_scrape_links = {}
        profile_extra_ids = []

        if profile_json_path.exists():
            try:
                profile_json_data = json.loads(profile_json_path.read_text(encoding="utf-8", errors="ignore"))
                profile_video_count = profile_json_data.get("video_count", "")
            except Exception:
                profile_video_count = ""

        profile_url = self.profile_url_from_config_file(project_dir)
        self.q.put(f"Profile videos count from profile.json: {profile_video_count or '-'}")
        if profile_video_count not in ("", None):
            try:
                gap = int(str(profile_video_count).replace(",", "")) - len(link_ids)
                self.q.put(f"Profile count minus unique links.txt IDs: {gap}")
            except Exception:
                pass

        self.q.put("Scraping visible profile post IDs for sanity compare...")
        profile_scrape_ids, profile_scrape_links, profile_scrape_error = self.scrape_profile_ids_for_sanity(profile_url)
        if profile_scrape_error:
            self.q.put(profile_scrape_error)
            if profile_report_ids_path.exists():
                cached_lines = [x.strip() for x in profile_report_ids_path.read_text(encoding="utf-8", errors="ignore").splitlines() if x.strip()]
                for line in cached_lines:
                    m = re.search(r"(\d{8,})", line)
                    if m:
                        vid = m.group(1)
                        if vid not in profile_scrape_ids:
                            profile_scrape_ids.append(vid)
                            profile_scrape_links[vid] = line
                if profile_scrape_ids:
                    self.q.put(f"Using cached profile_scrape_ids.txt with {len(profile_scrape_ids)} IDs.")
        else:
            self.q.put(f"Visible profile IDs scraped from page: {len(profile_scrape_ids)}")

        if profile_scrape_ids:
            profile_report_lines = []
            for vid in profile_scrape_ids:
                profile_report_lines.append(f"{vid}  {profile_scrape_links.get(vid, '')}")
            profile_report_ids_path.write_text("\n".join(profile_report_lines) + "\n", encoding="utf-8")

            profile_extra_ids = [vid for vid in profile_scrape_ids if vid not in link_set]
            self.q.put(f"Profile scraped IDs not in links.txt: {len(profile_extra_ids)}")
            missing_lines = []
            for vid in profile_extra_ids:
                line = f"{vid}  {profile_scrape_links.get(vid, '')}"
                missing_lines.append(line)
                self.q.put(f"  PROFILE NOT IN LINKS: {line}")
            profile_missing_links_path.write_text("\n".join(missing_lines) + ("\n" if missing_lines else ""), encoding="utf-8")
            self.q.put(f"Wrote profile scrape IDs to: {profile_report_ids_path}")
            self.q.put(f"Wrote profile IDs not in links.txt to: {profile_missing_links_path}")

        duplicate_ids = []
        seen = set()
        for line in raw_lines:
            m = re.search(r"/(?:video|photo)/(\d+)", line) or re.search(r"(?:video_id|item_id)=(\d+)", line)
            if not m:
                continue
            vid = m.group(1)
            if vid in seen and vid not in duplicate_ids:
                duplicate_ids.append(vid)
            seen.add(vid)

        self.q.put(f"Profile link lines: {len(raw_lines)}")
        self.q.put(f"Unique profile IDs from links.txt: {len(link_ids)}")
        if duplicate_ids:
            self.q.put(f"Duplicate IDs in links.txt: {len(duplicate_ids)}")

        html_ids = set()
        if index_path.exists():
            text = index_path.read_text(encoding="utf-8", errors="ignore")
            html_ids.update(re.findall(r'data-vid="(\d{8,})"', text))
            html_ids.update(re.findall(r'id="comments-data-(\d{8,})"', text))
            html_ids.update(re.findall(r'videos/(\d{8,})\.', text))
            html_ids.update(re.findall(r'slideshows/(\d{8,})/', text))
            self.q.put(f"Unique IDs rendered in HTML: {len(html_ids)}")
        else:
            self.q.put("archive_out/index.html not found. Run Build Archive Only first if you want to compare against HTML.")

        local_video_ids = set()
        if videos_dir.exists():
            for f in videos_dir.iterdir():
                if f.is_file():
                    m = re.search(r"(\d{8,})", f.stem)
                    if m:
                        local_video_ids.add(m.group(1))

        slideshow_ids = set()
        if slideshows_dir.exists():
            for d in slideshows_dir.iterdir():
                if d.is_dir() and re.fullmatch(r"\d{8,}", d.name):
                    if any(x.is_file() for x in d.iterdir()):
                        slideshow_ids.add(d.name)

        local_media_ids = local_video_ids | slideshow_ids
        missing_local = sorted(link_set - local_media_ids)
        missing_html = sorted(link_set - html_ids) if html_ids else []
        local_not_profile = sorted(local_media_ids - link_set)
        html_not_profile = sorted(html_ids - link_set) if html_ids else []
        video_only = sorted(local_video_ids - slideshow_ids)
        slideshow_only = sorted(slideshow_ids - local_video_ids)
        both_media = sorted(local_video_ids & slideshow_ids)

        self.q.put(f"Local video IDs: {len(local_video_ids)}")
        self.q.put(f"Local slideshow IDs: {len(slideshow_ids)}")
        self.q.put(f"Total unique local media IDs: {len(local_media_ids)}")
        self.q.put(f"Video-only IDs: {len(video_only)}")
        self.q.put(f"Slideshow-only IDs: {len(slideshow_only)}")
        if both_media:
            self.q.put(f"IDs with both video and slideshow media: {len(both_media)}")

        self.q.put(f"Profile IDs with no local video/slideshow media: {len(missing_local)}")
        for vid in missing_local:
            self.q.put(f"  MISSING MEDIA: {vid}  {link_map.get(vid, '')}")

        if html_ids:
            self.q.put(f"Profile IDs missing from HTML: {len(missing_html)}")
            for vid in missing_html:
                self.q.put(f"  MISSING HTML: {vid}  {link_map.get(vid, '')}")
            self.q.put(f"Local media IDs not in links.txt: {len(local_not_profile)}")
            for vid in local_not_profile[:50]:
                self.q.put(f"  LOCAL EXTRA: {vid}")
            if len(local_not_profile) > 50:
                self.q.put(f"  ...and {len(local_not_profile)-50} more")
            self.q.put(f"HTML IDs not in links.txt: {len(html_not_profile)}")
            for vid in html_not_profile[:50]:
                self.q.put(f"  HTML EXTRA: {vid}")
            if len(html_not_profile) > 50:
                self.q.put(f"  ...and {len(html_not_profile)-50} more")

        missing_file_lines = []
        if missing_path.exists():
            missing_file_lines = [x.strip() for x in missing_path.read_text(encoding="utf-8", errors="ignore").splitlines() if x.strip()]
            self.q.put(f"missing_videos.txt entries: {len(missing_file_lines)}")
            for line in missing_file_lines:
                self.q.put(f"  MISSING FILE ENTRY: {line}")
        else:
            self.q.put("missing_videos.txt not found.")

        report_lines = []
        report_lines.append("Archive Sanity Check Report")
        report_lines.append(f"Project: {project_dir}")
        report_lines.append("")
        report_lines.append(f"Profile link lines: {len(raw_lines)}")
        report_lines.append(f"Unique links.txt IDs: {len(link_ids)}")
        report_lines.append(f"Profile videos count from profile.json: {profile_video_count or '-'}")
        report_lines.append(f"Visible profile IDs scraped from page: {len(profile_scrape_ids)}")
        report_lines.append(f"Profile scraped IDs not in links.txt: {len(profile_extra_ids)}")
        report_lines.append(f"Unique HTML IDs: {len(html_ids)}")
        report_lines.append(f"Local video IDs: {len(local_video_ids)}")
        report_lines.append(f"Local slideshow IDs: {len(slideshow_ids)}")
        report_lines.append(f"Total unique local media IDs: {len(local_media_ids)}")
        report_lines.append("")
        def section(title, ids, include_link=True):
            report_lines.append(title)
            report_lines.append("=" * len(title))
            if not ids:
                report_lines.append("None")
            for vid in ids:
                if include_link:
                    report_lines.append(f"{vid}  {link_map.get(vid, '')}")
                else:
                    report_lines.append(str(vid))
            report_lines.append("")
        section("Profile scraped IDs not in links.txt", profile_extra_ids, False)
        if profile_extra_ids:
            report_lines.append("Profile scraped links not in links.txt")
            report_lines.append("==================================")
            for vid in profile_extra_ids:
                report_lines.append(str(vid) + "  " + str(profile_scrape_links.get(vid, "")))
            report_lines.append("")
        section("Profile IDs with no local video/slideshow media", missing_local)
        section("Profile IDs missing from HTML", missing_html)
        section("Duplicate IDs in links.txt", duplicate_ids)
        section("Local media IDs not in links.txt", local_not_profile, False)
        section("HTML IDs not in links.txt", html_not_profile, False)
        section("Slideshow-only IDs", slideshow_only)
        section("Video-only IDs", video_only)
        if missing_file_lines:
            report_lines.append("missing_videos.txt raw entries")
            report_lines.append("==============================")
            report_lines.extend(missing_file_lines)
            report_lines.append("")
        report_path.write_text("\n".join(report_lines), encoding="utf-8")
        self.q.put(f"Full sanity report written to: {report_path}")
        self.q.put("=== Sanity Check Complete ===")

    def open_archive(self):
        project_dir = self.ensure_project_dir()
        html_path = project_dir / "archive_out" / "index.html"
        if html_path.exists():
            webbrowser.open(html_path.resolve().as_uri())
        else:
            messagebox.showwarning("Not found", f"Archive HTML was not found:\n{html_path}")

    def open_project_folder(self):
        project_dir = self.ensure_project_dir()
        project_dir.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["explorer", str(project_dir)])

if __name__ == "__main__":
    App().mainloop()


def main():
    app = App()
    app.mainloop()
