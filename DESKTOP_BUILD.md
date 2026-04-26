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

## Things to remember (lessons from the first end-to-end build)

- **Ad-hoc sign without `--deep`.** `codesign --force --deep --sign -` will
  walk INTO the PyInstaller binary's embedded archive and overwrite bytes
  the bootloader needs — the next launch is silently broken (process
  starts, stdio detached, no banner, no port bind). `build_app.sh` only
  signs the two outer binaries (`cLAWd`, `cLAWd-backend`) which is enough
  for Keychain / Gatekeeper purposes.

- **Force the encrypted-file credentials backend in the bundle.**
  `cLAWd_backend_entry.py` sets `LAWSCHOOL_FORCE_FILE_BACKEND=1` when
  frozen. macOS's security daemon hangs indefinitely when an ad-hoc-
  signed Tauri-spawned PyInstaller binary tries to call into Keychain —
  even after the user types their password the request never returns.
  The encrypted-file backend (HKDF-derived Fernet, spec §7.7.2) writes
  to `~/Library/Application Support/cLAWd/credentials.enc` and bypasses
  security-server entirely. Trade-off: no system-Keychain integration.

- **CORS allow_origins=["*"].** `allow_origin_regex=".*"` doesn't reflect
  non-http schemes correctly under starlette's middleware — the bundled
  WebView's `tauri://localhost` origin came back without an
  `Access-Control-Allow-Origin` header and the browser blocked the
  response. The wildcard form is the simplest path that works for every
  origin a 127.0.0.1-only backend can see (dev server, Tauri WebView,
  any future `asset://` schemes).

- **45 s wait for the sidecar to bind.** PyInstaller's onefile bootloader
  unpacks ~150 MB of Python + C extensions to `/var/folders/.../T/_MEI*`
  on first launch — measured 20–25 s on Intel, less on Apple Silicon.
  Subsequent launches reuse the cached unpack and boot in ~2 s. The Rust
  shell waits up to 45 s for `:8000` to answer before showing the
  WebView; the window won't open with a dead backend.

- **The `/help/*` tree is a public route.** `FirstRunGate` redirects to
  `/first-run` whenever the LLM gate says no key — but the API-key
  walkthrough page (`/help/api-key`) lives BEFORE the user has saved a
  key. We exempt the `/help` prefix in `FirstRunGate` so the embedded
  YouTube tutorial page actually loads.

## One gap left (distribution polish)

**Apple-signed code + notarization.** Ad-hoc signing handles local-machine
Gatekeeper. For a download-from-the-internet flow (the install.sh path),
notarize with a Developer ID and run Tauri's `notarize` action. See
Tauri's macOS distribution docs.

Closed:

- **Dynamic routes** — each `[param]/page.tsx` is now a tiny server
  component that exports `generateStaticParams` (a single `__shell__`
  placeholder) and renders the client UI from `ClientPage.tsx`.
- **Icons** — `scripts/generate_icons.py` renders the full Apple icon
  matrix from a 1024×1024 master via PIL + `iconutil`. Design: deep-navy
  background, large cream serif § flanked by gold `{}` brackets,
  "cLAWd" wordmark.
- **Backend reachability** — see "Things to remember" above. End-to-end
  test confirmed: bundled `.app` boots, WebView talks to `:8000`,
  `/credentials/anthropic` POST stores the key, `/credentials/gate`
  reports `llm_enabled: true`, dashboard loads.

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
