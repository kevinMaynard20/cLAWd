"""PyInstaller entry-point for the bundled cLAWd backend.

The backend in dev runs via `uvicorn main:app`; in the bundled .app
PyInstaller needs a regular Python script as the entry. We import the
FastAPI app and hand it to uvicorn programmatically.

Bound to 127.0.0.1:8000 (spec §7.6) — same as `scripts/dev.sh`. The Tauri
shell waits for this port before showing the window.

Crash-loud: any exception during startup is caught, written to
`~/Library/Logs/cLAWd/backend.log`, AND printed to stderr unbuffered. The
Tauri sidecar otherwise eats the stdio because the bundle launches with
detached pipes — silent failures here used to look like "backend just
doesn't start" with zero diagnostic output.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path


# Force Python to flush every print/log line immediately. PyInstaller's
# onefile bootloader detaches stdio in some configurations; -u fixes it.
os.environ.setdefault("PYTHONUNBUFFERED", "1")


def _crash_log_path() -> Path:
    """Where startup failures land. Accessible via Console.app or `tail`
    even when the user can't see stderr."""
    log_dir = Path.home() / "Library" / "Logs" / "cLAWd"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "backend.log"


def _bootstrap_paths() -> None:
    """When PyInstaller wraps us into an executable, the working directory
    is the .app's `Contents/MacOS/`. Our backend uses content-addressed
    paths (`storage/` under the repo root) and template/schema lookups that
    walk up to find `spec.md`. Re-anchor those to the user's data dir so
    the bundled app writes to `~/Library/Application Support/cLAWd/`
    instead of inside the .app bundle (which is read-only after Gatekeeper
    signing).
    """
    if hasattr(sys, "_MEIPASS"):
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

        # Force the encrypted-file credentials backend when bundled. The
        # macOS Keychain hangs indefinitely when a Tauri-spawned, ad-hoc-
        # signed PyInstaller binary tries to access it — the security
        # daemon waits on a TCC prompt that never resolves cleanly even
        # after the user types their password. The encrypted-file backend
        # uses HKDF-derived Fernet (spec §7.7.2) so the key material is
        # encrypted at rest in the user's data dir and never touches
        # security-server. Trade-off: on macOS the user gets no
        # system-keychain integration; for a single-user local app that's
        # acceptable.
        os.environ.setdefault("LAWSCHOOL_FORCE_FILE_BACKEND", "1")

        # PyInstaller unpacks bundled modules under `sys._MEIPASS`. When the
        # spec set `pathex=[apps/api/src, apps/api]`, PyInstaller copied
        # those modules into the bundle but they're searched at runtime via
        # the bundle's own path machinery — for CPython sub-imports like
        # `from main import app` to resolve, the unpack root must be on
        # sys.path. PyInstaller usually does this; we add it explicitly to
        # cover edge cases.
        if sys._MEIPASS not in sys.path:
            sys.path.insert(0, sys._MEIPASS)


def _log_startup_banner() -> None:
    """Write a 'starting' line both to stderr and to the crash log so the
    user can confirm the sidecar got past the bootloader. Any later silent
    failure is then traceable to a specific module import."""
    msg = (
        f"[cLAWd-backend] starting · "
        f"frozen={getattr(sys, 'frozen', False)} · "
        f"meipass={getattr(sys, '_MEIPASS', None)} · "
        f"executable={sys.executable}"
    )
    print(msg, flush=True)
    print(msg, file=sys.stderr, flush=True)
    try:
        with _crash_log_path().open("a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def _main_inner() -> None:
    _log_startup_banner()
    _bootstrap_paths()

    import uvicorn

    from main import app  # noqa: F401  (importing exposes the FastAPI app)

    print("[cLAWd-backend] uvicorn.run …", flush=True)
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.environ.get("LAWSCHOOL_BACKEND_PORT", "8000")),
        log_level="info",
        # No --reload in production — we don't want the bundle restarting
        # itself when the user pokes around in the .app's Resources.
    )


def main() -> None:
    """Wrapper that captures any pre-uvicorn exception so we don't fail
    silently. Without this, an ImportError in the bundle (a missing hidden
    import, an unset sys.path) would simply exit the process — Tauri sees
    "backend never came up" with no clue why."""
    try:
        _main_inner()
    except BaseException as exc:  # noqa: BLE001 — we want EVERYTHING here
        try:
            with _crash_log_path().open("a", encoding="utf-8") as f:
                f.write(
                    f"\n[cLAWd-backend] FATAL: {type(exc).__name__}: {exc}\n"
                )
                traceback.print_exc(file=f)
        except OSError:
            pass
        # Also dump to stderr in case Tauri is capturing it.
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        # Re-raise so the process exits with a non-zero code; Tauri's
        # `CommandEvent::Terminated` then fires and the shell can fail
        # loudly instead of waiting forever.
        raise


if __name__ == "__main__":
    main()
