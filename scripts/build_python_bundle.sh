#!/usr/bin/env bash
# Bundle the FastAPI backend as a single-file executable for Tauri's
# `externalBin` slot.
#
# Output: apps/web/src-tauri/binaries/cLAWd-backend-<target-triple>
#
# `<target-triple>` is what Rust calls the host platform — Tauri's sidecar
# resolver picks the file matching the binary the shell is being built for.
# On Apple Silicon this is `aarch64-apple-darwin`; on Intel Macs it's
# `x86_64-apple-darwin`. We compute the right suffix below and rename the
# PyInstaller artefact in place so `cargo tauri build` picks it up.
#
# Why PyInstaller and not Nuitka or shiv:
# - We have a real venv + transitive ML deps (pymupdf, optional marker).
#   PyInstaller's "freeze the venv as-is" model is the lowest-risk path.
# - shiv produces a zipapp that requires Python on the host. Defeats the
#   "feels like a native app" goal — the user would have to install Python.
# - Nuitka compiles the source which would catch genuine bugs but adds
#   minutes to every build and historically misbehaves with C-extensions.
#
# Prereqs:
#   .venv must exist and have all backend deps installed:
#     python3 -m venv .venv && .venv/bin/pip install -e '.[dev]' pyinstaller

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ---------- preflight ----------

if [[ ! -x ".venv/bin/python" ]]; then
    echo "✗ .venv not found at $REPO_ROOT/.venv" >&2
    echo "  python3 -m venv .venv && .venv/bin/pip install -e '.[dev]' pyinstaller" >&2
    exit 1
fi

if ! .venv/bin/python -c "import PyInstaller" 2>/dev/null; then
    echo "✗ PyInstaller not in venv. Install with:" >&2
    echo "  .venv/bin/pip install pyinstaller" >&2
    exit 1
fi

# Resolve the Rust target triple. We trust the host detection rather than
# parsing rustc output so this works without Rust installed yet.
ARCH="$(uname -m)"
case "$ARCH" in
    arm64|aarch64) TARGET_TRIPLE="aarch64-apple-darwin" ;;
    x86_64) TARGET_TRIPLE="x86_64-apple-darwin" ;;
    *) echo "✗ unsupported architecture: $ARCH" >&2; exit 1 ;;
esac

echo "→ target triple: $TARGET_TRIPLE"

# ---------- bundle ----------

WORK_DIR="$REPO_ROOT/.build/pyinstaller"
DIST_DIR="$WORK_DIR/dist"
SPEC_FILE="$REPO_ROOT/scripts/cLAWd-backend.spec"

rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR"

echo "→ running PyInstaller (this can take a few minutes)…"
.venv/bin/pyinstaller \
    --noconfirm \
    --workpath "$WORK_DIR/build" \
    --distpath "$DIST_DIR" \
    "$SPEC_FILE"

# Spec is in --onefile mode → PyInstaller drops a single self-extracting
# executable at $DIST_DIR/cLAWd-backend (no `_internal/` directory).
# Tauri's externalBin resolver wants the file at:
#   apps/web/src-tauri/binaries/<name>-<target-triple>
# without a directory wrapper.
OUT_DIR="$REPO_ROOT/apps/web/src-tauri/binaries"
mkdir -p "$OUT_DIR"
rm -rf "$OUT_DIR/cLAWd-backend-$TARGET_TRIPLE"  # cleanup any stale onedir
SRC="$DIST_DIR/cLAWd-backend"
DST="$OUT_DIR/cLAWd-backend-$TARGET_TRIPLE"
if [[ ! -f "$SRC" ]]; then
    echo "✗ expected onefile binary at $SRC, didn't find one" >&2
    exit 1
fi
cp -p "$SRC" "$DST"
chmod +x "$DST"

echo "✓ sidecar at $DST"
echo "  size: $(du -h "$DST" | cut -f1)"
echo "  Tauri's externalBin = \"binaries/cLAWd-backend\" picks this up."
