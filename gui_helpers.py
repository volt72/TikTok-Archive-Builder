import base64
import ctypes
import io
import os
import re
import tempfile
from pathlib import Path

from gui_resources import EMBEDDED_ICON_B64

def get_embedded_icon_path():
    icon_file = Path(tempfile.gettempdir()) / "tiktok_archive_icon_clean.ico"
    if not icon_file.exists():
        icon_file.write_bytes(base64.b64decode(EMBEDDED_ICON_B64))
    return icon_file

def set_windows_app_user_model_id():
    if os.name != "nt":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "Volt7.TikTokArchive.Builder"
        )
    except Exception:
        pass

def apply_app_icon(window):
    icon_path = str(get_embedded_icon_path())
    try:
        window.iconbitmap(icon_path)
    except Exception:
        pass
    if os.name != "nt":
        return
    try:
        window.update_idletasks()
        hwnd = window.winfo_id()
        image_icon = 1
        lr_loadfromfile = 0x00000010
        lr_defaultsize = 0x00000040
        wm_seticon = 0x0080
        icon_small = 0
        icon_big = 1
        hicon = ctypes.windll.user32.LoadImageW(
            None,
            icon_path,
            image_icon,
            0,
            0,
            lr_loadfromfile | lr_defaultsize,
        )
        if hicon:
            ctypes.windll.user32.SendMessageW(hwnd, wm_seticon, icon_small, hicon)
            ctypes.windll.user32.SendMessageW(hwnd, wm_seticon, icon_big, hicon)
    except Exception:
        pass

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
