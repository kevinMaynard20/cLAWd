# PyInstaller spec for the cLAWd FastAPI backend.
#
# The spec is checked into git so build output is reproducible across
# machines; tweaks (hidden imports, datas paths) live here, not in the
# build script.
#
# We use `--onedir` (the default for spec files): PyInstaller produces a
# folder with the entry binary plus `_internal/` containing every Python
# package it found. Onedir launches faster than `--onefile` because there's
# no per-launch unpacking step — important for "feels like a native app."
#
# Hidden imports cover modules pulled in dynamically (FastAPI's auto-
# registered routes, sqlmodel's runtime metaclass discovery, anthropic's
# lazy SDK loader, pymupdf's compiled extensions).

import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

REPO_ROOT = os.path.abspath(os.path.dirname(SPECPATH) + "/..")  # noqa: F821 (PyInstaller injects SPECPATH)

# ---------------------------------------------------------------------------
# Hidden imports
# ---------------------------------------------------------------------------
# FastAPI/Starlette pull these in lazily depending on routes the app uses;
# PyInstaller's static analysis doesn't see them. Listed here explicitly so
# we don't get a missing-module ImportError at runtime in the bundled app.

HIDDEN_IMPORTS = [
    # FastAPI internals
    "uvicorn",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.middleware",
    "uvicorn.middleware.proxy_headers",
    # SQLModel / SQLAlchemy
    *collect_submodules("sqlmodel"),
    *collect_submodules("sqlalchemy.dialects.sqlite"),
    "sqlite_vec",
    # Anthropic SDK uses lazy module imports under `anthropic.types`.
    *collect_submodules("anthropic"),
    # PyMuPDF C extension is loaded by name.
    "pymupdf",
    # Our backend
    *collect_submodules("data"),
    *collect_submodules("primitives"),
    *collect_submodules("features"),
    *collect_submodules("routes"),
    *collect_submodules("costs"),
    *collect_submodules("credentials"),
    *collect_submodules("llm"),
]

# ---------------------------------------------------------------------------
# Data files
# ---------------------------------------------------------------------------
# Prompts (Handlebars templates) and JSON schemas live OUTSIDE the Python
# package tree so feature modules can re-load them at runtime. PyInstaller's
# default freezing only scoops up `.py`; we add the prompt + schema dirs as
# data so they ship inside the bundle.

DATAS = [
    (os.path.join(REPO_ROOT, "packages", "prompts"), "packages/prompts"),
    (os.path.join(REPO_ROOT, "packages", "schemas"), "packages/schemas"),
    (os.path.join(REPO_ROOT, "config"), "config"),
]
DATAS.extend(collect_data_files("anthropic"))
DATAS.extend(collect_data_files("sqlmodel"))

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

block_cipher = None

a = Analysis(  # noqa: F821 (PyInstaller injects this name)
    [os.path.join(REPO_ROOT, "scripts", "cLAWd_backend_entry.py")],
    pathex=[
        os.path.join(REPO_ROOT, "apps", "api"),
        os.path.join(REPO_ROOT, "apps", "api", "src"),
    ],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Heavy ML deps we don't ship in the bundled app — torch / Marker
        # are only used when explicitly installed in the dev venv. The
        # PyMuPDF fallback path doesn't need them and we don't want the
        # .app to balloon to 2 GB on a Mac without GPUs.
        "torch",
        "torchvision",
        "marker",
        "marker_pdf",
        "faster_whisper",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="cLAWd-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # keep stderr around so Tauri can capture sidecar logs
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,  # native-only — pin in build_python_bundle.sh if needed
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="cLAWd-backend",
)
