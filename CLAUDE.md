# CLAUDE.md — Working Agreement for This Project

This repo is being built to the specification in `spec.md` (Law School Study System). This file governs how you (Claude Code) work on it.

## Running the app

The whole stack — FastAPI backend on `127.0.0.1:8000` and Next.js frontend on `localhost:3000` — runs with one command:

```
make dev
```

What this does (`scripts/dev.sh`):
- Pre-flight: kills anything already bound to ports 8000 / 3000 so re-running is idempotent.
- Verifies `.venv/` and `apps/web/node_modules/` exist; bails with a clear message if not.
- Starts uvicorn (`apps/api/src/main:app`) and `npm run dev` (Next.js) in parallel.
- Interleaves their output with `[api]` / `[web]` prefixes (cyan / green when stdout is a TTY).
- **Ctrl-C tears down both cleanly**: cooperative SIGTERM walks the process tree, then a port-bound SIGKILL sweep catches any orphan worker thread.
- If either process dies on its own, the survivor is torn down too — never a half-running stack.

When `make dev` was force-killed (`kill -9` on the script itself bypasses the trap) and orphans remain on the dev ports:

```
make stop
```

That hard-kills anything bound to 8000 / 3000 and any stray `uvicorn main:app` / `next dev` processes by name. Idempotent.

```
make restart    # stop + dev
```

URLs:
- App: http://localhost:3000
- API docs (Swagger): http://127.0.0.1:8000/docs
- Health check: http://127.0.0.1:8000/system/health

First-time setup (one-shot):

```
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cd apps/web && npm install && cd -
```

## Test material in `docs/`

The user keeps real-world fixtures under the repo's top-level `docs/` directory:

- One **textbook PDF** (the assigned casebook for the course being tested).
- Several **practice exams** (PDFs or markdown). Some have grader memos; some don't.
- Possibly a syllabus and a transcript or two.

When you need to demonstrate or smoke-test an end-to-end flow (book ingestion, case-brief, IRAC grading, etc.), look in `docs/` first instead of asking the user for paths or generating synthetic fixtures. The contents are gitignored / not checked in — treat the folder as the user's local test bench.

A workflow-by-workflow walk-through of every must-have / high-value feature (with friction notes and concrete UI plans) lives in `docs/WORKFLOWS.md`. Read that before claiming a feature is "done" — backend wired up ≠ user can actually drive it.

Tunables (env vars):
- `LAWSCHOOL_TASK_CONCURRENCY` — how many ingestion tasks run in parallel (default 1; cap 8). Bump to 2 if you've got plenty of RAM and want two casebooks ingesting at once.
- `LAWSCHOOL_MAX_PDF_BYTES` — per-PDF upload cap (default 1 GiB).
- `LAWSCHOOL_MAX_TEXT_BYTES` — per-text-file upload cap (default 50 MiB).
- `LAWSCHOOL_MONTHLY_CAP_USD` — monthly Anthropic spend ceiling.
- `LAWSCHOOL_BACKEND_PORT` / `LAWSCHOOL_FRONTEND_PORT` — override the default 8000 / 3000.

## The checklist is mandatory

`CHECKLIST.md` is the live source of truth for progress. **You MUST consult and update it before and after every task.** No exceptions.

### Before starting any task

1. **Open `CHECKLIST.md` first.** Every session, every task.
2. **Find the task.** It should already be listed. If it's not, add it to the appropriate Phase/section before you start. Never silently expand scope — writing it into the checklist is the scoping step.
3. **Mark it `[~]` (in-progress).** This is the handshake that commits you to the work.
4. **Re-read the relevant spec section.** The checklist lists deliverables; the spec explains them. Do not work from memory of the spec.

### While working

- If you discover sub-tasks that belong under the current item, add them as nested checkboxes under it.
- If you hit a blocker, mark the item `[!]` with an inline note explaining what you're blocked on, and log the open question in `SPEC_QUESTIONS.md` per spec §0.
- If you decide an item is not applicable (e.g., superseded, out of scope), mark `[-]` with an inline reason — don't delete it.

### After completing a task

1. **Mark it `[x]`** as soon as it's done. Do not batch completions.
2. **Verify the spec's exit criterion** if the task closes out a phase or major feature — re-read the "Exit criterion" line in the checklist and confirm you've met it.
3. **Update related items.** If finishing task A unblocks task B, and B was `[!]`, clear the blocker note.

### When the spec is wrong or incomplete

Per spec §0: prefer asking the user over guessing. Log the ambiguity in `SPEC_QUESTIONS.md` and keep moving on unrelated work. Do not invent design decisions and bury them in code.

## Non-negotiables from the spec

These are the principles the spec calls "constraints on every decision" (§2). Before any substantive change, confirm you're not violating one:

- **Composed over primitives.** Every feature in §5 must be a thin orchestration of the four primitives (Ingest, Retrieve, Generate, Verify). If you're tempted to write a bespoke retrieval or prompt plumbing layer, stop and reconsider.
- **Source page numbers, never PDF indices.** Users say "page 518" meaning the printed number. Never expose PDF page indices.
- **Never ask an LLM to read a PDF directly.** Always route through the Marker pipeline first.
- **Prompts are data.** Every prompt is a versioned file under `packages/prompts/`, loaded at runtime. Never hardcode prompt strings.
- **Tests alongside code, not after.** The first commit for any feature must include at least one failing test. Feature "done" = tests passing, including e2e.
- **Grade against rubrics, not vibes.** IRAC grading is rubric-driven, deterministic given a rubric, auditable.
- **Never hallucinate citations.** Every claim traces to a Block id or Transcript segment id. Verification runs on critical paths.
- **Every LLM call emits a CostEvent.** No exceptions. Cache hits emit with `cached=true, total_cost_usd=0`.

## Execution order

Build phases (spec §9) are sequential, not parallel at the phase level. **Do not skip ahead.** Within Phase 5 the sub-features can be parallelized, but Phases 1→2→3→4 are a chain with exit criteria.

## Secrets & safety

- API keys go in the OS keyring (spec §7.7.2). Never write them to files, logs, or prompts.
- When rendering a key anywhere, show only the last 4 chars: `sk-ant-…XXXX`.
- The API binds to `127.0.0.1` only (spec §7.6).
- Single-user local app — no auth, no multi-tenant anything.

## Model & tooling defaults

- Generation: Claude Opus 4.7 by default (configurable per feature, spec §7.7.6).
- Embeddings: Voyage AI (optional; fallback to BM25 lexical with a visible badge if no key).
- PDF→markdown: Marker with `--use_llm`.
- Audio transcription: faster-whisper locally.

## When in doubt

The spec is the source of truth. The checklist tracks progress against it. This file governs behavior. When they conflict, spec > checklist > CLAUDE.md, and you should fix the lower-priority file.
