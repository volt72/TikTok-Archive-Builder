# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files, collect_dynamic_libs

block_cipher = None

ICON_PATH = 'tiktok_archive_icon_clean.ico'

hiddenimports = [
    'app_gui',
    'gui_helpers',
    'gui_resources',
    'html',
    'html.parser',
    'html.entities',
    'json',
    're',
    'time',
    'shutil',
    'subprocess',
    'urllib',
    'urllib.request',
    'zipfile',
    'pathlib',
    'tkinter',
    'tkinter.ttk',
    'tkinter.messagebox',
    'queue',
    'threading',
    'contextlib',
    'io',
    'webbrowser',
    'traceback',
    'playwright',
    'playwright.sync_api',
    'greenlet',
    'pyee',
]

for pkg in ['playwright', 'playwright._impl', 'pyee']:
    try:
        hiddenimports += collect_submodules(pkg)
    except Exception:
        pass

datas = []
binaries = []

for pkg in ['playwright', 'pyee']:
    try:
        datas += collect_data_files(pkg, excludes=[
            "**/.local-browsers/**",
            "driver/package/.local-browsers/**",
        ])
    except Exception:
        pass

for pkg in ['greenlet']:
    try:
        binaries += collect_dynamic_libs(pkg)
    except Exception:
        pass

a = Analysis(
    ['gui_integrated_output.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['playwright_runtime_hook.py'],
    excludes=[
        'playwright.async_api',
        'greenlet.tests',
        'setuptools',
        'pkg_resources',
        'distutils',
        'win32com',
        'pythoncom',
        'Pythonwin',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TikTokArchiveGUI',
    icon=ICON_PATH,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='TikTokArchiveGUI',
)
