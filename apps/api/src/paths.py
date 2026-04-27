"""Centralized path resolution for the cLAWd backend.

Three execution contexts have to be supported:

1. **Dev** (``make dev`` / ``pnpm tauri:dev``): the source tree on disk, with
   ``spec.md`` at the repo root. Walking up from any source file finds it.
2. **Bundled .app** (``cLAWd.app``): PyInstaller's onefile bootloader unpacks
   the bundle to ``sys._MEIPASS`` (``/var/folders/.../T/_MEI*``). The .app
   itself is read-only after Gatekeeper signing, and ``Path.cwd()`` resolves
   to ``/`` (the root of the filesystem) — anything that previously fell back
   to ``Path.cwd() / "storage" / …`` ended up trying to write to ``/storage``,
   which 500'd the request.
3. **Tests**: env-var overrides (``LAWSCHOOL_DB_PATH``, ``LAWSCHOOL_UPLOADS_DIR``,
   etc.) take precedence; tests use temp dirs.

Before this module existed, every package re-implemented the walk-up-and-fall-
back-to-cwd dance. The fallback was wrong in the bundle, so:

- ``/system/health`` 500'd on ``mkdir(/storage)``.
- ``prompt_loader`` couldn't find Handlebars templates → every LLM call would
  ``FileNotFoundError`` on first prompt render.
- ``costs/pricing.py``, ``costs/emphasis_weights.py``, ``primitives/generate.py``
  couldn't find their TOML config → wrong defaults, silently.

This module centralizes the resolution so a fix in one place covers all of
them, and read-only resources (prompts/schemas/config) are cleanly separated
from writable storage (DB/uploads/caches/credentials).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    """True iff running inside a PyInstaller bundle.

    PyInstaller sets both ``sys.frozen`` and ``sys._MEIPASS`` (the unpack
    dir). We check both because some other freezers set only ``sys.frozen``.
    """
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def repo_root() -> Path:
    """Root for **read-only** resources bundled at build time.

    Returns:
    - Frozen: ``sys._MEIPASS`` (PyInstaller's unpacked bundle root). The
      spec includes ``packages/prompts``, ``packages/schemas``, and
      ``config/`` here.
    - Dev: the directory containing ``spec.md`` (walked up from this file).
    - Last resort: ``Path.cwd()`` — only reached if neither holds, and is
      then almost certainly wrong, but matches historical behavior so a
      misconfigured test still produces a discoverable error rather than a
      hidden one.
    """
    if is_frozen():
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    here = Path(__file__).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "spec.md").exists():
            return candidate
    return Path.cwd()


def user_data_dir() -> Path:
    """Per-user writable app data dir.

    macOS: ``~/Library/Application Support/cLAWd/``. Created on access.

    This is the safe place to put SQLite, uploads, caches, and the
    encrypted credentials file when running in the bundled .app. The .app
    itself is read-only after Gatekeeper signing, so we cannot write
    *inside* the bundle.
    """
    d = Path.home() / "Library" / "Application Support" / "cLAWd"
    d.mkdir(parents=True, exist_ok=True)
    return d


def storage_root() -> Path:
    """Root for **writable** state (DB, uploads, marker_raw, pymupdf_raw).

    - Frozen: the user data dir (``~/Library/Application Support/cLAWd/``).
    - Dev: ``<repo>/storage/``.

    Tests should not call this directly — they should set the per-resource
    env var (``LAWSCHOOL_DB_PATH``, etc.) so they get isolated temp dirs.
    """
    return user_data_dir() if is_frozen() else repo_root() / "storage"
