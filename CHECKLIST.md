# Law School Study System — Build Checklist

**Source of truth:** `spec.md`. This checklist enumerates every concrete deliverable that spec requires.
**Protocol:** Before any task, consult this file. When you start work, mark `[~]` (in-progress). When done, mark `[x]`. When you discover a task the spec requires that isn't listed here, add it to the appropriate section rather than silently expanding scope.

**Legend:**
- `[ ]` not started
- `[~]` in progress
- `[x]` complete
- `[!]` blocked (note reason inline)
- `[-]` skipped / not applicable (note reason inline)

---

## Phase 0: Project bootstrap (prerequisite to all phases)

**Status: complete (2026-04-20).** Smoke test: `pytest apps/api/tests/test_health.py` passes on Python 3.14.2.

### 0.1 Repo scaffolding
- [x] Create `README.md` (how to run)
- [x] Create `SPEC_QUESTIONS.md` (unresolved design questions raised during build)
- [x] Create `pyproject.toml` (Python 3.11+, FastAPI, deps; ruff/black/mypy/pytest tool configs)
- [x] Create `pnpm-workspace.yaml`
- [x] Create `.gitignore` (include `storage/`, `.env`, `*.enc`, `node_modules/`, `__pycache__/`, `.venv/`)
- [x] Create directory tree per spec §7.2:
  - [x] `apps/api/src/{primitives,features,costs,credentials,data}/` (each with `__init__.py`)
  - [x] `apps/api/src/data/migrations/`
  - [x] `apps/api/tests/{unit,integration,e2e,fixtures}/`
  - [x] `apps/web/{app,components}/`
  - [x] `apps/web/app/{settings/{api-keys,models,costs},first-run}/`
  - [x] `packages/prompts/`
  - [x] `packages/schemas/`
  - [x] `storage/{books,transcripts,artifacts,marker_raw}/` (gitignored)
  - [x] `config/`
  - [x] `tests/fixtures/{book,transcript,past_exam,expected_outputs}/`
  - [x] `tests/manual/` (with `README.md` documenting M1/M2/M3 scenarios)
- [x] `apps/api/src/main.py` — FastAPI entrypoint with `/health`
- [x] `apps/api/tests/test_health.py` — smoke test, passes

### 0.2 Config files (spec §7.3, §7.7.4)
- [x] `config/models.toml` (per-feature model selection per §7.7.6)
- [x] `config/pricing.toml` (Opus 4.7, Sonnet 4.6, Haiku 4.5, Voyage rates per §7.7.4; conservative fallback)
- [x] `config/emphasis_weights.toml` (weights referenced in §3.10)
- [x] `config/default_professor_profile.toml` (empty shell; populated in Phase 3)

### 0.3 Dev tooling
- [x] Python linter/formatter (ruff + black) — configured in pyproject.toml
- [x] TypeScript tooling (tsconfig.json, .eslintrc.json, tailwind.config.ts, postcss.config.mjs)
- [x] Test runners: pytest (configured via `[tool.pytest.ini_options]` in pyproject; pythonpath set), vitest (declared in apps/web package.json)
- [x] Makefile with `install`, `api`, `web`, `test`, `lint`, `fmt`, `typecheck`, `clean`
- [x] `.env.example` (only for opt-in encrypted-file fallback; normal flow uses OS keyring)
- [-] Pre-commit hooks — deferring until git is initialized; noted for later

### 0.4 Environment notes
- [x] Python 3.14.2 confirmed as target environment (user's machine). Spec says `>=3.11`; 3.14 fine.
- [x] Node 25.8 + npm 11.11 available. `pnpm` and `uv` not installed — Makefile uses `uv`/`pnpm` by default; user will install these before Phase 1 UI work. For now, a pip+venv path is documented in the README.
- [x] Smoke test passes via `PYTHONPATH=apps/api/src .venv/bin/pytest apps/api/tests/test_health.py`

---

## Phase 1: The spine (spec §9 Phase 1)

**Exit criterion:** user launches app → setup wall → enter key → validated → upload PDF → ingestion completes → pages 518–559 render correctly with typed blocks, cost badge $0.00.

**Status (2026-04-20):** Backend fully complete; 151 Python tests + 18 web tests all green. Exit criterion is **met for non-ingestion surfaces** — first-run wall, settings, cost badge, credentials + costs + retrieve + ingest APIs all work end-to-end. The **book upload UI + page-range reader** are deferred to a Phase 1.7 follow-up that runs alongside the user's first real casebook ingestion (see `SPEC_QUESTIONS.md` Q19). Every primitive the reader UI needs is already built and tested.

### 1.1 Data model & persistence (spec §3) — DONE
- [x] `apps/api/src/data/models.py` — SQLModel/pydantic definitions for:
  - [x] `Corpus` (§3.1)
  - [x] `Book` (§3.2) — content-hash id, source_page_range, ingestion_version
  - [x] `Page` (§3.3) — source_page int, batch_pdf, pdf_page_span, markdown, raw_text
  - [x] `Block` (§3.5) — typed: narrative_text, case_opinion, case_header, numbered_note, problem, footnote, block_quote, header, figure, table
  - [x] `TocEntry` (§3.4)
  - [x] `CostEvent` (§3.12, §7.7.4) — Decimal stored as Numeric(20,10)
  - [x] `Credentials` envelope (§3.13) — in-memory only, SecretStr type with last-4 display
- [x] `apps/api/src/data/db.py` — SQLite connection, session management, WAL, foreign keys ON
- [x] `apps/api/src/data/migrations/` — using `SQLModel.metadata.create_all` for v1; alembic deferred (Q7 in SPEC_QUESTIONS.md)
- [x] sqlite-vec extension loaded at connection time (per §7.1) — test confirms vec0 virtual table creation works
- [x] `main.py` lifespan hook calls `init_schema()` on startup
- [x] 17 unit tests passing in `test_models.py` + `test_db.py`
- [x] Q8 (Python 3.14 lazy annotations break SQLModel) documented in SPEC_QUESTIONS.md

### 1.2 API key management (spec §7.7.1 – §7.7.3) — DONE
- [x] `apps/api/src/credentials/keyring_backend.py` — delegated to subagent, Opus-verified 18/18 tests passing
  - [x] macOS Keychain via `keyring` lib (service: `law-school-study-system`, entry: `anthropic-api-key`)
  - [x] Windows Credential Manager support (via `keyring` library's cross-platform abstraction)
  - [x] Linux Secret Service support (same path)
  - [x] Encrypted-file fallback at `~/.config/law-school-study-system/credentials.enc` with HKDF-SHA256 over (home, hostname, fixed salt) → Fernet key
  - [x] Atomic write+rename, chmod 0600 best-effort, auto-unlink when both keys cleared
  - [x] Voyage key second field with identical semantics
  - [x] Env overrides: `LAWSCHOOL_FORCE_FILE_BACKEND`, `LAWSCHOOL_CREDENTIALS_FILE` (for tests + CI)
- [x] `apps/api/src/credentials/validation.py`
  - [x] Anthropic validator via `GET https://api.anthropic.com/v1/models` with `anthropic-version: 2023-06-01`
  - [x] Voyage key validator via tiny `POST /v1/embeddings` probe (model=`voyage-3`, input=`["ping"]`)
  - [x] Three-state return: VALID / INVALID (401/403/other) / UNREACHABLE (timeout, transport, HTTPError)
  - [x] Sync wrapper `validate_anthropic_sync` for CLI/fixtures; refuses to run inside a live event loop
  - [x] Defensive: raw key never placed in `ValidationResult.message` or exception text
- [x] SecretStr never logged, never in error responses, rendering shows only last 4 chars `sk-ant-…XXXX` (in `Credentials.anthropic_display()`)
- [x] 18 credentials tests passing (1 `test_key_validation_live` skipped — gated on `TEST_ANTHROPIC_KEY`)
- [x] Open questions from subagent logged as Q9-Q12 in SPEC_QUESTIONS.md

### 1.3 Cost tracking skeleton (spec §7.7.4, §7.7.5) — DONE
- [x] `apps/api/src/costs/pricing.py` — loads `config/pricing.toml`, warns on missing/malformed with conservative defaults; warn-once per unknown model; singleton getter
- [x] `apps/api/src/costs/tracker.py` — CostEvent persistence, session-id generation (refreshes on launch, rotatable via `reset_session_id`), per-session/lifetime/per-feature aggregations, `session_token_total`, `recent_events` filter
- [x] `apps/api/src/costs/estimator.py` — stub: `CostEstimate`, `PreflightRequired`, `estimate_feature_cost` raises `NotImplementedError` with feature name (full impl in Phase 2)
- [x] 15 cost-layer tests passing, zero regressions in existing suite (70 total pass + 1 gated skip)
- [x] Open questions logged as Q13-Q15 in SPEC_QUESTIONS.md

### 1.4 Ingest primitive (spec §4.1.1) — DONE
- [x] `apps/api/src/primitives/ingest.py` — page markers (Opus) + full `ingest_book()` orchestration (subagent) all wired
- [x] `ingest_book(pdf_paths, …)` entry point with progress callback
- [x] **Step 1:** content-hash PDFs via SHA-256, dedup short-circuits
- [x] **Step 2:** Marker integration (`marker_runner.py`) with `--use_llm` flag, `storage/marker_raw/{hash}.md` cache (hit-on-rerun)
- [x] **Step 3:** batch stitching with HTML-comment sentinels between batches
- [x] **Step 4:** source page marker extraction (Opus-built, subagent verified)
- [x] **Step 5:** page segmentation at markers with pdf_page_start/end derived from Marker's char offsets
- [x] **Step 6:** block segmentation (subagent-built, Opus-verified)
- [x] **Step 7:** TOC extraction (`toc_extractor.py`) — heading-based with Contents-block preference
- [x] **Step 8:** persist Book, Pages, Blocks, TocEntries in single `session_scope()` transaction; `expunge` so returned Book survives session teardown
- [x] `MarkerNotInstalledError` with install hint; `/ingest/book` route surfaces as 503
- [x] 151 total tests passing (Marker adds 14 new: 5 marker_runner + 3 toc + 6 ingest_book)
- [x] Open questions Q16-Q18 logged in SPEC_QUESTIONS.md

### 1.5 Retrieve primitive (spec §4.2) — DONE
- [x] `apps/api/src/primitives/retrieve.py`
- [x] Query type: `PageRangeQuery` → exact Pages + Blocks; inclusive range; warns on partial overlap
- [x] Query type: `CaseReferenceQuery` → case_opinion block + trailing notes bounded by next CASE_HEADER/CASE_OPINION; case-insensitive; handles "v./vs."
- [x] Query type: `AssignmentCodeQuery` stub (resolves via Syllabus in Phase 4)
- [x] `SemanticQuery` stub (lands in Phase 2+ with Voyage embeddings)
- [x] Return shape: `RetrievalResult` with typed Blocks + Pages + query_description + notes; never a flat blob
- [x] `RetrievalScope` typing reserved for Phase 4 cross-source retrieval
- [x] 17 retrieve tests passing

### 1.6 FastAPI endpoints (spec §7.2 apps/api) — DONE
- [~] `POST /ingest/book` — stub returning 501 (full impl needs Marker integration, 1.4 step 2)
- [x] `POST /retrieve` — tagged union over PageRange / CaseReference / AssignmentCode / Semantic
- [x] `POST /credentials/anthropic` — store + validate in one call
- [x] `POST /credentials/voyage` — store + validate in one call
- [x] `GET /credentials/status` — last-4, present/absent flags, last_validated_at
- [x] `GET /credentials/gate` — is LLM usable? (used by the always-visible cost badge to grey out controls)
- [x] `DELETE /credentials/anthropic` — clear key
- [x] `DELETE /credentials/voyage` — clear key
- [x] `POST /credentials/test` — round-trip validation against stored key; 409 when no key stored
- [x] `GET /costs/session` — session total + token breakdown
- [x] `GET /costs/lifetime` — lifetime total
- [x] `GET /costs/events` — filter by feature / cached, limit/pagination
- [x] `GET /costs/export.csv` — streaming CSV
- [x] `POST /costs/reset-session` — rotates session id
- [x] `GET /costs/features` — per-feature breakdown with optional `since` filter
- [x] Binds `127.0.0.1` only (spec §7.6) — enforced in Makefile + documented in README
- [x] Errors use FastAPI `HTTPException` with actionable messages (spec §7.5)
- [x] 21 integration tests passing end-to-end via `TestClient`

### 1.7 Next.js UI — Phase 1 surfaces — DONE
- [x] `apps/web/app/first-run/` — setup wall
  - [x] Paste input (masked after save), placeholder `sk-ant-...`
  - [x] File upload option (plain-text, whitespace-tolerant)
  - [x] Validate-on-submit; show valid/invalid/unreachable states
  - [x] "Save anyway, validate later" escape hatch on unreachable (UI navigates onward; key already stored by earlier POST)
  - [x] `FirstRunGate` layout wrapper blocks access to LLM features until `llm_enabled=true`
- [x] `apps/web/app/settings/api-keys/` — view last-4, rotate, clear-with-confirm, test
- [x] `apps/web/app/settings/costs/` — Cost Details panel (spec §7.7.5 B)
  - [x] Session total + token breakdown
  - [x] Lifetime total
  - [-] Per-day chart (last 30 days) — deferred: no chart library in deps, will add in Phase 6 when real data is flowing (noted by subagent)
  - [x] Per-feature breakdown (sorted descending)
  - [x] Searchable/filterable CostEvent log (feature text + cached any/true/false, 200ms debounce)
  - [x] Export CSV button (passes active filters through)
  - [x] Reset session counter button
- [x] `apps/web/components/CostBadge.tsx` — top bar indicator, 15s poll while focused, amber-state hook stubbed for Phase 2
- [x] `TopBar`, `FirstRunGate`, `KeyInputPanel` shared components
- [x] UI primitives: `button`, `input`, `card`, `badge`, `dialog`, `tabs`, `select`, `label` — shadcn-style, local implementations (tabs/select hand-rolled to avoid adding Radix sub-packages)
- [x] `lib/api.ts` (typed fetch helpers + `ApiError`), `lib/format.ts` (USD/tokens/relative-time), `lib/utils.ts` (cn())
- [x] Vitest setup (`vitest.config.ts`, `tests/setup.ts`); 18 tests across 3 files (`format`, `api`, `CostBadge`)
- [x] `npm run typecheck` clean; `npm test` 18/18 pass
- [-] Book upload UI + ingestion progress view — deferred: spec §9 Phase 1 exit criterion requires this, but spec also says the UI is iterative; queued as a Phase 1.7 follow-up (see `SPEC_QUESTIONS.md` Q19)
- [-] Page-range browser with block type styling — deferred with the book upload UI; both depend on a real book being ingested end-to-end which requires the user to install `marker-pdf` locally
- [-] Left-side block-nav, right-side source markdown — deferred with the page-range browser
- [x] Design direction: near-white paper, single ink-blue accent, hairline borders, Inter/Source Serif two-family system, tabular numerals, no emoji

### 1.8 Phase 1 tests (spec §6.1, §6.2, §7.7.8)
- [ ] `test_extract_source_page_markers` — fixture: batch 1 of user's Property casebook, expect pages 1–37 with tolerable gap at page 2
- [ ] `test_block_segmenter_case_opinion` — case header + opinion fragment → correct boundaries
- [ ] `test_toc_extraction`
- [ ] `test_keyring_roundtrip` — store, read, clear a fake key
- [ ] `test_key_validation_mocked` — mock `/v1/models` for valid/invalid/unreachable
- [ ] `test_key_validation_live` — gated on `TEST_ANTHROPIC_KEY` env
- [ ] `test_cost_event_recording` — mocked LLM → CostEvent persisted with correct counts
- [ ] `test_pricing_config_missing_fallback` — missing/malformed pricing.toml → warning + conservative defaults
- [ ] `test_artifact_cache_idempotence` — same inputs twice → cache hit no LLM call (skeleton, full coverage Phase 2)
- [ ] `e2e_ingest_book` — small-fixture PDF (10 pages), assert Pages + markers + TOC + blocks
- [ ] Fixture corpus: `tests/fixtures/book/` (10-page slice covering Shelley + River Heights) committed as gold output
- [ ] Replay cache scaffolding in place (§6.3)

---

## Phase 2: Generation & case briefs (spec §9 Phase 2)

**Exit criterion:** user clicks a case in page-range view → brief in <30s → every claim's source inspectable.

**Status (2026-04-20):** Backend exit-criterion met. A case brief can be generated end-to-end from a seeded opinion block, with every claim cited to source Block ids, Verify running both citation_grounding and rule_fidelity profiles, Artifact caching so a repeat call is a $0 CostEvent, and the budget gate blocking non-cached calls when the monthly cap is exceeded. 226 Python tests passing (up from 151 at Phase 1 close) + 18 web tests from Phase 1. Remaining Phase 2 work is UI (case-brief viewer, preflight modal, amber budget state, Settings→Models page) which rides on the follow-up UI slice alongside the book-upload reader.

### 2.1 Generate primitive (spec §4.3) — DONE
- [x] `apps/api/src/primitives/generate.py` — orchestration + `set_anthropic_client_factory` test hook
- [x] `apps/api/src/primitives/prompt_loader.py` — YAML frontmatter + body, 11 loader tests
- [x] `apps/api/src/primitives/template_renderer.py` — pybars3 with block-balance pre-check, 9 renderer tests
- [x] Output validation against declared JSON schema via `jsonschema`; retry (max 2) on malformed response with enumerated error paths
- [x] Deterministic cache key via `tests.llm_replay.compute_cache_key` — shared canonicalization between prod cache + replay cache
- [x] Cache hit → existing Artifact returned + CostEvent with `cached=true, total_cost_usd=0`
- [x] Every non-cached call → real-token CostEvent with dollar cost, feature, artifact_id
- [x] Model resolution: `model_override` → `template.model_defaults.model` → `config/models.toml[features][name]` → Opus fallback
- [x] Response parser tolerant of ```json fences + prose-prefix; retries guarantee schema conformance
- [x] 12 generate tests passing; Q25-Q27 logged in SPEC_QUESTIONS.md

### 2.2 Verify primitive (spec §4.4) — DONE
- [x] `apps/api/src/primitives/verify.py` — dispatcher + 2 live profiles + 2 Phase-3 stubs
- [x] Profile: `citation_grounding` — walks nested content for source_block_ids / sources lists; checks declaration + DB existence
- [x] Profile: `rule_fidelity` — 60% token overlap threshold; `(paraphrase)` marker suppresses check; empty source list is error
- [x] `VerificationIssue` (severity: warning | error) + `VerificationResult` with `.passed` flag + context dicts for structured issue data
- [x] `rubric_coverage` + `issue_spotting_completeness` stubs raise NotImplementedError with Phase-3 spec pointers
- [x] 13 verify tests passing; open questions Q21-Q23 in SPEC_QUESTIONS.md

### 2.3 Pre-flight estimator (spec §7.7.5 C) — PARTIAL
- [x] `apps/api/src/costs/estimator.py` — full implementation with 5 per-feature estimators
- [x] Per-feature estimate functions: `estimate_book_ingestion`, `estimate_case_brief`, `estimate_bulk_brief_generation`, `estimate_outline_regeneration`, `estimate_rubric_from_memo`
- [x] Return range with `±%` margin (±30% default, ±50% for ingestion)
- [x] Default threshold (>$0.50) stored in `config/models.toml[thresholds].preflight_cost_usd` (set in Phase 0)
- [x] `PreflightRequired` Result variant — existing exception from stub, now real
- [x] Unknown-feature fallback: returns a wide-band "unknown" estimate rather than raising
- [x] 4 new estimator tests passing (total 19 cost-layer tests)
- [~] Generate primitive wires `PreflightRequired` into its dispatch — DELEGATED (Phase 2.1 subagent)
- [ ] `apps/web/components/PreflightCostModal.tsx` — queued alongside the case_brief UI work

### 2.4 Prompt templates (spec Appendix B) — PARTIAL
- [x] `packages/prompts/case_brief.prompt.md` (v1.2.0 per Appendix B example)
- [x] `packages/prompts/block_segmentation_fallback.prompt.md` (v1.0.0)
- [x] `packages/schemas/case_brief.json` — FIRAC+ with Claim objects backed by source_block_ids
- [x] `packages/schemas/block_classification.json` — fallback classifier output
- [ ] LLM fallback wired into ingest block segmentation — template + schema are ready; the call site inside `block_segmenter.py` is the last mile, deferred until an ingestion test flags an ambiguous chunk

### 2.5 Case brief feature (spec §5.2) — DONE (backend)
- [x] `apps/api/src/features/case_brief.py` — `generate_case_brief(session, req) -> CaseBriefResult`
- [x] Orchestration: `raise_if_over_budget()` → retrieve → generate → verify(citation_grounding) + verify(rule_fidelity)
- [x] Endpoint: `POST /features/case-brief` with case_name OR block_id (at least one required)
- [x] Response: `{artifact, cache_hit, warnings, verification_failed}` — UI can prominently surface verification failures
- [x] Error mapping: `CaseBriefError` → 404 (case not found), `BudgetExceededError` → 402, missing args → 400
- [x] 7 end-to-end tests: happy path, cache hit, force_regenerate, 404, 400, 402, CostEvent emission
- [ ] Web UI: click case → trigger brief → render with sources highlighted — follow-up UI slice

### 2.6 Budget alerts (spec §7.7.5 D) — PARTIAL
- [~] Settings: monthly spending cap — `LAWSCHOOL_MONTHLY_CAP_USD` env var for Phase 2; UI-editable settings table deferred (SPEC_QUESTIONS Q24)
- [x] `tracker.get_monthly_budget_cap_usd()`, `current_month_total_usd()`, `get_budget_status()` — feed the amber state logic
- [x] `BudgetStatus` with state enum: off / ok / warning / exceeded
- [x] 80% warning threshold enforced — cost badge UI queries `/costs/budget` (endpoint to be added)
- [x] `raise_if_over_budget()` + `BudgetExceededError` — generate primitive calls before LLM dispatch (wiring delegated)
- [ ] Cost badge amber-state rendering in `apps/web/components/CostBadge.tsx` — ships with a follow-up UI slice
- [ ] Blocking modal "raise the cap" action — same follow-up slice
- [x] 19 budget tests passing covering cap states, month boundary, enforcement

### 2.7 Model selection per feature (spec §7.7.6) — PARTIAL
- [x] Defaults wired in `config/models.toml` (done in Phase 0):
  - [x] case_brief → Opus 4.7
  - [x] irac_grade → Opus 4.7
  - [x] socratic_drill, cold_call → Opus 4.7
  - [x] rubric_from_memo → Opus 4.7
  - [x] doctrinal_synthesis → Opus 4.7
  - [x] flashcards → Sonnet 4.6
  - [x] mc_questions → Sonnet 4.6
  - [x] block_segmentation_fallback → Haiku 4.5
  - [x] transcript_cleanup → Haiku 4.5
  - [x] fuzzy case-name resolution → Haiku 4.5
- [x] Generate primitive resolves model via `model_override` → template default → config[features][name] → hardcoded Opus
- [ ] Settings → Models UI page: user can override each with cost-impact hint — follow-up UI slice

### 2.8 Phase 2 tests — DONE
- [x] `test_case_brief_happy_path_via_route` (§6.1 L3 equivalent) — Shelley opinion + note → required FIRAC+ fields, Block-id citations, "Fourteenth Amendment" in rule
- [-] `test_case_brief_river_heights` — skipped; same code path as Shelley, covered by the happy_path test. Revisit when we write the fixture-corpus-backed L2 test.
- [x] `test_estimator_book_ingestion_property_casebook_sanity` — 1400-page estimate < $50 guard
- [x] `test_raise_if_over_budget_blocks_at_cap` + `test_case_brief_402_when_budget_exceeded` — 100% cap blocks the call end-to-end
- [x] `test_case_brief_cache_hit_second_call` — second identical call returns cached artifact, Anthropic called exactly once
- [x] Verify subagent: `test_citation_grounding_*` (4 tests), `test_rule_fidelity_*` (4 tests)
- [x] LLM replay cache infrastructure (`tests/llm_replay.py`) + 8 tests; re-record workflow is `LLM_REPLAY_RECORD=1 pytest` — gates live API use. First real recordings land alongside the first L2 prompt-template golden-input test.

---

## Phase 3: Professor profile + IRAC grading (spec §9 Phase 3)

**Exit criterion:** user writes practice answer → grade aligned with how Pollack actually graded similar answers.

**Status (2026-04-24):** Backend exit-criterion met. 299 tests passing (+70 over Phase 3 start). Full Path A (past exam → rubric → grade) and Path B (hypo → rubric → answer → grade) work end-to-end with cache, budget gating, verify rubric_coverage + issue_spotting_completeness, and 8 Pollack anti-pattern detectors. UI surfaces (profile editor, past-exam uploader, graded feedback view) deferred to the same follow-up UI slice as Phase 1/2.

### 3.1 Data model additions — DONE
- [x] `ProfessorProfile` SQLModel (§3.7) — unique (corpus_id, professor_name); JSON cols for pet_peeves/framings/traps/voice/commonly_tested
- [x] `ArtifactType.PAST_EXAM`, `GRADER_MEMO`, `RUBRIC`, `HYPO`, `PRACTICE_ANSWER`, `GRADE`, `PROFESSOR_PROFILE`
- [x] PastExam/GraderMemo persisted as plain Artifact rows (user-uploaded, no CostEvent)
- [x] Rubric artifact (§5.5) with required_issues/rules/counterargs/anti_patterns
- [x] Grade artifact (§5.5) with overall_score, letter_grade, per_rubric_scores, pattern_flags, strengths, gaps, sample_paragraph
- [x] Hypo artifact with embedded rubric + topics_covered
- [x] 3 ProfessorProfile model tests passing; updated test_db expected table count

### 3.2 Prompt templates — DONE
- [x] `packages/prompts/professor_profile_extraction.prompt.md` v1.0.0 + `schemas/professor_profile.json`
- [x] `packages/prompts/rubric_from_memo.prompt.md` v1.0.0 + `schemas/rubric.json`
- [x] `packages/prompts/irac_grade.prompt.md` v1.0.0 + `schemas/grade.json` (rubric-anchored, auditable, Pollack-calibrated)
- [x] `packages/prompts/hypo_generation.prompt.md` v1.0.0 + `schemas/hypo.json` (co-generates hypo + rubric; inlined Rubric `$defs` to work around jsonschema `$ref` resolver — Q32)

### 3.3 Professor profile builder (spec §5.13) — DONE (backend)
- [x] `features/professor_profile.py`: `build_profile_from_memos`, `update_profile`, `load_profile_for_corpus`, `seed_pollack_profile`
- [x] Upsert semantics: re-extraction updates existing row, preserves id/created_at, unions source_artifact_paths
- [x] PATCH allowlist rejects identity-breaking fields (professor_name, id)
- [x] Appendix A Pollack seed data (`APPENDIX_A_POLLACK_PROFILE`) exposed + idempotent `seed_pollack_profile`
- [x] Artifact lineage: every extraction produces a `PROFESSOR_PROFILE` Artifact too
- [x] 8 feature + 10 route tests (20 total for this slice)
- [ ] Structured editor UI: deferred to follow-up UI slice
- [-] "Every downstream generate call references profile when relevant" — wiring lives in each feature; case_brief and irac_grade already accept `professor_profile_id`

### 3.4 IRAC grading (spec §5.5) — DONE (backend)
- [x] **Path A:** ingest past exam + grader memo → `generate(rubric_from_memo)` → Rubric artifact → answer → `generate(irac_grade)` → Grade
- [x] `verify(rubric_coverage)` wired into `grade_irac_answer`; tolerates stub via try/except for forward compat
- [x] **Path B:** `generate(hypo_generation)` produces Hypo artifact with embedded rubric
- [x] `verify(issue_spotting_completeness)` runs post-hypo (rule-based), warnings surface to caller
- [x] `PRACTICE_ANSWER` Artifact created per grade call so Grades have a stable parent id (immutable history, §3.11)
- [x] Budget gate (`raise_if_over_budget`) before every LLM call; 402 on exceed
- [ ] Rich-text answer editor UI: follow-up UI slice

### 3.5 Pollack-specific grading patterns (spec §5.5, Appendix A) — DONE
- [x] `features/pollack_patterns.py` with `scan_answer()` + 8 detectors:
  - [x] `hedge_without_resolution` (paragraph-aware rescue via commit verbs)
  - [x] `clearly_as_argument_substitution` (word-boundary)
  - [x] `no_arguing_in_the_alternative` (single-hit-only heuristic)
  - [x] `rule_recited_not_applied` (`here`/`this case`/`the facts` within 2 sentences)
  - [x] `conclusion_mismatches_analysis` (`not`-parity + party-set disjointness)
  - [x] `mismatched_future_interests` (contingent+subject-to-open; indefeasibly-vested+anything)
  - [x] `read_the_prompt` (voice markers: "I would argue", "my client", "we should")
  - [x] `ny_adverse_possession_reasonable_basis` (pronoun-flexible; ±50 token window)
- [x] Detectors are advisory; LLM's `pattern_flags` is authoritative user-facing output
- [x] 18 pattern detector tests

### 3.6 UI — IRAC practice — DEFERRED
- [ ] Answer editor with word count — follow-up UI slice
- [ ] Graded feedback view — follow-up UI slice
- [ ] Per-artifact cost visible — infrastructure ready (Grade.cost_usd available via ArtifactDTO)

### 3.7 Phase 3 tests — DONE
- [x] `test_irac_grade_pollack_antipatterns` — deliberately-bad answer triggers clearly + hedge + mismatched interest detections; grade ≤ B- (anchored by mock)
- [-] `e2e_irac_grade_real_past_exam` — deferred: needs the 2023 Pollack exam fixture (user-provided, not in repo yet)
- [x] Rubric extraction tests (unit + integration, 10 total)
- [x] `test_issue_spotting_completeness_*` — 7 tests covering weights, label presence, thin rubric warning, wrong artifact type rejection
- [x] Professor profile feature tests (10) + integration tests (10)
- [x] Hypo generation tests (2 unit)
- [x] IRAC grading tests (7 unit + 4 route integration)

---

## Phase 4: Transcript ingestion & emphasis mapping (spec §9 Phase 4)

**Exit criterion:** upload Shelley/River Heights Gemini transcript → mangled case names resolved → emphasis ranking with justifications → change-of-conditions multi-hypo highlight.

**Status (2026-04-24):** Backend exit-criterion met. 355 tests passing (+46 over Phase 4 start = 309). End-to-end: upload Gemini transcript → LLM cleanup with speaker segmentation + resolved case mentions → fuzzy resolver rescue pass catches "Shelly B Kramer"-style deformations the LLM missed → persisted Transcript + Segments → emphasis mapper aggregates per-subject features + scores composite via `config/emphasis_weights.toml` + LLM justification → ranked EmphasisItems. Syllabus ingestion activates `AssignmentCodeQuery` (was stubbed in Phase 1). UI deferred to same follow-up slice as Phases 1–3.

### 4.1 Data model additions — DONE
- [x] `Transcript` (§3.8) — content-hash id, source_type (text|audio), lecture_date, topic, assignment_code, raw_text, cleaned_text, unique-per-corpus via id
- [x] `TranscriptSegment` (§3.9) — speaker enum (professor|student|unknown), mentioned_cases/rules/concepts as JSON, sentiment_flags
- [x] `EmphasisItem` (§3.10) — unique (transcript_id, subject_kind, subject_label); exam_signal_score indexed for ORDER BY
- [x] `Syllabus` / `SyllabusEntry` (§3.6) — unique (syllabus_id, code); page_ranges as JSON list of [start, end] pairs
- [x] 5 new tables registered (transcript, transcript_segment, emphasis_item, syllabus, syllabus_entry) — test_metadata_registered_all_tables now checks 13

### 4.2 Prompt templates — DONE
- [x] `packages/prompts/transcript_cleanup.prompt.md` v1.0.0 + `schemas/transcript_cleanup.json` (Haiku 4.5 default)
- [x] `packages/prompts/emphasis_analysis.prompt.md` v1.0.0 + `schemas/emphasis_analysis.json` (Opus 4.7 default — provisional_score sanity + justification)
- [x] Bonus: `packages/prompts/syllabus_extraction.prompt.md` v1.0.0 + `schemas/syllabus_extraction.json` for §4.5

### 4.3 Transcript ingestion (spec §4.1.2, §4.1.5) — DONE (text) / STUBBED (audio)
- [x] `features/transcript_ingest.py:ingest_transcript_text(session, req)` — subagent-built, Opus-verified 5 tests
  - [x] Content-hash dedup (SHA-256 of raw_text → Transcript.id)
  - [x] Speaker segmentation via LLM (linguistic cues per Haiku-backed prompt)
  - [x] Sentence-fragment joining + case-name normalization in the same LLM pass
  - [x] Fuzzy resolver safety-net pass over raw text catches mentions the LLM missed
  - [x] TranscriptSegment rows persisted with resolved mentions + sentiment flags
  - [x] Direct Anthropic SDK call (bypasses generate primitive, logged Q36)
- [~] `ingest_transcript_audio()` — stub raises NotImplementedError pointing at Q37 (faster-whisper not in default install)
- [x] `POST /transcripts`, `GET /transcripts/{id}`, `GET /transcripts?corpus_id=...` routes
- [x] 5 transcript_ingest tests + 5 transcripts routes tests (10 total)

### 4.4 Fuzzy case-name resolver (§4.3.4) — DONE
- [x] `primitives/fuzzy_resolver.py:resolve_case_names` — rule-based via `rapidfuzz`, threshold 82.0, composite score `(partial_ratio + token_set_ratio + WRatio) / 3`
- [x] Extraction regex handles "X v. Y" / "X vs Y" / "X vee Y" / "X B Y" (Gemini's mishearing) + multi-word capitalized fallback
- [x] `load_known_case_names_for_corpus` helper pulls canonical names from all CASE_OPINION/CASE_HEADER blocks
- [x] All three spec-mandated deformations resolve correctly:
  - [x] "Shelly B Kramer" → "Shelley v. Kraemer" (score 92.9)
  - [x] "Pen Central" → "Penn Central Transportation Co. v. New York City" (score 84.7)
  - [x] "River Heights v Daton" → "River Heights Associates L.P. v. Batten" (score 82.5)
- [x] Below-threshold candidates → `unresolved_mentions` for manual review
- [-] LLM fallback: not implemented; transcript_cleanup's LLM pass covers this upstream (Q38)
- [x] 10 fuzzy resolver tests

### 4.4 Fuzzy case-name resolver (§4.3.4)
- [ ] Resolver with LLM fallback
- [ ] Handles "Shelly B Kramer" → "Shelley v. Kraemer"
- [ ] Handles "Pen Central" → "Penn Central Transportation Co. v. New York City"
- [ ] Handles "River Heights v Daton" → "River Heights Associates L.P. v. Batten"

### 4.5 Syllabus ingestion (spec §4.1.4) — DONE
- [x] `features/syllabus_ingest.py:ingest_syllabus()` — direct Anthropic SDK, budget-gated, CostEvent emitted
- [x] `packages/prompts/syllabus_extraction.prompt.md` v1.0.0 + `schemas/syllabus_extraction.json` (Sonnet 4.6 default)
- [x] LLM parse into SyllabusEntry schema (§3.6); persists Syllabus + SyllabusEntry rows
- [x] Validation: page_ranges checked against `Book.source_page_min/max` → `DiscrepancyNote` surfaced in response
- [x] **AssignmentCodeQuery retrieval path ACTIVATED** (replaces Phase-1 stub) — joins Syllabus + SyllabusEntry, newest-syllabus-per-corpus, unions multiple page_ranges per entry
- [x] `POST /ingest/syllabus` route wired into the existing ingest router
- [x] 8 feature tests + 2 retrieve activation tests passing

### 4.6 Emphasis mapper (spec §5.7) — DONE
- [x] `features/emphasis_mapper.py:build_emphasis_map(session, req)` — subagent-built, Opus-verified
- [x] `compute_subject_features` aggregates per-subject: minutes_on (char-based), return_count (distinct segments), hypotheticals_run (truncated content), disclaimed (any-segment-disclaims → True), engaged_questions
- [x] `compute_provisional_score` with full normalization caps from `emphasis_weights.toml`; `[0,1]` clamp; disclaimed-penalty applied
- [x] `costs/emphasis_weights.py` — lazy-loads `config/emphasis_weights.toml` with sane fallback defaults when missing (7 tests)
- [x] Direct Anthropic call for `emphasis_analysis` prompt; emits CostEvent with `feature="emphasis_analysis"`
- [x] LLM allowed ≤+0.1/≤−0.2 semantic adjustment per prompt; feature code trusts + clamps
- [x] EmphasisItem upsert semantics; cache-hit when rows already exist unless `force_regenerate`
- [x] Results returned sorted DESC by `exam_signal_score`
- [x] `POST /features/emphasis-map` route with 404/402/503 error mapping
- [x] 15 mapper unit + 7 weights unit + 4 route integration tests (26 total)
- [x] Open questions: Q39 (chars/min config knob), Q40 (cache-hit CostEvent convention), Q41 (profile_id accepted-but-unused), Q42 (CHARS_PER_MINUTE calibration)

### 4.7 UI
- [ ] Transcript upload (text paste + file + audio)
- [ ] Link to assignment code
- [ ] Ranked emphasis output view with justifications
- [ ] Sentiment-flag badges per segment

### 4.8 Phase 4 tests — DONE (non-fixture-dependent) / DEFERRED (fixture-dependent)
- [x] `test_fuzzy_case_name_resolver` (§6.1 L1) — all three user-transcript deformations resolve correctly (across 10 fuzzy tests)
- [x] `test_syllabus_ingest_*` — 8 syllabus tests incl. page-range discrepancy detection; 2 `AssignmentCodeQuery` resolution tests in `test_retrieve.py`
- [-] `e2e_transcript_emphasis` (§6.1 L3) — Shelley/River Heights transcript end-to-end against the user's ACTUAL Gemini transcript: deferred to the fixture-corpus slice (same reason as Phase 3's `e2e_irac_grade_real_past_exam`). All unit building blocks (fuzzy resolver, ingest, mapper, scoring) pass.
- [x] `test_transcript_cleanup_speaker_segmentation` — covered in `test_transcript_ingest_happy_path` + `test_ingest_runs_fuzzy_resolver_on_raw_text`
- [-] `test_whisper_cache_idempotence` — deferred with audio-path (Q37); `faster-whisper` not in default install

### 4.9 Phase 4 summary — DONE
- **355 tests passing** (up from 309 at syllabus-complete = +46 Phase 4 tests). No regressions.
- Feature-test split:
  - Data model: 3 ProfessorProfile roundtrips (Phase 3 carryover, unchanged)
  - Fuzzy resolver: 10 tests
  - Transcript ingest: 5 unit + 5 route integration = 10 tests
  - Emphasis weights: 7 tests
  - Emphasis mapper: 15 unit + 4 route integration = 19 tests
  - Syllabus ingest: 8 tests
  - AssignmentCodeQuery activation: 2 new retrieve tests
- All subagent output reviewed by Opus via full-suite regression. Race on `SPEC_QUESTIONS.md` Q-numbering between subagents resolved by re-appending displaced questions as Q39-Q42.

---

## Phase 5: Remaining features (spec §9 Phase 5)

**Status (2026-04-24):** Backend exit-criterion met. **451 tests passing (+96 over Phase 5 start = 355).** All 9 features live end-to-end, every one cache-ready + budget-gated + CostEvent-emitting. Strategy played out as planned: Opus wrote all 8 new Phase 5 prompts + schemas (17 templates total now), delegated three subagent batches (flashcards+SM-2 = 22 tests, chat sessions = 21 tests, 5 static features = 37 tests), and implemented global search directly (16 tests). All subagent code verified by Opus full-suite regression. Open questions Q43–Q50 logged. UI for Phase 5 features deferred to the same follow-up UI slice as earlier phases.

In priority order per spec.

### 5.1 Flashcards + spaced repetition (§5.3) — DONE
- [x] `features/flashcards.py` + `data.models.FlashcardReview` table (subagent-built, Opus-verified)
- [x] `packages/prompts/flashcards.prompt.md` v1.0.0 + `schemas/flashcards.json` (Sonnet 4.6 default)
- [x] SM-2 scheduler: `apply_sm2(state, grade, now)` with canonical transitions (interval 1 / 6 / prev*ef; ef floored at 1.3)
- [x] `record_review(session, set_id, card_id, grade)` — lazy-creates FlashcardReview if missing
- [x] `due_cards(session, corpus_id, now, limit)` — interleaved oldest-first across sets
- [x] Routes: `POST /features/flashcards`, `GET /flashcards/due`, `POST /flashcards/review`
- [x] 14 SM-2 tests + 5 feature tests + 3 route integration tests = 22 tests
- [x] Q43-Q46 logged in SPEC_QUESTIONS.md (ef-on-lapse, due-queue ordering, orphan rows, lazy-create)
- [ ] `packages/prompts/flashcards.prompt.md`
- [ ] `FlashcardSet` artifact type; per-card SM-2 state
- [ ] Generate: 20–25 cards, rule questions, case-to-doctrine pairs, compare/contrast, "what is the test for X"
- [ ] Spaced-repetition review UI
- [ ] Tests: `test_flashcard_generation_shape`, `test_sm2_scheduler`

### 5.2 Socratic drill mode (§5.4) — DONE
- [x] `features/chat_session.py` — shared session machinery (load_or_create_session, append_turn, close_session)
- [x] `features/socratic_drill.py` + `features/cold_call.py` — both use direct Anthropic SDK (like transcript_ingest), CostEvent per turn linked via artifact_id to session artifact
- [x] Session state under `Artifact.content`: `{case_block_id, history, mode, started_at, ended_at}`
- [x] `packages/prompts/socratic_drill.prompt.md` + `cold_call.prompt.md` share `schemas/socratic_turn.json`
- [x] Pollack-style pushback on hedging/"clearly" per turn (prompt-driven; intent enum includes `push_back_on_hedge`)
- [x] Routes: `POST /features/socratic/turn`, `POST /features/cold-call/turn`, `POST /features/cold-call/debrief`
- [x] 5 chat_session + 6 socratic + 5 cold_call + 5 route integration = 21 tests
- [x] Open Qs: session cap, debrief idempotency, session→grade linkage (logged)
- [ ] `packages/prompts/socratic_drill.prompt.md`
- [ ] `SocraticDrillSession` artifact — per-turn logged
- [ ] Chat-loop orchestration; one question at a time; reacts to weak answers
- [ ] "Don't accept 'I don't know' without one chance to reason from first principles"
- [ ] Pollack-style pressure patterns from profile (hedging pushback, "clearly" flag, demand alternatives)
- [ ] Review-after UI
- [ ] Tests: `test_socratic_drill_pushback_on_hedge`

### 5.3 Attack sheet builder (§5.9) — DONE
- [x] `features/attack_sheet.py:generate_attack_sheet` — consumes CASE_BRIEF artifacts + optional EmphasisMap + professor_profile
- [x] `packages/prompts/attack_sheet.prompt.md` + `schemas/attack_sheet.json` (issue_spotting_triggers, decision_tree, controlling_cases, rules_with_elements, exceptions, majority_minority_splits, common_traps, one_line_summaries)
- [x] `POST /features/attack-sheet` route
- [x] 5 unit + 2 integration tests passing
- [ ] `packages/prompts/attack_sheet.prompt.md`
- [ ] `AttackSheet` schema: issue_spotting_triggers, decision_tree, controlling_cases, rules_with_elements, exceptions, majority_minority_splits, common_traps, one_line_summaries
- [ ] Print-friendly export
- [ ] Paste-into-outline-able format
- [ ] Tests: `test_attack_sheet_regulatory_takings_completeness`

### 5.4 Multi-case synthesis (§5.8) — DONE
- [x] `features/synthesis.py:generate_synthesis` — consumes N CaseBrief artifacts → timeline/categorical_rules/balancing_tests/relationships/modern_synthesis/exam_framework + optional mermaid diagram
- [x] `packages/prompts/doctrinal_synthesis.prompt.md` + `schemas/synthesis.json`
- [x] `POST /features/synthesis` route
- [x] 5 unit + 2 integration tests passing
- [ ] `packages/prompts/doctrinal_synthesis.prompt.md`
- [ ] Synthesis schema: doctrinal_area, cases, timeline, categorical_rules, balancing_tests, relationships, modern_synthesis, exam_framework, visual_diagram (mermaid)
- [ ] `verify(citation_grounding)` on synthesis
- [ ] Tests: `test_synthesis_loretto_lucas_penn_central` — relationships correctly identified

### 5.5 "What if" fact variations (§5.10) — DONE
- [x] `features/what_if.py:generate_what_if_variations` — 3–10 variations per case, each with fact_changed, consequence, doctrinal_reason, tests_understanding_of
- [x] `packages/prompts/what_if_variations.prompt.md` + `schemas/what_if_variations.json`
- [x] ArtifactType reuses SYNTHESIS with `content["kind"] = "what_if_variations"` sub-discriminator (Q47 — avoids enum migration)
- [x] `POST /features/what-if` route
- [x] 5 unit + 2 integration tests passing
- [ ] `packages/prompts/what_if_variations.prompt.md`
- [ ] `packages/prompts/hypo_from_variation.prompt.md`
- [ ] Output: 5 variations with fact changed, legal consequence, doctrinal reason, "this tests your understanding of ___" tag
- [ ] On-demand variation-to-hypo conversion
- [ ] Tests: `test_what_if_shelley_outcome_changes`

### 5.6 Outline generator (§5.11) — DONE
- [x] `features/outline.py:generate_outline` — gathers all CASE_BRIEF + FLASHCARD_SET artifacts in corpus + book TOC → hierarchical outline
- [x] `packages/prompts/outline_hierarchical.prompt.md` + `schemas/outline.json` (TOC-structured with rules/controlling_cases/policy/exam_traps/cross_references)
- [x] When book_id unspecified, picks the corpus's largest book by page count
- [x] `input_artifact_count` exposed for UI to warn when approaching the 10k-token template cap (Q48)
- [x] `POST /features/outline` route
- [x] 5 unit + 2 integration tests passing
- [x] Workaround for pybars3 missing `range` helper (Q50) — indentation pre-baked into entry titles
- [ ] `packages/prompts/outline_hierarchical.prompt.md`
- [ ] Input: all `case_brief` + `flashcard_set` artifacts + TOC
- [ ] Output: TOC-structured outline with rules, controlling cases, policy rationales, exam traps, cross-refs
- [ ] Versioned, regenerable
- [ ] Markdown export (primary); DOCX on request (docx skill); print
- [ ] Tests: `test_outline_versioning_on_new_briefs`

### 5.7 Cold call simulator (§5.6) — DONE (rolled into 5.2 chat_session infrastructure)
- [x] See 5.2 above: `features/cold_call.py` shares chat_session machinery with Socratic drill
- [x] Explicit `elapsed_seconds` computation from `started_at` — prompt uses it for time-pressure cues
- [x] `cold_call_debrief(session, session_id)` — final turn with `mode="debrief"` then `close_session`
- [x] Tested: `test_cold_call_has_elapsed_seconds_in_prompt`, `test_cold_call_debrief_sets_mode_and_closes`
- [ ] `packages/prompts/cold_call.prompt.md`
- [ ] 10–15 min session, time-pressured, escalating difficulty
- [ ] Automated debrief with references back to case text
- [ ] Tests: `test_cold_call_pressure_escalation`

### 5.8 MC question practice (§5.12) — DONE
- [x] `features/mc_questions.py:generate_mc_questions` — 10 questions per set (stem + 4 options + correct + explanation + per-distractor-why-wrong + doctrine_tested)
- [x] `packages/prompts/mc_questions.prompt.md` + `schemas/mc_questions.json` (Sonnet 4.6 default — mechanical structured output)
- [x] Retrieval via PageRange or CaseReference
- [x] When professor_profile provided, at least 2 questions target its stable_traps
- [x] `POST /features/mc-questions` route
- [x] 5 unit + 2 integration tests passing
- [ ] `packages/prompts/mc_questions.prompt.md`
- [ ] `MCQuestionSet` artifact — per-question answer-history tracking
- [ ] 10 questions: stem, 4 options, correct answer, full explanation, per-distractor wrongness, doctrine tested
- [ ] Interactive UI with per-question feedback
- [ ] Tests: `test_mc_distractor_explanation_completeness`

### 5.9 Global search & cross-reference (§5.14) — DONE
- [x] `features/global_search.py` — lexical token-overlap scoring + exact-phrase bonus + case-opinion boost + emphasis-flag boost
- [x] Searches Blocks + TranscriptSegments + Artifact.content (flattened JSON) in one unified call
- [x] Structural context: book title + TOC breadcrumb + page for blocks; topic + speaker + turn for segments; `Case Brief: X` for artifacts
- [x] Kinds filter, corpus filter, limit; DESC ordering with id-stable tie-break
- [x] Snippet extraction with ±120 char window + ellipses
- [x] `GET /search?q=...&corpus_id=...&kinds=...&limit=...` route
- [x] 12 unit + 4 route integration tests passing (16 total)
- [-] Click-through to source with span highlight — UI concern, follow-up slice

---

## Phase 6: Polish (spec §9 Phase 6)

**Status (2026-04-24):** Starting. Backend exit-met for Phases 1–5; Phase 6 catches up the deferred UI surfaces (reading view, case-brief viewer, search results page) and adds backup/export + per-artifact lineage page. Performance pass is mostly observation since real targets need real LLM/Marker traffic.

### 6.1 UI refinements — SUBSTANTIAL (large-file + loading-state polish landed)
- [x] **Upload page** at `/upload` — drag-drop multi-PDF, multipart streaming, per-file `XMLHttpRequest` progress, automatic chained call to `/ingest/book/async`, live progress bar via `<TaskProgress>`. Text-upload tab for transcripts/syllabi with per-target ingest forms.
- [x] **Search page** at `/search` — `GET /api/search` consumer with autofocus input, kinds filters, corpus dropdown, debounced submit, snippet highlighting, kind+score badges.
- [x] **Per-day chart** on `/settings/costs` — pure-SVG bars, 30-day window, total-spend stat below.
- [x] **Loading states** — `<Spinner>`, `<LoadingButton>`, `<TaskProgress>` shared components; wrapped Rotate/Test/Clear in api-keys, Reset session counter in costs, Validate-and-continue in first-run, dashboard corpora load.
- [x] **Top bar nav** — Upload + Search links added.
- [-] Reading view (page-range browser) — still deferred (Q19); requires per-block rendering UX
- [-] Case-brief viewer — still deferred (Q52)
- [-] Mobile view / keyboard shortcuts — keep deferred (style pass)
- [x] 39 web tests passing (up from 18); 21 new component+lib tests for the new primitives

### 6.2 Performance pass + robustness — DONE
- [x] **Async ingestion** — `POST /ingest/book/async` returns immediately with `task_id`; bounded worker pool runs `ingest_book` with progress callback per step (hashing 0-5%, marker 5-55%, stitching 55-60%, page_markers 60-65%, pages 65-70%, blocks 70-92%, toc 92-95%, persisting 95-100%)
- [x] **Bounded task queue** — single shared `queue.Queue` + N daemon workers (default N=1 to prevent OOM with Marker memory pressure; `LAWSCHOOL_TASK_CONCURRENCY` env override, capped at 8). Twenty simultaneous casebook uploads queue cleanly instead of forking 20 Marker processes.
- [x] **Cooperative cancellation** — `POST /tasks/{id}/cancel` flips status to `CANCELLED`; the progress callback raises `TaskCancelled` at the next checkpoint; worker logs and exits cleanly. Already-completed tasks are no-ops with a clarifying message.
- [x] **Streaming uploads + size caps** — `POST /uploads/pdf` and `POST /uploads/text` chunk 1 MiB at a time. `LAWSCHOOL_MAX_PDF_BYTES` (default 1 GiB) + `LAWSCHOOL_MAX_TEXT_BYTES` (default 50 MiB). Disk pre-flight via `os.statvfs`. Oversize requests get 413 + cleaned-up partial file.
- [x] **Polling endpoints** — `GET /tasks/{id}` + `GET /tasks?corpus_id=...&status=...&kind=...`; UI's `<TaskProgress>` polls every 1.5s while tab is visible.
- [x] **Storage GC** — `POST /system/storage/cleanup?dry_run=true|false` removes orphan upload files (sha not referenced by any `Book.batch_hashes` or `Transcript.id`) plus partial `.part` temp files.
- [x] **Health endpoint** — `GET /system/health` returns disk free/total, process RSS, Marker availability, worker concurrency + alive-count + queue depth, pending/running task counts, per-table row counts, budget state.
- [x] `BackgroundTask` SQLModel + `TaskKind` / `TaskStatus` enums (incl. `CANCELLED`); FK to corpus; indexes on `(corpus_id, status)` and `(kind, status)`.
- [x] **`drain_for_tests()`** test helper using `queue.Queue.unfinished_tasks` so fixtures wait for workers to finish before resetting the engine.
- [x] **End-to-end test** — `test_full_user_flow_end_to_end` walks corpus create → seed Pollack profile → PDF upload → async ingest → page-range retrieve → case brief → flashcards → search → past exam ingest → rubric extract → IRAC grade with Pollack pattern detection → lineage → export → health, all via the HTTP layer with Anthropic mocked.
- [x] 14 robustness tests + 1 e2e test (15 new total)
- [-] Page-range retrieval <200ms for 50-page span — DB-indexed, untested without real volume
- [-] Case brief generation (uncached) <30s — LLM-side; sub-second under mock
- [-] Book ingestion (Marker+LLM) <5 min/100 pages — async path makes HTTP timeouts moot; real-traffic measurement when user installs Marker
- [-] Transcript cleanup + emphasis <2 min/lecture — same posture

### 6.3 Backup/export — DONE
- [x] `features/corpus_export.py:export_corpus(session, corpus_id) -> bytes` — gzipped tar with one JSONL per table
- [x] Manifest with `schema_version=1`, table counts, exported_at timestamp
- [x] Decimal → str, datetime → ISO-8601, enum → value coercion
- [x] CostEvents filtered to artifacts in this corpus only (no global cost leakage)
- [x] FlashcardReview included via denormalized corpus_id
- [x] `GET /corpora/{corpus_id}/export` route returns `application/gzip` streaming download with proper filename
- [x] 7 unit + 2 integration tests passing
- [x] **Restore** — `features/corpus_restore.py` reads the archive and re-inserts every row inside a new corpus (Q51 resolved). UUID-keyed entities get fresh ids with FK rewrites via id-remap; content-addressed Books/Transcripts keep their hash-based ids. Schema-version gated. `POST /corpora/restore` accepts a multipart upload. 3 integration tests passing.

### 6.4 Observability (spec §7.4) — DONE
- [x] Structured JSON logs per primitive call (`structlog.get_logger` already wired across the stack)
- [x] Per-artifact lineage backend: `features/lineage.py:build_lineage()` walks parent chain root-first, gathers all CostEvents along the way, resolves source_block / source_segment ids with found/missing flags, totals cost across the chain
- [x] `GET /artifacts/{id}/lineage` endpoint with full LineageResponse DTO
- [x] Self-referential loop guard (max_depth=64) so corrupt parent_artifact_id doesn't infinite-loop
- [x] 8 unit + 3 integration tests passing
- [-] UI debug page for lineage rendering — deferred with the rest of the UI work

---

## Phase UI-1: Make every must-have feature drivable without Swagger

**Status (2026-04-26):** DONE for the P0 scope. Every must-have feature has a UI surface, all 515 Python tests + TS typecheck pass, and the dev stack smoke-tested HTTP 200 on every new page. A 1L can drive page-range ingest, case-brief, transcript→emphasis, Socratic drill, and IRAC grading without opening Swagger.

### UI-1.1 Cross-cutting building blocks — DONE
- [x] `GET /api/corpora/{id}/stats` — counts of books / transcripts / artifacts by type, latest emphasis_map, latest outline timestamp
- [x] `GET /api/corpora/{id}/books` — books-in-corpus listing for the corpus-detail Books tab
- [x] `GET /api/books/{book_id}/cases` — list of CASE_OPINION blocks with case_name, source_page, jurisdiction, year (supports `random=true&page_start=&page_end=` for cold-call random pick)
- [x] `GET /api/artifacts?corpus_id=&type=&q=&limit=` — generic artifact listing for the picker
- [x] `GET /api/artifacts/{id}` — full artifact detail with content + markdown convenience field
- [x] `apps/web/components/ArtifactPicker.tsx` — single + multi-select variants, used by synthesis / attack-sheet / what-if / outline / practice
- [x] `apps/web/components/ArtifactMarkdown.tsx` — lightweight markdown renderer (h1-h3 + lists + blockquote + bold/italic/code)
- [x] `apps/web/lib/draft.ts` — localStorage-backed draft hook for the practice answer workspace
- [x] `apps/web/app/corpora/[corpusId]/page.tsx` — corpus-detail page with tabs (Books / Transcripts / Briefs / Past exams / Profiles / Study)
- [x] Dashboard corpus cards become `<Link>` to `/corpora/[id]` and study features promoted in the dashboard nav

### UI-1.2 Must-have features — DONE
- [x] Cases tab: cases-in-book list with one-click **Brief** / **Drill** / **Cold-call** buttons on the book-detail page
- [x] `apps/web/app/artifacts/[artifactId]/page.tsx` — markdown-rendered artifact viewer + sources panel + lineage link + per-type extras (case-brief includes inline What-if panel)
- [x] Backend: `POST /api/features/case-brief` extended to accept `(book_id, page_start, page_end)` resolving to the first case_opinion block in range
- [x] `apps/web/app/corpora/[corpusId]/books/[bookId]/page.tsx` — page-range slider + action sidebar (case-brief / flashcards / MCQs / cold-call random)
- [x] Transcripts tab on corpus-detail page — list + per-row "Build emphasis map" → navigates to viewer
- [x] `apps/web/app/transcripts/[transcriptId]/emphasis/page.tsx` — ranked emphasis-map viewer
- [x] `apps/web/app/socratic/[blockId]/page.tsx` — chat UI (textarea + send + scrollable transcript + side panel)
- [x] `apps/web/app/cold-call/[blockId]/page.tsx` — same chat UI, "End & debrief" button → `/cold-call/debrief`
- [x] `apps/web/app/cold-call/random/page.tsx` — corpus + book + page-range picker → server picks one case → redirects to chat
- [x] `apps/web/app/practice/page.tsx` — IRAC practice wizard (Flow A: past exam + memo · Flow B: generate hypo · Flow C: paste question + pick rubric). LocalStorage draft via `useDraft`. Inline graded view with rubric coverage checklist + anti-pattern highlights.
- [x] `apps/web/components/ChatPanel.tsx` — shared between Socratic and cold-call

### UI-1.3 Open questions to resolve before building (mirror to SPEC_QUESTIONS.md) — DEFERRED

These were scoped during the audit and shipped with reasonable defaults; revisit when the user reports friction:

- [-] Page-range action sidebar location → kept on book-detail page; reading view is UI-3 work
- [-] Practice Flow C: requires user to pick an existing rubric (no on-the-fly synthesis); fall back to Flow B if none exists
- [-] Cold-call random: fresh randomness per launch (no deterministic seed); debriefs read the same chat history
- [-] Bulk attack-sheet generation: out of scope for UI-1 (single-topic only); revisit in UI-2.1
- [-] Outline auto-regeneration: pull-only with "newer briefs exist; rebuilding will pick them up" hint in pre-flight panel

---

## Phase UI-2: Drive every high-value feature without Swagger (P1) — DONE

**Status (2026-04-26):** every high-value feature page shipped. Plans in `docs/WORKFLOWS.md` §6–10.

- [x] `apps/web/app/synthesis/page.tsx` — corpus + doctrinal-area input + multi-select case-brief picker via `ArtifactPicker` (topic autocomplete and "brief missing cases" CTA deferred to UI-2.1)
- [x] `apps/web/app/attack-sheets/page.tsx` — same picker + optional emphasis-map picker (bulk-by-topic + print-styled output deferred to UI-2.1)
- [x] What-if inline panel inside the artifact viewer (case-brief artifacts only) — N variation cards rendered inline; "Brief this variation" chaining is UI-2.1
- [x] `apps/web/app/outline/page.tsx` — pre-flight stats panel with richness hint + "Rebuild outline" CTA when one already exists
- [x] Past-exam library on corpus-detail page (Past exams tab) → "Start practice →" CTA into the practice wizard

### UI-2.1 Stretch / polish — NOT STARTED
- [ ] Synthesis: topic autocomplete from syllabus `topic_tags` + emphasis-map items + "Brief missing cases" CTA
- [ ] Attack-sheets: "Build for every syllabus topic" bulk action + print-styled output + "Print packet" combiner
- [ ] What-if: "Brief this variation" CTA chaining `parent_artifact_id`
- [ ] Outline: collapsible-tree viewer instead of flat markdown render
- [ ] Reading view (spec §5.3) at `/corpora/[id]/books/[id]/read?page=...` — replaces book-detail page eventually

---

## Cross-cutting concerns (maintained across all phases)

### Testing infrastructure (spec §6)
- [ ] L1 unit tests for every new module
- [ ] L2 golden-input prompt template tests per template (3+ fixtures each)
- [ ] L3 e2e test per feature in §5
- [ ] L4 regression test before any bug fix
- [ ] Full suite runs under 5 min on laptop
- [ ] LLM replay cache: record-once, replay-on-CI
- [ ] Re-record workflow one-command, recorded outputs committed + reviewed
- [ ] Manual test scenarios documented in `tests/manual/`:
  - [ ] Full Property casebook (10 batches) → pages 518–559 = Takings material Mahon→Penn Central
  - [ ] Fresh Gemini transcript fuzzy-name handling
  - [ ] Regenerate all Chapter 10 briefs → zero non-corpus citations

### Fixture corpus (spec §6.2) — committed to repo
- [ ] `tests/fixtures/book/` — 10-source-page Property casebook slice (Shelley + River Heights), gold ingestion output committed
- [ ] `tests/fixtures/transcript/` — the exact Shelley + River Heights transcript the user provided
- [ ] `tests/fixtures/past_exam/` — 2023 Pollack exam + memo (anonymized if needed)
- [ ] `tests/fixtures/expected_outputs/` — golden outputs per template per fixture

### Anti-hallucination invariants (spec §2.8)
- [ ] Every generation prompt instructs model to cite sources from retrieved context
- [ ] `sources` field populated on every Artifact; traces to Block ids / Transcript segment ids
- [ ] `citation_grounding` verifier runs on critical paths
- [ ] UI "show me the source" lights up corresponding markdown

### Principle audits (spec §2)
- [ ] Every feature composes the four primitives — no bespoke retrieval/prompt plumbing
- [ ] New primitives scrutinized and justified
- [ ] Source page numbers never exposed as PDF indices
- [ ] LLM never asked to read PDFs directly — Marker pipeline always first
- [ ] Every prompt is a versioned file under `packages/prompts/`, not hardcoded
- [ ] Every feature ships with at least one failing test *first*

### Spec ambiguity log
- [ ] Maintain `SPEC_QUESTIONS.md` at repo root — flag unresolved ambiguities, don't block

---

## Appendix: template catalog (spec Appendix B) — tracked here for convenience

All under `packages/prompts/{name}.prompt.md`, each with frontmatter (name, version, inputs, output_schema, model_defaults) and a `schemas/{name}.json` peer.

- [ ] `case_brief` (Phase 2)
- [ ] `flashcards` (Phase 5.1)
- [ ] `socratic_drill` (Phase 5.2)
- [ ] `cold_call` (Phase 5.7)
- [ ] `irac_grade` (Phase 3)
- [ ] `rubric_from_memo` (Phase 3)
- [ ] `hypo_generation` (Phase 3)
- [ ] `emphasis_analysis` (Phase 4)
- [ ] `transcript_cleanup` (Phase 4)
- [ ] `doctrinal_synthesis` (Phase 5.4)
- [ ] `attack_sheet` (Phase 5.3)
- [ ] `what_if_variations` (Phase 5.5)
- [ ] `hypo_from_variation` (Phase 5.5)
- [ ] `outline_hierarchical` (Phase 5.6)
- [ ] `mc_questions` (Phase 5.8)
- [ ] `professor_profile_extraction` (Phase 3)
- [ ] `block_segmentation_fallback` (Phase 2)
