# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for eteq -- a one-file console executable.
#
#   pyinstaller eteq.spec      ->  dist/eteq.exe
#
# About the web asset
# -------------------
# The browser player lives in src/eteq/web/index.html and is a *data* file, so
# PyInstaller cannot find it by following imports; it is listed in `datas`
# below and bundled at the path "eteq/web/index.html".
#
# At runtime the HTTP server resolves it with importlib.resources
# (importlib.resources.files("eteq") / "web" / "index.html"). In a one-file
# build PyInstaller unpacks the bundle into a temporary directory exposed as
# sys._MEIPASS and puts it on sys.path, so "eteq/web/index.html" inside the
# bundle resolves exactly like the installed package's data file and no
# frozen-specific code path is required. Code that wants the directory
# directly can fall back to os.path.join(sys._MEIPASS, "eteq", "web").
#
# Because `datas` uses the destination "eteq/web", the mapping must stay in
# sync with [tool.setuptools.package-data] in pyproject.toml.

import os

# The entry script is packaging/entrypoint.py, not a module inside the package:
# PyInstaller runs the entry script as __main__ with no package context, which
# breaks the relative imports that eteq's modules use.
#
# The spec file sits at the repository root; SPECPATH is provided by PyInstaller.
ROOT = os.path.abspath(SPECPATH)
SRC = os.path.join(ROOT, 'src')

a = Analysis(
    [os.path.join(ROOT, 'packaging', 'entrypoint.py')],
    pathex=[SRC],
    binaries=[],
    datas=[
        (os.path.join(SRC, 'eteq', 'web', 'index.html'), 'eteq/web'),
    ],
    hiddenimports=['eteq'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Keep the one-file exe small: eteq is standard library only.
        'tkinter',
        'unittest',
        'pydoc_data',
        'lib2to3',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='eteq',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
