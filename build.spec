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

import certifi as _certifi_mod
_CA_BUNDLE = _certifi_mod.where()

# Collect all Python system DLLs (Windows Store Python doesn't expose them to PyInstaller)
_PY_DLL_DIR = os.path.join(sys.base_prefix, "DLLs")
_BINARIES = []
if os.path.isdir(_PY_DLL_DIR):
    for _f in os.listdir(_PY_DLL_DIR):
        if _f.lower().endswith((".dll", ".pyd")):
            _BINARIES.append((os.path.join(_PY_DLL_DIR, _f), "."))

# Collect pycryptodome native modules (hook-Crypto.py sometimes misses them on Windows Store Python)
import glob as _glob
from PyInstaller.compat import EXTENSION_SUFFIXES as _EXT_SUFFIXES
_CRYPTO_DIR = os.path.join(SPEC_DIR, "venv", "Lib", "site-packages", "Crypto")
if os.path.isdir(_CRYPTO_DIR):
    for _root, _dirs, _files in os.walk(_CRYPTO_DIR):
        _rel = os.path.relpath(_root, _CRYPTO_DIR)
        _target = "Crypto" + (os.sep + _rel if _rel != "." else "")
        for _ext in _EXT_SUFFIXES:
            for _f in _glob.glob(os.path.join(_root, "*" + _ext)):
                _BINARIES.append((_f, _target))

block_cipher = None

a = Analysis(
    [os.path.join(SPEC_DIR, "src", "agent_core", "cli.py"),
     os.path.join(SPEC_DIR, "run.py"),
     os.path.join(SPEC_DIR, "gateway_server.py")],
    pathex=[SRC_DIR],
    binaries=_BINARIES,
    datas=[
        (PROMPTS_DIR, "prompts"),
        (os.path.join(SPEC_DIR, ".env.example"), "."),
        (_CA_BUNDLE, "certifi"),
    ],
    hiddenimports=[
        "json5",
        "openai",
        "requests",
        "dotenv",
        "lark_oapi",
        "fastembed",
        "typing_extensions",
        "anyio",
        "httpx",
        "pydantic",
        "pydantic_core",
        "certifi",
        "sqlite3",
        "_sqlite3",
        "pydantic_core._pydantic_core",
        "jiter",
        "jiter.jiter",
        "mmh3",
        "py_rust_stemmers",
        "py_rust_stemmers.py_rust_stemmers",
        "tokenizers",
        "tokenizers.tokenizers",
        "yaml",
        "yaml._yaml",
        "hf_xet",
        "hf_xet.hf_xet",
        "google._upb._message",
        "mcp",
        "mcp.client",
        "mcp.client.session",
        "mcp.client.sse",
        "mcp.client.stdio",
        "mcp.shared",
        "mcp.shared.message",
        "mcp.types",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[os.path.join(SPEC_DIR, "pyi_rth_crypto.py")],
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
    exclude_binaries=True,
    name="NanoGhost",
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
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="NanoGhost",
)


