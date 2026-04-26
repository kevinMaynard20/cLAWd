"""PyInstaller entry-point for the bundled cLAWd backend.

The backend in dev runs via `uvicorn main:app`; in the bundled .app PyInstaller
needs a regular Python script as the entry. We import the FastAPI app and
hand it to uvicorn programmatically.

Bound to 127.0.0.1:8000 (spec §7.6) — same as `scripts/dev.sh`. The Tauri
shell waits for this port before showing the window.
"""

from __future__ import annotations

import os
import sys


def _bootstrap_paths() -> None:
    """When PyInstaller wraps us into an executable, the working directory
    is the .app's `Contents/MacOS/`. Our backend uses content-addressed
    paths (storage/ under the repo root) and template/schema lookups that
    walk up to find `spec.md`. Re-anchor those to the user's data dir so
    the bundled app writes to ~/Library/Application Support/cLAWd/ instead
    of inside the .app bundle (which is read-only after Gatekeeper signing).
    """
    if hasattr(sys, "_MEIPASS"):
        # We're inside a PyInstaller bundle — point env vars at user dirs.
        from pathlib import Path

        data_root = Path.home() / "Library" / "Application Support" / "cLAWd"
        data_root.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("LAWSCHOOL_DB_PATH", str(data_root / "lawschool.db"))
        os.environ.setdefault(
            "LAWSCHOOL_MARKER_CACHE_DIR", str(data_root / "marker_raw")
        )
        os.environ.setdefault(
            "LAWSCHOOL_PYMUPDF_CACHE_DIR", str(data_root / "pymupdf_raw")
        )
        os.environ.setdefault(
            "LAWSCHOOL_CREDENTIALS_FILE", str(data_root / "credentials.enc")
        )


def main() -> None:
    _bootstrap_paths()

    import uvicorn

    from main import app  # noqa: F401  (importing exposes the FastAPI app)

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.environ.get("LAWSCHOOL_BACKEND_PORT", "8000")),
        log_level="info",
        # No --reload in production — we don't want the bundle restarting
        # itself when the user pokes around in the .app's Resources.
    )


if __name__ == "__main__":
    main()
