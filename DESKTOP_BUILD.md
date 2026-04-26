# Building cLAWd.app (Tauri shell)

This branch (`tauri-shell`) packages the FastAPI backend + Next.js frontend
as a single double-clickable Mac app. No Terminal, no `make dev`, no
manual port management — the app boots, waits for the backend, opens its
window, and tears the whole stack down on close.

## What you get

- `cLAWd.app` — drag into `/Applications`, double-click to launch.
- Single Dock icon. ⌘Q closes the window AND kills the backend (synchronous
  via the Rust shell's `WindowEvent::CloseRequested` handler).
- All user data lives at `~/Library/Application Support/cLAWd/`:
  - `lawschool.db` — SQLite database
  - `pymupdf_raw/` — cached page-by-page extractions
  - `marker_raw/` — Marker output (only when Marker is installed)
  - `credentials.enc` — fallback file for the Anthropic key (when the OS
    keyring is unavailable; primary store is still the macOS keychain)
- Spec §7.6 still holds: backend binds to 127.0.0.1 only.

## First-time prereqs

```
brew install rust pnpm
.venv/bin/pip install pyinstaller
(cd apps/web && pnpm install)   # picks up @tauri-apps/cli
```

Rust adds ~1 minute the first time it compiles the Tauri shell; cached
afterwards. PyInstaller is fast — it just freezes the venv.

## Build the .app

```
bash scripts/build_app.sh
```

Three stages:
1. PyInstaller bundles the backend → `apps/web/src-tauri/binaries/cLAWd-backend-aarch64-apple-darwin/`.
2. `pnpm build` (with `NEXT_OUTPUT=export`) emits the static frontend → `apps/web/out/`.
3. `pnpm tauri build` compiles the Rust shell + packages everything → `apps/web/src-tauri/target/release/bundle/macos/cLAWd.app`.

The DMG variant ships at `…/bundle/dmg/cLAWd_0.1.0_aarch64.dmg` (ready to
hand off via download / AirDrop).

## Iterating

For frontend-only changes use Tauri's dev mode — it spawns `next dev` and
the backend sidecar live, with hot-reload:

```
cd apps/web && pnpm tauri:dev
```

For backend-only changes, re-run `bash scripts/build_python_bundle.sh` then
`pnpm tauri build` — the frontend layer is unchanged. Or just continue
using `make dev` from the repo root for development; the bundled `.app` is
strictly a delivery format.

## Known gaps (open work)

This branch shipped the scaffolding + build pipeline. Two items still need
attention before the .app is fully production-ready:

1. **Static-export dynamic routes.** The 6 dynamic pages
   (`/corpora/[id]`, `/artifacts/[id]`, etc.) need
   `generateStaticParams` exports for Next 15's `output: "export"` mode.
   Until then the export build either fails or produces a SPA shell that
   only resolves dynamic routes via in-app `<Link>` navigation — typing
   `/corpora/abc` directly in the URL bar won't work. For a one-user
   desktop app where you always enter via the Get Started panel and click
   through, this is acceptable; the user never types URLs.

   Fix path: add `export function generateStaticParams() { return [] }`
   to each `[param]/page.tsx` and configure Tauri to fall back to
   `index.html` for unknown paths. ~30 minutes of work.

2. **Real icons.** `apps/web/src-tauri/icons/` currently has 1×1 PNG
   placeholders so the bundle pipeline doesn't reject the build. To swap
   in real artwork:
   ```
   cd apps/web && pnpm tauri icon path/to/source-1024x1024.png
   ```
   Drops a full set (32×32, 128×128, 128×128@2x, .icns, .ico) into the
   icons directory.

3. **Code signing + notarization.** For ad-hoc local install (drag
   /Applications, right-click → Open) Gatekeeper just nags once. For a
   real distribution flow, sign with an Apple Developer ID and run the
   Tauri notarize action. See Tauri's macOS distribution docs.

## How the shell handles cleanup

Three independent kill paths in `apps/web/src-tauri/src/main.rs`:

- `WindowEvent::CloseRequested` — fires when the user clicks the red
  traffic-light or hits ⌘Q. Kills the backend BEFORE the WebView shuts
  down so the next launch doesn't see "address in use" on :8000.
- `RunEvent::Exit` — fires when the run loop terminates (logout, force
  quit via Activity Monitor, the app crashes). Same kill, second chance.
- `BackendProcess` is a Tauri-managed `Mutex<Option<CommandChild>>`; the
  mutex makes start/stop atomic, and the option lets us `.take()` the
  child so a double-kill no-ops cleanly.

Tauri's `tauri-plugin-shell` only exposes `kill()` (SIGKILL on POSIX), not
SIGTERM, so the FastAPI shutdown hooks don't run. The SQLite WAL is
checkpointed on every commit so this doesn't risk corruption — the
trade-off is the loss of any in-flight LLM-call rollback metadata, which
is rebuilt next session anyway.
