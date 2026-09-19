# -*- mode: python ; coding: utf-8 -*-

import os
import sys

SPEC_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
SRC_DIR = os.path.join(SPEC_DIR, "src")
PROMPTS_DIR = os.path.join(SPEC_DIR, "prompts")

print(f"SPEC_DIR: {SPEC_DIR}")
print(f"SRC_DIR exists: {os.path.exists(SRC_DIR)}")
print(f"PROMPTS_DIR exists: {os.path.exists(PROMPTS_DIR)}")

import certifi as _certifi_mod
_CA_BUNDLE = _certifi_mod.where()

block_cipher = None

a = Analysis(
    [os.path.join(SPEC_DIR, "src", "agent_core", "cli.py"),
     os.path.join(SPEC_DIR, "run.py"),
     os.path.join(SPEC_DIR, "gateway_server.py")],
    pathex=[SRC_DIR],
    binaries=[],
    datas=[
        (PROMPTS_DIR, "prompts"),
        (os.path.join(SPEC_DIR, ".env.example"), "."),
        (_CA_BUNDLE, "certifi"),
        (os.path.join(SPEC_DIR, "VERSION"), "."),
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
        "agent_core.mcp",
        "agent_core.mcp_client",
        "agent_core.mcp_client.config",
        "agent_core.mcp_client.manager",
        "agent_core.mcp_client.http_sse",
        "agent_core.mcp_client.stdio_client",
        "agent_core.mcp_client.manifest_cache",
        "agent_core.setup_wizard",
        "agent_core.version",
        "agent_core.update",
        "agent_core.scheduler",
        "agent_core.tool.builtins.tasks",
        "agent_core.channel.base",
        "agent_core.channel.registry",
        "agent_core.channel.endpoint",
        "agent_core.channel.directory",
        "agent_core.channel.feishu.channel",
        "agent_core.runtime.inbox",
        "agent_core.runtime.consumer",
        "agent_core.runtime.subagent_pool",
        "agent_core.runtime.watcher",
        "agent_core.router",
        "agent_core.sink",
        "agent_core.channel.admin",
        "agent_core.tool.builtins.send",
        "agent_core.tool.builtins.endpoints",
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


