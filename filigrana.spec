# -*- mode: python ; coding: utf-8 -*-
# ─────────────────────────────────────────────────────────────
#  filigrana.spec  —  PyInstaller spec per PDF Sporca
#  Esegui con:  pyinstaller filigrana.spec
# ─────────────────────────────────────────────────────────────

import sys
from pathlib import Path

block_cipher = None

a = Analysis(
    ['filigrana.py'],
    pathex=[],
    binaries=[],
    datas=[
        # Includi i dati di tkinterdnd2 (libreria DnD)
        (str(Path(sys.exec_prefix) / 'Lib/site-packages/tkinterdnd2'), 'tkinterdnd2'),
    ],
    hiddenimports=[
        'tkinterdnd2',
        'PIL._tkinter_finder',
        'numpy',
        'numpy.core._methods',
        'numpy.lib.format',
        'pypdf',
        'fitz',
        'reportlab',
        'reportlab.pdfgen',
        'reportlab.lib.pagesizes',
        'reportlab.lib.utils',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='PDFSporca',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon='filigrana.ico',  # <- decommentare se hai un'icona .ico
)
