#!/usr/bin/env bash
# One-shot build of the cLAWd.app:
#   1. Bundle the FastAPI backend with PyInstaller → src-tauri/binaries/
#   2. Build the Next.js frontend (static export → apps/web/out/)
#   3. Compile the Tauri shell → apps/web/src-tauri/target/release/bundle/
#
# Output:
#   apps/web/src-tauri/target/release/bundle/macos/cLAWd.app
#   apps/web/src-tauri/target/release/bundle/dmg/cLAWd_0.1.0_aarch64.dmg
#
# Drag the .app into /Applications and you're done. The bundle is signed
# only if you set TAURI_SIGNING_PRIVATE_KEY etc. — see Tauri docs. For a
# single-user local install, ad-hoc Gatekeeper "Open anyway" works.
#
# First-time prereqs (one-shot, ~5 minutes):
#   brew install rust pnpm
#   .venv/bin/pip install pyinstaller
#   cd apps/web && pnpm install
#
# Re-run this script every time backend code changes (the sidecar binary
# needs to be re-bundled). For pure frontend changes use `pnpm tauri:dev`
# from apps/web/ — Tauri spawns the dev server live and hot-reloads.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "============================================================"
echo "  cLAWd.app build pipeline"
echo "============================================================"

echo
echo "→ [1/3] bundling FastAPI backend with PyInstaller…"
bash scripts/build_python_bundle.sh

echo
echo "→ [2/3] building Next.js static export…"
cd apps/web
NEXT_OUTPUT=export pnpm build
cd "$REPO_ROOT"

echo
echo "→ [3/3] compiling Tauri shell + packaging .app…"
cd apps/web
pnpm tauri build
cd "$REPO_ROOT"

# Ad-hoc sign both binaries inside the freshly-built .app. Without this,
# macOS's security daemon refuses Keychain calls from the Tauri-spawned
# sidecar (silent, indefinite hang). We sign the OUTER binaries only —
# `--deep` would traverse into the PyInstaller-embedded archive and
# corrupt the bootloader's PYZ lookup. The bundled entry script also
# forces the encrypted-file credentials backend (LAWSCHOOL_FORCE_FILE_
# BACKEND=1) so the keychain is never touched anyway, but signing the
# binaries makes Gatekeeper less sus and fixes other minor friction.
APP_PATH="$REPO_ROOT/apps/web/src-tauri/target/release/bundle/macos/cLAWd.app"
if [[ -d "$APP_PATH" ]] && command -v codesign >/dev/null 2>&1; then
    echo
    echo "→ ad-hoc signing the bundle"
    codesign --force --sign - "$APP_PATH/Contents/MacOS/cLAWd-backend" 2>&1 | tail -1
    codesign --force --sign - "$APP_PATH/Contents/MacOS/cLAWd" 2>&1 | tail -1
fi

echo
echo "✓ Build complete."
APP_PATH="$REPO_ROOT/apps/web/src-tauri/target/release/bundle/macos/cLAWd.app"
DMG_GLOB="$REPO_ROOT/apps/web/src-tauri/target/release/bundle/dmg/cLAWd_*.dmg"
if [[ -d "$APP_PATH" ]]; then
    echo "  .app: $APP_PATH"
fi
for dmg in $DMG_GLOB; do
    [[ -f "$dmg" ]] && echo "  .dmg: $dmg"
done
