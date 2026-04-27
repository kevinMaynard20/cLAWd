# cLAWd — Law School Study System

Local-first study system for 1L doctrinal courses. Ingest a textbook once, interact with it forever: case briefs, flashcards, multiple-choice questions, multi-case synthesis, attack sheets, hierarchical outlines, IRAC grading, Socratic and cold-call drills, transcript-to-emphasis mapping.

> **Canonical spec:** `spec.md`. **Build progress:** `CHECKLIST.md`. **Working agreement:** `CLAUDE.md`.

## Install on a Mac (~2 minutes)

You don't need to know any code. You don't need Python, Node, Rust, or Homebrew.

1. **Open Terminal.** Press <kbd>⌘ Space</kbd>, type `Terminal`, hit <kbd>Return</kbd>.

2. **Paste this single line** into the Terminal window and press <kbd>Return</kbd>:

   ```sh
   curl -fsSL https://raw.githubusercontent.com/kevinMaynard20/cLAWd/main/install.sh | bash
   ```

3. **Wait.** The installer will:
   - Detect whether your Mac is Apple Silicon or Intel.
   - Download the matching `.dmg` (~150 MB) into your `Downloads` folder.
   - Mount it, copy `cLAWd.app` into `/Applications`, eject it.
   - Strip the macOS "downloaded from internet" warning.
   - Launch the app.

   You may be prompted for your Mac password once (so it can copy into `/Applications`). That's normal.

4. **In the app**, paste your Anthropic API key when asked. Don't have one? Click "How do I get this?" — there's a short walkthrough video in-app.

5. **Click `Upload`** in the top bar to add a casebook PDF.

That's it. To re-install or update later, paste the same one-liner — it overwrites cleanly.

### Where things live on your Mac

- **App:** `/Applications/cLAWd.app` — drag to Trash to uninstall.
- **Your data** (database, uploads, generated artifacts, encrypted API key): `~/Library/Application Support/cLAWd/`.
- **Logs** (if anything goes wrong): `~/Library/Logs/cLAWd/sidecar.log`.

Closing the window quits the app cleanly. The bundled backend gets killed on close — no orphan processes, no stuck ports.

### Manual download (no Terminal)

Prefer not to paste into Terminal? Open the [latest release page](https://github.com/kevinMaynard20/cLAWd/releases/latest), download the `.dmg` for your architecture (`aarch64` = Apple Silicon, `x86_64` = Intel Mac), double-click it, and drag `cLAWd.app` to `/Applications`.

## Stack

- **Backend:** Python 3.11, FastAPI, SQLite + sqlite-vec
- **Frontend:** Next.js (App Router), TypeScript, Tailwind, shadcn/ui
- **LLM:** Anthropic Claude (Opus 4.7 default) via the user's own API key
- **Embeddings:** Voyage AI (optional; BM25 fallback if absent)
- **PDF→markdown:** Marker (`--use_llm`), PyMuPDF4LLM fallback
- **Audio:** faster-whisper (local)

## Running

One command for the whole stack (FastAPI backend on `:8000`, Next.js frontend on `:3000`):

```
make dev
```

Ctrl-C stops both cleanly. If a prior session crashed and orphans remain:

```
make stop      # hard-kill anything on the dev ports
make restart   # stop + dev
```

Open http://localhost:3000. The first-run wall asks for your Anthropic API key (stored in your OS keychain). API docs live at http://127.0.0.1:8000/docs.

For a packaged Mac app (single double-clickable `.app`, no Terminal, backend dies on close), see `DESKTOP_BUILD.md`. The Tauri build pipeline lives on the `tauri-shell` branch; one command (`bash scripts/build_app.sh`) produces `cLAWd.app` + a DMG.

**First-time setup** (once):

```
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cd apps/web && npm install && cd -
```

Optional heavy deps (only if you'll actually run Marker / faster-whisper locally):

```
.venv/bin/pip install -e '.[dev,marker,audio]'
```

**Tunables** (env vars): `LAWSCHOOL_TASK_CONCURRENCY` (default 1; how many ingest tasks run in parallel), `LAWSCHOOL_MAX_PDF_BYTES` (default 1 GiB), `LAWSCHOOL_MAX_TEXT_BYTES` (default 50 MiB), `LAWSCHOOL_MONTHLY_CAP_USD`, `LAWSCHOOL_BACKEND_PORT` / `LAWSCHOOL_FRONTEND_PORT`.

## Layout

See spec §7.2. Top-level:

```
apps/api        FastAPI backend (primitives, features, costs, credentials, data)
apps/web        Next.js frontend (first-run wall, settings, reading view, cost badge)
packages/prompts   Versioned prompt templates (spec §2.4)
packages/schemas   JSON schemas for artifact outputs
config/            TOML configs (models, pricing, emphasis weights, default profile)
storage/           Content-addressed local store (books, transcripts, artifacts, marker output) — gitignored
tests/             Fixture corpus + manual test scenarios
```

## Key principles (spec §2, abridged)

- **Compose over primitives.** Ingest, Retrieve, Generate, Verify. Everything user-facing is a thin orchestration of these four.
- **Source page numbers, never PDF indices.** The number on the page the professor cites is the only number the UI shows.
- **Prompts are data.** Every prompt is a versioned file under `packages/prompts/`.
- **Tests alongside code.** Every feature ships with a failing test *first*.
- **Never hallucinate citations.** Verification runs on critical paths.
- **Every LLM call emits a `CostEvent`.** Cache hits record $0 with `cached=true`.

## Running tests

```
uv run pytest                       # all Python tests
uv run pytest apps/api/tests/unit   # unit only
pnpm --filter @lawschool/web test   # web tests
```

LLM-dependent tests run against a replay cache (§6.3). Live-API tests are gated behind `TEST_ANTHROPIC_KEY` env and skipped by default.
