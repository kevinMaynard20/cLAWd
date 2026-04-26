#!/usr/bin/env bash
# cLAWd one-paste installer.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/kevinMaynard20/cLAWd/main/install.sh | bash
#
# What it does:
#   1. Detects your Mac's architecture (Apple Silicon vs Intel).
#   2. Pulls the latest release manifest from GitHub.
#   3. Downloads the matching .dmg into ~/Downloads.
#   4. Mounts it, copies cLAWd.app into /Applications, ejects the DMG.
#   5. Removes the macOS Gatekeeper "downloaded from internet" attribute
#      so the app opens without a right-click dance.
#   6. Launches the app.
#
# It does NOT require Python, Node, Rust, or any build tooling on your
# machine — everything ships pre-built in the DMG. ~150 MB download.
#
# Re-running is safe: the script overwrites the existing /Applications
# install in place (it's how every regular app updater works).

set -euo pipefail

REPO="kevinMaynard20/cLAWd"
APP_NAME="cLAWd.app"
APP_PATH="/Applications/${APP_NAME}"

# ---------- pretty output ----------

if [[ -t 1 ]]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; CYAN=$'\033[36m'; RED=$'\033[31m'
    GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
else
    BOLD=""; DIM=""; CYAN=""; RED=""; GREEN=""; YELLOW=""; RESET=""
fi
say()  { echo "${CYAN}${BOLD}→${RESET} $*"; }
warn() { echo "${YELLOW}${BOLD}⚠${RESET} $*" >&2; }
die()  { echo "${RED}${BOLD}✗${RESET} $*" >&2; exit 1; }

cleanup() {
    # Defensive unmount in case the script bails between mount + detach.
    if [[ -n "${MOUNTPOINT:-}" && -d "$MOUNTPOINT" ]]; then
        hdiutil detach "$MOUNTPOINT" -quiet -force >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

# ---------- preflight ----------

if [[ "$(uname)" != "Darwin" ]]; then
    die "cLAWd is currently macOS-only. Detected: $(uname)"
fi

ARCH="$(uname -m)"
case "$ARCH" in
    arm64|aarch64) LABEL="aarch64" ;;
    x86_64) LABEL="x86_64" ;;
    *) die "Unsupported architecture: $ARCH" ;;
esac
say "Architecture: ${BOLD}$LABEL${RESET}"

for cmd in curl hdiutil ditto plutil; do
    command -v "$cmd" >/dev/null 2>&1 || die "Missing required command: $cmd"
done

# ---------- locate the latest release ----------

say "Querying latest release on github.com/$REPO …"
RELEASE_JSON="$(
    curl -fsSL \
        -H "Accept: application/vnd.github+json" \
        "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null \
    || true
)"

if [[ -z "$RELEASE_JSON" ]] || [[ "$RELEASE_JSON" == *"\"message\":\"Not Found\""* ]]; then
    cat >&2 <<EOF
${RED}${BOLD}✗ No GitHub Release found yet.${RESET}

The repository owner hasn't published a release. You can either:

  1. Wait for them to push a tag (the GitHub Actions workflow at
     .github/workflows/release.yml builds + publishes automatically).

  2. Build from source — needs Rust, pnpm, and PyInstaller:
     ${DIM}git clone https://github.com/$REPO.git${RESET}
     ${DIM}cd cLAWd && bash scripts/build_app.sh${RESET}

EOF
    exit 1
fi

# Pull the right asset URL out of the JSON without jq (curl|bash users
# rarely have it installed). Python's json module is on every Mac.
ASSET_URL="$(
    python3 - <<PY
import json, sys
data = json.loads(sys.stdin.read() or "{}")
label = "${LABEL}"
for asset in data.get("assets", []):
    name = asset.get("name", "")
    if name.endswith(f"-{label}.dmg"):
        print(asset.get("browser_download_url", ""))
        break
PY
<<<"$RELEASE_JSON"
)"

if [[ -z "$ASSET_URL" ]]; then
    die "Latest release has no DMG for $LABEL. Try the .app.tar.gz under Releases manually."
fi

TAG="$(python3 -c 'import json,sys;print(json.loads(sys.stdin.read())["tag_name"])' <<<"$RELEASE_JSON")"
say "Latest release: ${BOLD}$TAG${RESET}"

# ---------- download ----------

DOWNLOADS="${HOME}/Downloads"
mkdir -p "$DOWNLOADS"
DMG_PATH="${DOWNLOADS}/cLAWd-${TAG}-${LABEL}.dmg"

say "Downloading $(basename "$DMG_PATH") …"
curl -fL --progress-bar "$ASSET_URL" -o "$DMG_PATH"

# ---------- install ----------

say "Mounting DMG …"
MOUNT_OUT="$(hdiutil attach "$DMG_PATH" -nobrowse -readonly -quiet)"
MOUNTPOINT="$(echo "$MOUNT_OUT" | awk '/\/Volumes\// {for(i=3;i<=NF;i++) printf "%s ", $i; print ""}' | head -n1 | sed 's/ *$//')"
[[ -d "$MOUNTPOINT/$APP_NAME" ]] || die "DMG mounted but $APP_NAME not present at $MOUNTPOINT"

if [[ -d "$APP_PATH" ]]; then
    say "Replacing existing /Applications/$APP_NAME …"
    rm -rf "$APP_PATH"
fi

say "Copying to /Applications (this is the only step that may prompt for your password) …"
ditto "$MOUNTPOINT/$APP_NAME" "$APP_PATH"

say "Ejecting DMG …"
hdiutil detach "$MOUNTPOINT" -quiet
MOUNTPOINT=""

# Strip the quarantine attribute so first launch doesn't trigger the
# "from the internet — are you sure?" dialog. Safe because the user just
# explicitly downloaded + ran our installer.
say "Clearing quarantine flag …"
xattr -dr com.apple.quarantine "$APP_PATH" 2>/dev/null || true

# ---------- launch ----------

say "Launching cLAWd …"
open "$APP_PATH"

cat <<EOF

${GREEN}${BOLD}✓ cLAWd installed.${RESET}

  App:        $APP_PATH
  Data dir:   ~/Library/Application Support/cLAWd/
  Update:     re-run this installer; it overwrites in place.

First launch the app shows a Get Started panel walking you through
upload-textbook → brief-cases → study tools.

EOF
