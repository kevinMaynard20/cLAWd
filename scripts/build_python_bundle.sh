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

# PyInstaller produces $DIST_DIR/cLAWd-backend (a directory with the binary
# inside, since we use `--onedir` in the spec for faster startup). Tauri
# wants a single file in `externalBin`, but it does support directories
# too via `binaries/<name>/...`. We move the whole onedir output to
# src-tauri/binaries/cLAWd-backend-<triple>/ and Tauri will pick the
# entry-point binary.
OUT_DIR="$REPO_ROOT/apps/web/src-tauri/binaries"
mkdir -p "$OUT_DIR"
rm -rf "$OUT_DIR/cLAWd-backend-$TARGET_TRIPLE"
mv "$DIST_DIR/cLAWd-backend" "$OUT_DIR/cLAWd-backend-$TARGET_TRIPLE"
# Tauri's sidecar resolver expects the entry binary at
# `<name>-<triple>` (without extension) — we move it up to satisfy that.
# We keep the rest of the onedir as a sibling _internal directory.
ENTRY="$OUT_DIR/cLAWd-backend-$TARGET_TRIPLE/cLAWd-backend"
if [[ -x "$ENTRY" ]]; then
    # Symlink or move the entry binary up next to the directory so Tauri
    # finds it at `binaries/cLAWd-backend-<triple>` like its docs show.
    cp -p "$ENTRY" "$OUT_DIR/cLAWd-backend-$TARGET_TRIPLE.bin"
    # Tauri actually wants the directory itself named with the triple
    # suffix when the externalBin path ends without a triple — leave the
    # current layout; the .bin copy is just a backup.
fi

echo "✓ sidecar at $OUT_DIR/cLAWd-backend-$TARGET_TRIPLE/"
echo "  Tauri's externalBin = \"binaries/cLAWd-backend\" picks this up."
