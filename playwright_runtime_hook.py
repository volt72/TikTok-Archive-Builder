import os
import sys
from pathlib import Path

# In one-folder PyInstaller builds, bundled files live in _internal.
base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
browser_path = base / "playwright" / "driver" / "package" / ".local-browsers"

if browser_path.exists():
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_path)
