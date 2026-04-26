# Law School Study System

Local-first study system for 1L doctrinal courses. Ingest a textbook once, interact with it forever: case briefs, Socratic drills, IRAC grading, transcript-to-emphasis mapping.

**Canonical spec:** `spec.md`. **Build progress:** `CHECKLIST.md`. **Working agreement:** `CLAUDE.md`.

## Status

Phase 0 (bootstrap) in progress. No features are working yet.

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
