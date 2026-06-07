# -*- mode: python ; coding: utf-8 -*-

import os
import sys
import shutil

SPEC_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
SRC_DIR = os.path.join(SPEC_DIR, "src")
PROMPTS_DIR = os.path.join(SPEC_DIR, "prompts")

print(f"SPEC_DIR: {SPEC_DIR}")
print(f"SRC_DIR exists: {os.path.exists(SRC_DIR)}")
print(f"PROMPTS_DIR exists: {os.path.exists(PROMPTS_DIR)}")

block_cipher = None

a = Analysis(
    [os.path.join(SPEC_DIR, "run.py"),
     os.path.join(SPEC_DIR, "gateway_server.py")],
    pathex=[SRC_DIR],
    binaries=[],
    datas=[
        (PROMPTS_DIR, "prompts"),
    ],
    hiddenimports=[
        "json5",
        "openai",
        "requests",
        "dotenv",
        "lark_oapi",
        "fastembed",
        "typing_extensions",
        "aiosqlite",
        "anyio",
        "httpx",
        "pydantic",
        "pydantic_core",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "scipy",
    ],
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
    name="NanoGhost",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

if __name__ == '__main__':
    import shutil
    import os
    if os.path.exists('dist'):
        shutil.rmtree('dist')
    import PyInstaller.__main__
    PyInstaller.__main__.run(['build.spec', '--clean', '-y'])
