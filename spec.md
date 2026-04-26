ok  # Law School Study System — Design Specification

**Version:** 1.0
**Target builder:** Claude Code (agentic development)
**User:** 1L student, Cardozo School of Law (NYC), catching up on missed readings and preparing for closed-book finals
**Current course of particular interest:** Property (Professor Pollack), but design must generalize to Con Law, Criminal Law, and any other 1L doctrinal course

---

## 0. How to read this document

This spec is the single source of truth for building the system. It is written for a Claude Code instance that will execute it autonomously, but every design decision here is grounded in research about (a) how law school actually works, (b) how Professor Pollack actually grades, and (c) what properties of real casebook PDFs and real lecture transcripts we must accommodate.

**Read the spec in order.** Later sections assume definitions from earlier ones. In particular:

- §2 (Core Principles) governs every design decision in §4–§9.
- §3 (Data Model) is the vocabulary used everywhere else.
- §4 (Four Primitives) is the foundation. Every user-facing feature in §5 is an orchestration of these four.
- §6 (Testing) is **not** optional and **not** a later phase. Tests are written alongside code from Phase 1.
- §9 (Build Phases) is the execution plan. Do not skip ahead.

**When this spec is wrong or incomplete,** prefer asking the user over guessing. The user is available in an interactive chat during build. Flag unresolved ambiguities in `SPEC_QUESTIONS.md` at the repo root and move on; do not block.

---

## 1. Vision

Build a locally-run, persistent study system that lets a law student ingest a textbook once and interact with it forever: asking for page ranges, generating case briefs, running Socratic drills, writing practice IRAC answers and getting graded on them, synthesizing doctrinal arcs across cases, and — most distinctively — correlating lecture transcripts with readings to identify what the professor actually emphasized (and therefore what will actually be on the exam).

The system is used locally by a single trusted user. No multi-tenant concerns, no authentication beyond "it runs on my machine." All inputs and outputs live on the user's local disk. The user expects successive queries on the same textbook, weeks or months apart, to be fast and to reuse prior ingestion work rather than re-parsing the PDF every time.

The **north-star use case** the system must ace: the user says *"I missed the Takings lecture. Give me pages 518–559 of the Property casebook, plus the Gemini transcript of the lecture I have in my inbox. I want briefs for every case, a synthesis of how Loretto, Lucas, and Penn Central fit together, a set of Socratic drills on each case, an attack sheet I can bring into the closed-book exam, and three IRAC hypos with graded feedback."* The system should produce all of that in a single session, with the professor's actual emphasis driving what gets highlighted.

---

## 2. Core Principles

These are not aspirations. They are constraints on every decision.

### 2.1 Build on primitives, compose to features

Every user-facing feature — case brief generation, Socratic drill, IRAC grading, attack sheet, outline generator, cold-call simulator, synthesis, "what if" variations, exam emphasis mapping — must be implemented as a thin orchestration over four primitives (§4). No feature gets its own bespoke retrieval layer or prompt plumbing. When a new feature is added, the team's first question is "which primitives does this compose?" If the answer is "it needs a new primitive," that change is scrutinized: new primitives are expensive and must earn their place.

### 2.2 Local-first, persistent, idempotent

The app runs on the user's machine. Textbooks, transcripts, generated artifacts, and user-written practice answers all live in a local SQLite database plus a content-addressed file store on disk. Ingestion is expensive (minutes per book) and must happen only once per book version. Every primitive is idempotent: running the same query twice yields the same result from cache unless the user explicitly forces a regeneration.

### 2.3 Source page numbers, not PDF page numbers

**This is a first-class design constraint and a common failure mode for naive implementations.** When a law student says "pages 518–559," they mean the page numbers printed on the casebook pages — the numbers professors use on syllabi and in class. These do *not* correspond one-to-one to PDF page indices. The casebooks the user has are reflowed e-book exports where one printed casebook page spans roughly 2–3 PDF pages, and the original printed page number is preserved inline as a bare numeric line at the page break (see §3.3 for the schema and §4.1 for the extraction logic). The system must always accept and return *source* page numbers, and must never expose PDF page indices to the user.

A related consequence: **never rely on a language model reading PDFs directly as a substitute for a proper PDF-to-markdown pipeline.** Direct PDF ingestion by an LLM loses the source page numbers, mangles footnote markers, fragments case opinion boundaries, and hallucinates text in low-confidence regions. The production pipeline uses Marker (with the `--use_llm` flag for highest accuracy) to produce structured markdown with preserved page markers. The LLM enters the pipeline *after* ingestion, operating on clean text. (The author of this spec discovered this limitation first-hand by trying to read the user's casebook PDFs directly — the result was lossy enough to make clear that it must never be relied on in production.)

### 2.4 Prompt templates are data, not code

Every prompt — for case brief extraction, Socratic question generation, IRAC grading, synthesis, etc. — lives as a versioned template file under `prompts/`, loaded at runtime. When the user wants to tweak how case briefs are generated (the user already has detailed opinions about this, see Appendix B), they edit a `.prompt.md` file, not Python. The app is a thin shell around a library of prompts.

### 2.5 Atomic tests, then e2e, always

Every module in §4 and §5 ships with atomic unit tests. Every user-facing feature ships with at least one end-to-end test that runs the full pipeline on a fixture input and asserts on the output's structure and minimum content. See §6. The first Claude Code commit for any feature must include at least one failing test; the feature is not "done" until that test passes and the e2e test passes.

### 2.6 Grade against the real rubric, not vibes

Naive LLM grading of legal writing is notoriously lenient. The IRAC grader (§5.5) must not "feel" its way to a score; it must work from a pre-generated per-hypothetical rubric that enumerates required issues, required rules, expected counterarguments, and professor-specific anti-patterns (e.g., Pollack's zero-tolerance for "it depends on the jurisdiction" non-answers — see Appendix A). Grading is rubric-driven, deterministic given a rubric, and auditable: the user can always see why they got the score they got.

### 2.7 Professor voice profiles

Each professor gets a persistent voice profile (§3.7) built from their past exam memos, syllabus, and any other artifacts the user has. The profile captures: grading pet peeves, favored doctrinal framings, commonly tested doctrines, prompt conventions (voice, role, word limits), and stable traps year-to-year. Every grading run and every Socratic drill is conditioned on the relevant professor's profile. This is the generalization of the "transcript-to-emphasis mapper": the system learns each professor's priors and applies them everywhere.

### 2.8 Never hallucinate citations

Case citations, statutory references, and page numbers are always traced back to a source in the ingested corpus. If the system cannot find a verified source for a claim, it says so explicitly rather than inventing one. This is enforced at the prompt-template layer (every generation prompt instructs the model to cite sources from the retrieved context) and at the verification layer (§4.4, post-generation verification on critical paths).

---

## 3. Data Model

The vocabulary that every other section uses.

### 3.1 Corpus

The top-level container. One corpus per course — e.g., "Property – Pollack – Spring 2026." A corpus holds:

- **Books** (ingested textbooks)
- **Transcripts** (ingested lecture transcripts, text or audio-derived)
- **Syllabus** (if available, a mapping from assignment codes to readings — see §3.6)
- **Professor profile** (see §3.7)
- **Artifacts** (everything the system generates: briefs, flashcards, hypos, rubrics, practice answers, grades, synthesis documents, attack sheets, outlines)

### 3.2 Book

A book is an ingested textbook. Schema:

```
Book:
  id:                  content-hash of source PDF
  title:               str
  edition:             str or None
  authors:             list[str]
  source_pdf_path:     local filesystem path (may span multiple batch PDFs)
  ingested_at:         timestamp
  source_page_range:   (min_source_page, max_source_page)  # e.g., (1, 1423)
  toc:                 TableOfContents  # see §3.4
  pages:               list[Page]       # see §3.3
  ingestion_method:    "marker" | "marker+llm" | "pymupdf4llm"
  ingestion_version:   schema version, for re-ingest when we improve the pipeline
```

Books are *content-addressed*: the id is the hash of the source PDF. If the user ingests the same PDF twice, the second call is a no-op. If the user ingests a revised edition (different hash), it's treated as a separate book.

### 3.3 Page

The atomic addressable unit. Schema:

```
Page:
  book_id:        Book.id
  source_page:    int   # the printed page number — the one on the syllabus
  batch_pdf:     str    # which batch PDF this came from (for multi-batch books)
  pdf_page_span: (start, end)  # the range of PDF pages this source page spans
  markdown:       str   # clean markdown with footnotes, italics, headers
  raw_text:       str   # fallback plain text
  blocks:         list[Block]  # structured segmentation — see §3.5
```

**Why source_page is an int, not a string:** some casebooks use "v" or "xii" for front matter. Front matter gets negative-indexed or stored under a separate `front_matter_pages` field; the main body is always integer-indexed. When a user asks for "page xii" the resolver handles the mapping.

**Why batch_pdf and pdf_page_span are tracked:** the user uploads the casebook in batches (hit PDF size limits). The ingestion layer stitches batches into one logical book, but retains the provenance for debugging and for re-ingestion if a batch is re-uploaded.

### 3.4 TableOfContents

Extracted from the book's front matter during ingestion. Schema:

```
TocEntry:
  level:          1..6  (Part, Chapter, Section, Subsection, ...)
  title:          str
  source_page:    int
  children:       list[TocEntry]
```

Used for (a) letting the user browse a book structurally, (b) mapping syllabus entries to page ranges, and (c) giving the LLM structural context when generating briefs and outlines ("this case appears under Chapter 10, Section B: Physical Occupations").

### 3.5 Block

Inside a Page, content is segmented into typed blocks. This is the crucial structure that enables case-brief extraction, footnote handling, and note-block parsing.

```
Block:
  type:           one of: "narrative_text", "case_opinion", "case_header",
                          "numbered_note", "problem", "footnote",
                          "block_quote", "header", "figure", "table"
  source_page:    int   # the page this block starts on
  markdown:       str
  metadata:       dict  # type-specific:
                        #   case_opinion:  {court, year, citation, judge, case_name}
                        #   numbered_note: {number, has_problem: bool}
                        #   footnote:      {footnote_number, parent_block_id}
```

The `case_opinion` type is the most important. It's the unit the case-brief generator operates on: a contiguous block of court-authored text bounded by a case header (with case name, citation, year) and terminated by the next header or the first post-opinion numbered note. Segmenting this correctly from the extracted markdown is a specific, tested concern of the ingestion layer (§4.1.3).

### 3.6 Syllabus

Optional but highly valuable. If the user provides a syllabus, it maps assignment codes to page ranges:

```
SyllabusEntry:
  code:            str   # e.g., "PROP-C5" or "Class 14"
  date:            date or None
  title:           str   # e.g., "Easements I"
  page_ranges:     list[(int, int)]  # source page ranges
  cases_assigned:  list[str]   # case names for cross-reference
  topic_tags:      list[str]   # e.g., ["easements", "creation", "express"]
```

When the user says "ingest PROP-C5" it resolves to the page range; when the emphasis mapper sees the professor spend a lot of time on a case assigned in PROP-C5, it tags the emphasis event with that code.

### 3.7 Professor Profile

Built from the user's uploads of the professor's past exam memos, syllabus, and any other artifacts. Schema:

```
ProfessorProfile:
  professor_name:      str
  course:              str
  school:              str
  exam_format:         ExamFormat  # time, word limit, open/closed book, structure
  pet_peeves:          list[PetPeeve]
  favored_framings:    list[Framing]
  stable_traps:        list[Trap]
  voice_conventions:   list[VoiceConvention]
  source_artifacts:    list[paths]
```

Each item has a brief description and pointers back to the source text that justified it. Built initially by an LLM extraction pass over the uploaded memos (prompt template: `prompts/professor_profile_extraction.md`), then the user can edit the result. Re-runnable when new artifacts are added. See Appendix A for the worked profile for Pollack.

### 3.8 Transcript

An ingested lecture transcript. Schema:

```
Transcript:
  id:             content-hash
  corpus_id:      Corpus.id
  source_type:    "text" | "audio"
  source_path:    str
  lecture_date:   date or None
  topic:          str or None        # user-provided or inferred
  assignment_code: str or None       # links to SyllabusEntry
  raw_text:       str
  cleaned_text:   str                # after speaker segmentation and cleanup
  segments:       list[TranscriptSegment]
  emphasis_map:   EmphasisMap        # see §3.10, generated lazily
```

The user's actual transcripts are Gemini auto-transcriptions of audio — very rough (case names mangled, no speaker labels, sentence fragments). Ingestion includes a cleanup pass (§4.1.5) that produces `cleaned_text` and `segments`.

### 3.9 TranscriptSegment

```
TranscriptSegment:
  start_char:     int   # offset in cleaned_text
  end_char:       int
  speaker:        "professor" | "student" | "unknown"
  content:        str
  mentioned_cases:    list[CaseReference]  # resolved to canonical case names
  mentioned_rules:    list[str]            # e.g., "touch and concern"
  mentioned_concepts: list[str]            # e.g., "state action doctrine"
  sentiment_flags:    list[str]            # e.g., "disclaimed_as_not_testable",
                                           #       "returned_to_multiple_times",
                                           #       "professor_hypothetical",
                                           #       "student_question_professor_engaged"
```

The `mentioned_cases` are resolved through fuzzy matching (§4.3.4) — Gemini writes "Shelly B Kramer" and we must recognize that as "Shelley v. Kraemer."

### 3.10 EmphasisMap

Output of the transcript-to-emphasis mapper (§5.7). Not just "minutes spent per case." Schema:

```
EmphasisMap:
  transcript_id:   Transcript.id
  items:           list[EmphasisItem]

EmphasisItem:
  subject:         CaseReference | Rule | Concept
  minutes_on:      float
  return_count:    int   # number of distinct times the professor returned to it
  hypotheticals_run:  list[str]   # hypos the professor ran through on this
  disclaimed:      bool  # "I wouldn't hold you responsible for this"
  engaged_questions: int  # student questions that generated extended discussion
  exam_signal_score: float  # composite 0..1 — the headline metric
  justification:   str   # human-readable "why we think this is important"
```

`exam_signal_score` is computed from the other fields with weights that are themselves configurable (see `config/emphasis_weights.toml`), not hardcoded.

### 3.11 Artifact Types (outputs)

Every generated artifact has this envelope:

```
Artifact:
  id:                 uuid
  corpus_id:          Corpus.id
  type:               "case_brief" | "flashcard_set" | "hypo" | "rubric" |
                      "practice_answer" | "grade" | "synthesis" |
                      "attack_sheet" | "outline" | "socratic_drill" |
                      "cold_call_session" | "mc_question_set"
  created_at:         timestamp
  created_by:         "system" | "user"
  sources:            list[Source]   # Page ids, Block ids, Transcript segments
  content:            type-specific payload (JSON)
  parent_artifact_id: uuid or None   # e.g., practice_answer -> hypo
  prompt_template:    str   # which prompt template + version generated this
  llm_model:          str   # which Claude model, for reproducibility
  cost_usd:           Decimal  # total cost of all LLM calls that produced this artifact
  regenerable:        bool
```

The `sources` field is the anti-hallucination mechanism. Every claim in a case brief must trace to a Block id. Every rule in an attack sheet must trace to a Page. When the user clicks "show me the source" in the UI, it lights up the corresponding markdown.

### 3.12 CostEvent

Logged for every LLM call (generation, embedding, validation ping). Full schema and semantics are specified in §7.7.4; the short form here is:

```
CostEvent:
  id:               uuid
  timestamp:        timestamp
  session_id:       str
  model:            str
  provider:         "anthropic" | "voyage"
  input_tokens:     int
  output_tokens:    int
  total_cost_usd:   Decimal
  feature:          str
  artifact_id:      uuid or None
  cached:           bool
```

CostEvents are a first-class persisted entity, not a logging afterthought: the UI queries them directly to render the cost badge, the Cost Details panel, and the per-artifact cost attribution.

### 3.13 Credential envelope

Not persisted to SQLite — lives in the OS keyring (§7.7.2). In-memory representation:

```
Credentials:
  anthropic_api_key:   SecretStr or None
  voyage_api_key:      SecretStr or None
  last_validated_at:   timestamp or None
  last_validation_ok:  bool
```

`SecretStr` never renders in logs, error messages, or API responses except as `sk-ant-…XXXX` (last 4 chars only).

---

## 4. The Four Primitives

Every feature in §5 is a composition of these.

### 4.1 Primitive 1: Ingest

Takes raw inputs (PDFs, audio, text files), produces the persistent data structures in §3.

#### 4.1.1 Ingest a book

```
ingest_book(pdf_paths: list[Path], known_metadata: dict = {}) -> Book
```

Steps:

1. **Hash and dedupe.** Content-hash each PDF. If the combined hash matches an existing Book, return it.
2. **PDF-to-markdown.** Run Marker with `--use_llm --format markdown --extract_images` on each batch PDF. Store raw markdown output under `storage/marker_raw/{hash}.md`.
3. **Stitch batches.** Concatenate batch outputs in user-specified order into a single unified markdown document.
4. **Extract source page markers.** Scan the markdown for bare numeric lines that form a monotonically-increasing subsequence (algorithm below). This gives the mapping from position-in-markdown to source-page-number.
5. **Segment into pages.** Slice the markdown at the source page markers. Each slice becomes a Page record with the appropriate `source_page` and `pdf_page_span` (pdf_page_span is reconstructed from Marker's page metadata).
6. **Segment into blocks.** Within each Page, run block segmentation (§4.1.3) to produce typed Block records.
7. **Extract TOC.** Parse the front matter and any "Contents" headers to produce the TableOfContents structure.
8. **Persist.** Write Book, Pages, and Blocks to SQLite. Write markdown to disk.

**Source page marker extraction algorithm (the critical bit):**

```
Given: a list of (line_number, integer_value) candidates for bare numeric
lines. Extract the longest strictly-increasing subsequence starting from
a small value (1, 2, or 3) where consecutive values differ by 1 or 2
(allowing a small gap tolerance for missing markers).

Footnote numbers will appear as noise — often higher integers than the
current page cursor, and non-monotonic. Rejecting non-monotonic values
filters them out.

Validate the extracted sequence against Marker's own page metadata: the
markdown positions of extracted page markers should increase monotonically
with Marker's internal pdf-page index. If not, flag for manual review.
```

Tested against the Property casebook batch 1 (100 PDF pages), this algorithm correctly identifies source pages 1–37 with one tolerable gap (page 2 was not extracted — it's a Part-I divider page with no page number printed). The test fixtures (§6) pin this exact case.

#### 4.1.2 Ingest a transcript (text path)

```
ingest_transcript_text(text: str, metadata: dict) -> Transcript
```

Steps:

1. Hash, dedupe.
2. **Clean up.** Run the transcript cleanup prompt (prompts/transcript_cleanup.md) which: (a) segments speaker turns (professor vs student) using linguistic cues since no speaker labels are present, (b) joins sentence fragments that Gemini broke across paragraph boundaries, (c) normalizes case-name deformations against a fuzzy-matching pass over the corpus's ingested cases.
3. **Segment.** Split into TranscriptSegments at speaker-turn boundaries.
4. **Resolve mentions.** Run case-name resolution, rule extraction, concept extraction on each segment.
5. **Persist.**

#### 4.1.3 Block segmentation (inside ingest_book)

Casebook markdown is not just paragraphs. Inside a Page we find: running narrative text, case opinions (the actual court decision), numbered notes (0–12 typically, after each case), problems, footnotes, block quotes (for quoted statutes or secondary sources), figures, and tables. Correctly typing these is essential because downstream features operate on typed blocks:

- Case brief generator consumes `case_opinion` blocks.
- Numbered-note parser consumes `numbered_note` blocks for "what the professor might quiz from notes."
- Outline generator weights `header` blocks as structural anchors.

Block segmentation is rule-based (regex for headers, case headers, note numbers) with an LLM fallback for ambiguous cases. Prompt: `prompts/block_segmentation_fallback.md`.

A case opinion is detected by this pattern: a heading-like line matching `^([A-Z][A-Za-z ']+) v\. ([A-Z][A-Za-z ']+)$` or similar, followed within 3 lines by a line matching `^[A-Z][a-z]+ Court of [A-Z][a-z]+, \d{4}$` or a citation pattern (`\d+ U\.S\. \d+`, `\d+ S\.E\.2d \d+`, etc.). Once detected, the opinion block extends until the next case header, the next top-level header, or the first "Notes and Questions" / numbered-note block.

#### 4.1.4 Ingest a syllabus

```
ingest_syllabus(path: Path, book: Book) -> Syllabus
```

Syllabus formats vary (PDF, DOCX, Google Docs export). LLM-based parse into the schema in §3.6. The resolution from assignment code to Book page ranges is validated: if the syllabus says "PROP-C5: pp. 498–521" and those pages don't exist in the ingested book, we flag the discrepancy.

#### 4.1.5 Ingest audio transcript

```
ingest_transcript_audio(audio_path: Path, metadata: dict) -> Transcript
```

Audio is the secondary path. Run Whisper locally (`whisper.cpp` or `faster-whisper`) to produce text, then feed into `ingest_transcript_text`. Cache the Whisper output keyed by audio-file hash so re-running is free.

### 4.2 Primitive 2: Retrieve

Given a query, returns relevant structured content from the corpus.

```
retrieve(
  query: str | PageRange | AssignmentCode | CaseReference,
  corpus: Corpus,
  scope: RetrievalScope = AUTO,
  max_tokens: int = 20000,
) -> RetrievalResult
```

The query can be any of:

- **Page range:** "pages 518–559 of [book_id]" → returns exactly those Pages and all their Blocks.
- **Assignment code:** "PROP-C5" → resolves via Syllabus, then retrieves as page range.
- **Case reference:** "Shelley v. Kraemer" → returns the `case_opinion` block plus all subsequent `numbered_note` blocks on the same and following pages that reference it.
- **Semantic query:** "touch and concern doctrine" → embedding-based retrieval over Blocks, returning top-k with diversification.
- **Cross-source:** "anything about state action in my Property corpus" → searches Blocks in Books plus TranscriptSegments in Transcripts, returns unified ranked list.

Retrieval always returns structured results (a list of typed Blocks / Segments with their source metadata), never a flat text blob. Callers can choose to flatten for LLM input, but the structure is preserved for source attribution.

**Embedding model:** for semantic retrieval, use Voyage AI (default) via the Voyage API; configurable via `config/models.toml`. Embeddings are computed once at ingestion time, stored in SQLite with sqlite-vec, and reused. Note: Voyage is used *only* for embedding — all generation goes through Claude per the provider decision for this project.

### 4.3 Primitive 3: Generate

Run an LLM prompt template against retrieved content and produce a structured artifact.

```
generate(
  template_name: str,
  inputs: dict,
  retrieval: RetrievalResult,
  professor_profile: ProfessorProfile or None,
  model: str = "claude-opus-4-7",
  cache_key: str or None = None,
) -> Artifact
```

Every prompt template is a file under `prompts/` with a frontmatter header declaring its inputs, expected output schema, and version. Example structure:

```
---
name: case_brief
version: 1.2.0
inputs:
  - case_opinion_block: Block
  - following_notes: list[Block]
  - professor_profile: ProfessorProfile (optional)
output_schema: schemas/case_brief.json
model_defaults:
  model: claude-opus-4-7
  max_tokens: 4000
---

# System prompt

You are helping a 1L law student brief a case from their casebook...

[full prompt body, using {{variable}} templating]
```

Output is always structured: the template declares its JSON schema, the runtime validates the LLM response against that schema, and malformed responses trigger a retry (max 2 retries, then surface error to user). The user never sees raw LLM text on critical paths.

**Caching.** Every generate call has a deterministic cache key derived from (template name, template version, inputs hash, retrieval content hash, model). If a cache hit exists, return it (and emit a CostEvent with `cached=true` and `total_cost_usd=0` for bookkeeping). Regenerate only if the user explicitly requests it or any of those keys changed.

**Cost tracking.** Every non-cached generate call emits a CostEvent (§3.12, §7.7.4) with real token counts from the API response, computed dollar cost, the feature that triggered the call, and the resulting artifact's id. The feature layer does not have to opt in — the Generate primitive handles CostEvent emission automatically. The per-artifact `cost_usd` field is the sum of all CostEvents linked to that artifact.

**Pre-flight estimation.** When a feature composes many generate calls (e.g., "brief every case on these 40 pages" → ~12 briefs), the orchestrating feature queries `costs.estimator.estimate_feature_cost(feature_name, inputs)` *before* dispatching the calls. If the estimate exceeds the user's configured threshold (§7.7.5), the feature returns a `PreflightRequired` result and the UI shows the confirmation modal. The feature re-dispatches only after explicit user confirmation.

### 4.4 Primitive 4: Verify

Post-generation verification of critical-path outputs. Prevents hallucinated citations, missed issues, fabricated rules.

```
verify(
  artifact: Artifact,
  verification_profile: str,
) -> VerificationResult
```

Verification profiles include:

- **`citation_grounding`**: every citation in the artifact resolves to a Block in the corpus.
- **`rubric_coverage`**: for IRAC grading, verify every rubric item was actually scored.
- **`rule_fidelity`**: compare stated rules in a brief against the case opinion text; flag material deviations.
- **`issue_spotting_completeness`**: for a generated hypo's rubric, run a second LLM pass to spot issues from scratch and compare coverage.

Verification failures produce either (a) a soft warning attached to the artifact ("this brief cites a case the system could not find in the corpus") or (b) a hard retry of the generation step with the verifier's feedback fed back in. The choice is per-profile and configurable.

---

## 5. Features

Each feature below is one or more orchestrations of the four primitives. The purpose of each sub-section is to specify: *what the user sees*, *what primitives it calls*, *what prompt templates it uses*, *what gets persisted*.

### 5.1 Page-range ingestion and retrieval

*User says:* "Give me pages 518–559 of the Property casebook."

Orchestration:

1. `retrieve(PageRange(book_id, 518, 559))` → list of Pages and their Blocks.
2. Render in the UI as a browsable reading view, with left-side navigation by block (case opinion / notes), right-side source markdown.
3. From any block, the user can trigger case brief generation, flashcards, or Socratic drill on that specific block.

This is the foundational feature. Everything else composes on top of retrieval against a page range.

### 5.2 Case brief auto-generator

*User says:* "Brief every case on these pages." or selects a specific case.

Orchestration:

1. `retrieve` the target case opinion block plus its following numbered-note blocks.
2. `generate(template="case_brief", ...)` with those blocks and the professor profile. Output follows FIRAC+ structure: Facts, Procedural Posture, Issue, Holding, Rule, Reasoning, Significance, Where This Fits (doctrinal arc context pulled from the TOC), and — if a professor profile is available — a "Likely Emphasis" section.
3. `verify(profile=rule_fidelity)` — check that stated rules don't materially deviate from the opinion text.
4. Persist as `Artifact(type="case_brief")`.

The brief is rendered with every citation linkable back to the exact markdown block. See Appendix B for the prompt template outline and Appendix C for a worked example on *Shelley v. Kraemer*.

### 5.3 Flashcard generator

*User says:* "Flashcards for this reading."

Orchestration:

1. `retrieve` page range or specific blocks.
2. `generate(template="flashcards", ...)` with the user's flashcard formula (20–25 cards, rule questions, case-to-doctrine pairings, compare/contrast, "what is the test for X"). See Appendix B.
3. Persist as a `FlashcardSet` artifact.

Flashcards are studied via a simple spaced-repetition front-end (SM-2 algorithm; state persists per-card per-user).

### 5.4 Socratic drill mode

*User says:* "Drill me on Shelley v. Kraemer" or "Drill me on this case."

This is interactive, not one-shot. The orchestration:

1. Retrieve the case opinion, plus the professor profile's "cold_call_prep" patterns.
2. Enter a chat loop with a system prompt that casts the LLM as the professor. The LLM asks one question at a time. The user answers. The LLM reacts — pushes back on weak answers, alters facts ("what if the landlord had given written notice?"), probes for reasoning rather than recall.
3. The LLM is instructed to *never* accept "I don't know" without first giving the student one chance to reason from first principles. If the user genuinely can't answer, the LLM provides the answer *and* the reasoning path that would have gotten there.
4. Session is persisted as a `SocraticDrillSession` artifact with every turn logged. User can review after.

Template: `prompts/socratic_drill.md`. The prompt enforces Pollack-style pressure patterns pulled from his profile: pushes back on hedging ("don't just tell me 'it depends' — commit"), demands alternative arguments, flags "clearly" as an argument-weakness signal.

### 5.5 IRAC practice with graded feedback

*The riskiest feature. Specified in detail.*

Two paths:

**Path A: Grade a user answer to a real past exam.** User uploads a past exam and the accompanying grader memo. Orchestration:

1. Ingest exam and memo as special artifact types (`PastExam`, `GraderMemo`).
2. Extract a **ground-truth rubric** from the grader memo via `generate(template="rubric_from_memo", ...)`. The rubric enumerates: required issues, required rules, expected counterarguments, professor's explicitly-named pet peeves for this specific question.
3. User writes their answer in the app (rich text editor with word count).
4. `generate(template="irac_grade", ...)` with the rubric, the answer, and the professor profile. Output structure:

```
Grade:
  overall_score:       float  (calibrated 0..100)
  letter_grade:        str    (rough mapping to A/B/C — informational only)
  per_rubric_scores:   list[RubricItemScore]
  pattern_flags:       list[PatternFlag]  # "used 'clearly' without argument",
                                          # "hedged without resolution",
                                          # "stated rule without applying to facts"
  strengths:           list[str]
  gaps:                list[str]          # issues not spotted, rules not stated
  what_would_have_earned_more_points:  str
  sample_paragraph:    str   # one rewritten paragraph showing an A-level version of a B-level chunk
```

5. `verify(profile=rubric_coverage)` — every rubric item has a score and justification.
6. Persist as `Artifact(type="grade")`.

**Path B: Generate a novel hypo, then grade against a synthetic rubric.** When no past exam is in play:

1. User says "give me an exam-style hypo covering topics X, Y, Z."
2. `generate(template="hypo_generation", ...)` produces the hypo AND its rubric simultaneously — the rubric enumerates the issues the hypo was designed to test. This is key: rubric and hypo are generated together so rubric coverage is guaranteed.
3. `verify(profile=issue_spotting_completeness)` — a second LLM pass attempts to spot issues from scratch and compares against the rubric; if the verifier finds issues the rubric missed, the rubric is augmented.
4. User writes answer.
5. Grade per Path A step 4 onward.

**Grading calibration is anchored to the professor profile.** For Pollack, the grader must specifically detect and penalize: hedging without commitment, using "clearly" as argument substitution, failing to argue in the alternative when the prompt signaled ambiguity, mismatched future interest names (§Appendix A item 3), stating a rule without applying it to the specific facts, conclusions that don't match analysis ("in sum X" immediately followed by "therefore not-X"). These are not generic grading hints — they are pulled from Pollack's 2023 and 2024 exam memos, which the system has ingested.

### 5.6 Cold call simulator

Variant of Socratic drill, but targeted and adversarial. User picks a case (e.g., *Youngstown*) and "sits in the chair." The LLM simulates 10–15 minutes of cold-call questioning, escalating from facts to holding to concurrences to hypotheticals that push on the doctrine's limits. Session is timed; user responds in the chat. Post-session: automated debrief highlighting strong and weak answers, with references back to the case text.

Template: `prompts/cold_call.md`. Composes Socratic drill with a more aggressive professor persona, longer duration, and explicit time pressure.

### 5.7 Transcript-to-emphasis mapper

*The feature that turns lecture time into exam signal.*

Given a Transcript (ingested and cleaned) and optionally the corresponding assigned reading:

1. `retrieve` the reading for the lecture (via assignment_code → page range, or user-specified).
2. For each TranscriptSegment, the mentioned_cases / mentioned_rules / mentioned_concepts are already resolved (done at ingest, §4.1.2).
3. Compute per-subject (case / rule / concept) the features listed in §3.10: minutes_on, return_count, hypotheticals_run, disclaimed, engaged_questions.
4. `generate(template="emphasis_analysis", ...)` to produce justifications and the composite `exam_signal_score` for each subject.
5. Render as a ranked list: "What the professor emphasized, in order." Top items become candidates for priority in attack sheets and practice hypos.

**Emphasis is a multi-signal composite, not "minutes spent."** The weights (configurable) reward: professor returning to a topic multiple times across the lecture; running multiple hypotheticals on it; engaging deeply with student questions about it; specifically *not* disclaiming it. And penalize: explicit "you won't be responsible for this."

### 5.8 Multi-case synthesis

*User says:* "How do Loretto, Lucas, and Penn Central relate to each other?"

1. `retrieve` each case's opinion block, plus the notes around all three that discuss the relationships.
2. `generate(template="doctrinal_synthesis", ...)` producing a structured synthesis:

```
Synthesis:
  doctrinal_area:      str   # "Regulatory Takings"
  cases:               list[CaseRef]
  timeline:            list[TimelineEvent]   # chronological doctrinal evolution
  categorical_rules:   list[Rule]            # each tagged with its case
  balancing_tests:     list[Test]
  relationships:       list[Relationship]    # "Loretto created a per-se rule carve-out from Penn Central's balancing"
  modern_synthesis:    str                   # the current doctrinal map
  exam_framework:      str                   # "how to attack a takings question on exam"
  visual_diagram:      mermaid_source        # optional flowchart
```

3. `verify(citation_grounding)`.
4. Persist.

### 5.9 Attack sheet builder

*User says:* "Attack sheet for regulatory takings."

At end of a topic, auto-generate a one-page reference:

```
AttackSheet:
  topic:                str
  issue_spotting_triggers:  list[Trigger]  # keywords/fact patterns that signal the doctrine
  decision_tree:        DecisionTree       # "first ask X; if yes, ask Y; if no, ..."
  controlling_cases:    list[CaseRef]
  rules_with_elements:  list[RuleWithElements]
  exceptions:           list[Exception]
  majority_minority_splits:  list[Split]
  common_traps:         list[Trap]         # pulled from professor profile
  one_line_summaries:   list[str]          # for exam-day memorization
```

Generation uses `prompts/attack_sheet.md`. Attack sheets are export-friendly — printable, paste-into-outline-able. Property is closed-book at Cardozo under Pollack; the attack sheet is the artifact the user spends the night before the exam memorizing.

### 5.10 "What if" fact variations

*User says:* "Give me five fact variations on Shelley that would change the outcome."

1. `retrieve` the case.
2. `generate(template="what_if_variations", ...)` producing 5 variations, each with: the fact changed, the legal consequence, the doctrinal reason. Also: "this tests your understanding of ___" tag per variation.
3. Each variation can be converted into a mini-hypo with `generate(template="hypo_from_variation", ...)` on demand.

### 5.11 Outline generator

*User says:* "Build an outline for Property from all my briefs and flashcards so far."

1. `retrieve` all `case_brief` and `flashcard_set` artifacts in the corpus, plus the book's TOC.
2. `generate(template="outline_hierarchical", ...)` producing a course outline organized by the TOC hierarchy, with per-topic:
   - Rule statements
   - Controlling cases (brief-cited)
   - Policy rationales
   - Exam traps from the professor profile
   - Cross-references to other topics (e.g., "see also: covenants § running with the land")
3. Persist as a versioned outline document. Re-generable as new briefs are added.

Outlines are exported as markdown (primary), DOCX on request (via the docx skill), or printed.

### 5.12 Multiple-choice practice

Pollack's Part I is 10 MC questions. Users need MC-specific practice.

1. `generate(template="mc_questions", ...)` over a page range or topic, producing 10 MC questions, each with: stem, 4 options, correct answer, full explanation, what each distractor is wrong about, what doctrine it tests.
2. Interactive UI: user answers, sees per-question feedback.
3. Persist as `MCQuestionSet` artifact; track user's per-question answer history for review.

### 5.13 Professor profile builder and editor

1. User uploads exam memos, syllabus, any other artifacts.
2. `generate(template="professor_profile_extraction", ...)` produces a profile.
3. User reviews in a structured editor (checkbox-list of pet peeves, voice conventions, etc.) and edits.
4. Profile is persisted and referenced by every downstream generation.

### 5.14 Search and cross-reference

Global search over the corpus: books + transcripts + all generated artifacts. Results show structural context ("Chapter 10 § B" / "Class 14 transcript, minute 18"). Clicking a result opens the source with the relevant span highlighted.

---

## 6. Testing Strategy

Not optional. Not deferrable. Written alongside every feature.

### 6.1 Test layers

**L1 — Unit tests.** Per-module, pure-function tests. Prompt templates are *not* tested here (they require LLM calls); everything else is. Examples:

- `test_extract_source_page_markers` — input: a fixture markdown blob from the Property casebook with known page markers. Output: exact expected list of (position, source_page) tuples. Pinned to batch 1 of the user's actual book (§7.2).
- `test_block_segmenter_case_opinion` — input: a fragment containing a case header and opinion text. Output: correct block typing and boundaries.
- `test_fuzzy_case_name_resolver` — input: "Shelly B Kramer", "Pen Central", "River Heights v Daton". Output: "Shelley v. Kraemer", "Penn Central Transportation Co. v. New York City", "River Heights Associates L.P. v. Batten". All three appear in the user's actual transcripts.
- `test_syllabus_resolver` — input: assignment code + syllabus. Output: page range.
- `test_rubric_coverage_verifier` — input: artifact with known missing coverage. Output: correct flagged gaps.
- `test_artifact_cache_idempotence` — same inputs twice → second call is cache hit, no LLM call.

**L2 — Prompt template tests.** Each prompt template ships with at least 3 golden-input fixtures and asserted output shapes. Not full-text match (LLM outputs vary), but structural: "output must contain these fields, must cite these Block ids, must not contain these anti-patterns." Examples:

- `test_case_brief_shelley` — golden input: the *Shelley v. Kraemer* opinion + notes from the user's casebook. Output must have: non-empty Facts, Issue, Holding, Rule, Reasoning, Significance. Rule section must mention "state action" and "Fourteenth Amendment." Source citations must include the specific Block ids we expect.
- `test_irac_grade_pollack_antipatterns` — golden input: a deliberately-bad practice answer featuring "clearly," "it depends on the jurisdiction," and a mismatched future interest name. Output must flag all three pattern failures and not score the answer above a B.

**L3 — End-to-end tests.** Full pipelines against fixture inputs. Every feature in §5 has at least one. Examples:

- `e2e_ingest_book` — ingest a small-fixture PDF (10 pages), assert Pages created, page markers extracted, TOC present, blocks typed.
- `e2e_transcript_emphasis` — ingest the fixture Shelley/River Heights transcript (the one the user already provided), ingest the corresponding pages of the casebook, run emphasis mapping, assert that "Shelley v. Kraemer" and "River Heights v. Batten" are both top-3 by exam_signal_score, and that the "state action doctrine" is flagged despite not being a case name.
- `e2e_irac_grade_real_past_exam` — ingest the 2023 Pollack exam memo as a ground-truth rubric source, write a deliberately-mediocre answer to Part II Q2 (future interests), assert the grade flags the exact errors Pollack flagged in his memo (mismatched interest names, Brenda's interest not being heritable), and that the letter-grade output is in the C range.

**L4 — Regression tests.** Any bug found in the wild becomes a test before it's fixed. No exceptions.

### 6.2 Test data: the fixture corpus

A minimal corpus lives in `tests/fixtures/`:

- `tests/fixtures/book/` — a 10-source-page slice of the user's actual Property casebook covering *Shelley v. Kraemer* and *River Heights v. Batten*. Ingested once, committed to the repo as the gold ingestion output (Pages, Blocks, markdown). Re-ingestion produces identical output.
- `tests/fixtures/transcript/` — the exact Shelley + River Heights transcript the user provided, as text.
- `tests/fixtures/past_exam/` — the 2023 Pollack exam + memo (anonymized if needed).
- `tests/fixtures/expected_outputs/` — golden outputs for every prompt template on every fixture input.

This fixture corpus is what every e2e test runs against. It must be small enough to run the full test suite in under 5 minutes on a laptop. LLM calls during tests go through a **replay cache** (see §6.3) so CI does not depend on live API access.

### 6.3 LLM call replay

For tests, LLM calls are captured once (against a live API) and replayed from disk on subsequent runs. Cache key is the full (model, system prompt, user prompt, temperature) tuple. This is how test runs stay fast and deterministic without mocking away the actual LLM behavior.

When a prompt template is changed, the replay cache invalidates for that template, tests are re-recorded (a one-command workflow), and the new cached responses are committed. Code review on prompt changes includes reviewing the new recorded outputs.

### 6.4 Manual test scenarios

Not automatable but documented in `tests/manual/`:

- "Ingest the full Property casebook (all 10 batches) and verify page-range retrieval for pages 518–559 returns exactly the Takings material starting with Mahon and ending inside Penn Central."
- "Run the full workflow on a fresh Gemini transcript the user emails in — assert the fuzzy name resolver handles all deformations."
- "Regenerate all briefs for Chapter 10; assert no brief cites a case that doesn't exist in the corpus."

### 6.5 Performance targets

- Page-range retrieval: <200ms for a 50-page span.
- Case brief generation (uncached): <30s.
- Book ingestion (Marker with LLM): <5 min per 100 printed pages.
- Transcript cleanup + emphasis mapping: <2 min for a 90-minute lecture.

---

## 7. Non-Functional Requirements

### 7.1 Stack

- **Backend:** Python 3.11, FastAPI.
- **Frontend:** Next.js (App Router), TypeScript, Tailwind, shadcn/ui.
- **Database:** SQLite with sqlite-vec extension for embeddings.
- **File storage:** local filesystem, content-addressed (`storage/{hash-prefix}/{hash}`).
- **LLM:** Anthropic API (Claude Opus 4.7 as default, configurable per template).
- **Embeddings:** Voyage AI by default (Voyage is Anthropic's recommended embedding partner and is separate from the generation provider commitment; the user only committed to Claude for generation). Configurable via `config/models.toml`.
- **PDF→markdown:** Marker (`datalab-to/marker`) with `--use_llm` flag for textbooks; PyMuPDF4LLM fallback for clean simple PDFs.
- **Audio transcription (optional path):** `faster-whisper` running locally.

### 7.2 Repo layout

```
/                                   (repo root)
  spec.md                           (this file)
  README.md                         (how to run)
  SPEC_QUESTIONS.md                 (unresolved design questions raised during build)
  pyproject.toml
  pnpm-workspace.yaml
  apps/
    api/                            (FastAPI)
      src/
        primitives/
          ingest.py
          retrieve.py
          generate.py
          verify.py
        features/
          case_brief.py
          socratic_drill.py
          irac_grader.py
          emphasis_mapper.py
          ...
        costs/
          tracker.py               (CostEvent persistence + session totals)
          estimator.py             (pre-flight estimates for expensive ops)
          pricing.py               (loads config/pricing.toml)
        credentials/
          keyring_backend.py       (OS keyring + encrypted-file fallback)
          validation.py            (Anthropic + Voyage key validators)
        data/
          models.py                 (SQLModel / pydantic)
          db.py
          migrations/
      tests/
        unit/
        integration/
        e2e/
        fixtures/
    web/                            (Next.js)
      app/
        settings/
          api-keys/
          models/
          costs/
        first-run/                 (setup wall)
      components/
        CostBadge.tsx              (always-visible top bar indicator)
        PreflightCostModal.tsx
  packages/
    prompts/                        (the prompt library, shared between dev and tests)
    schemas/                        (JSON schemas for artifact types)
  storage/                          (gitignored)
    books/
    transcripts/
    artifacts/
    marker_raw/
  config/
    models.toml
    pricing.toml
    emphasis_weights.toml
    default_professor_profile.toml
```

### 7.3 Config

All configurable weights, model choices, and prompt-level knobs live in `config/*.toml`. No magic numbers in code.

### 7.4 Observability

- Structured logs (JSON) per primitive call with: primitive name, inputs hash, duration, cache-hit/miss, model used, tokens in/out.
- Per-artifact lineage: when a brief was generated, which template version, which model, which retrieval result. Viewable in a debug UI page.

### 7.5 Error handling

- All primitives return `Result[T, Error]` — no unhandled exceptions bubble to the UI.
- User-facing errors are actionable: "the syllabus references pages 498–521 but the ingested book only goes to page 423 — did you upload all batches?" not "KeyError: 498."

### 7.6 Security

Single-user local app. No authentication. The API binds to `127.0.0.1` only. No data leaves the machine except for LLM API calls to Anthropic (and optionally Voyage). The user's Anthropic API key is managed through the UI and stored in the OS keyring (see §7.7), never logged, never sent anywhere except Anthropic.

### 7.7 API key management and cost tracking

The user supplies their own Anthropic API key; the app cannot function without one. Key management and cost tracking are UI-first, persistent across sessions, and cheap to audit.

#### 7.7.1 First-run experience

On first launch, before any feature is usable, the UI shows a setup wall requesting the Anthropic API key. The user has two input paths:

- **Paste:** a text input (masked after save). Placeholder: `sk-ant-...`.
- **Upload:** a file picker that accepts a plain-text file containing the key (one line, whitespace-tolerant). This supports users who already have a `~/.anthropic/api_key` file or whose org distributes keys as files.

After input, the app validates the key by calling `GET https://api.anthropic.com/v1/models` (cheap, no tokens consumed). The UI shows one of three states:

- **Valid** — key accepted, stored, setup complete.
- **Invalid** — key rejected by Anthropic (401). UI prompts re-entry with the exact error message.
- **Unreachable** — network error. UI offers retry and a "save anyway, validate later" escape hatch.

No feature that requires an LLM call is enabled until a valid key is stored.

#### 7.7.2 Storage

Keys are stored via the OS keyring, using the `keyring` Python library:

- **macOS:** Keychain.
- **Windows:** Credential Manager.
- **Linux:** Secret Service (GNOME Keyring, KWallet).

The keyring entry is `anthropic-api-key` under service name `law-school-study-system`. If keyring access fails (headless Linux without Secret Service running, or a locked-down environment), the app falls back to an encrypted file at `~/.config/law-school-study-system/credentials.enc`, encrypted with a machine-specific key derived from the user's home directory and hostname (enough to prevent casual snooping by other users on a shared machine; not a high-security solution — we are explicit about this in the UI).

The key is read at app startup and held in process memory. It is never written to logs, never included in error reports, and never sent to any endpoint other than Anthropic's API.

#### 7.7.3 Settings page

Exposed in the UI under Settings → API Key. The user can:

- View the last 4 characters of the stored key (never the full key).
- Rotate the key (replace with a new one, re-validated).
- Clear the key (requires confirmation — all LLM features disable immediately, no in-flight calls continue).
- Test the current key (round-trip to `/v1/models`).

The Voyage embedding key, if the user opts into semantic retrieval, is managed through the same UI in a second field with identical semantics. Voyage is optional; if no Voyage key is provided, semantic retrieval falls back to BM25 lexical search and a visible badge indicates reduced retrieval quality.

#### 7.7.4 Cost tracking — the data model

Every LLM call the app makes is logged as a `CostEvent` in SQLite:

```
CostEvent:
  id:               uuid
  timestamp:        timestamp
  session_id:       str   # refreshes on app launch; grouping key for per-session totals
  model:            str   # e.g., "claude-opus-4-7"
  provider:         "anthropic" | "voyage"
  input_tokens:     int
  output_tokens:    int
  input_cost_usd:   Decimal
  output_cost_usd:  Decimal
  total_cost_usd:   Decimal
  feature:          str   # e.g., "case_brief", "irac_grade", "emphasis_analysis"
  artifact_id:      uuid or None   # if this call produced a persisted Artifact
  cached:           bool   # true if this was a replay-cache hit with $0 cost
```

Cost is computed from the token counts returned in the API response, multiplied by per-model rates from `config/pricing.toml`:

```toml
[anthropic.claude-opus-4-7]
input_per_mtok  = 15.00
output_per_mtok = 75.00

[anthropic.claude-sonnet-4-6]
input_per_mtok  = 3.00
output_per_mtok = 15.00

[anthropic.claude-haiku-4-5]
input_per_mtok  = 1.00
output_per_mtok = 5.00

[voyage.voyage-3]
input_per_mtok  = 0.06
output_per_mtok = 0.00   # embeddings are input-only
```

Pricing is read at startup. If the file is missing or malformed, the app surfaces a warning and defaults to conservative (high) estimates to avoid under-reporting.

#### 7.7.5 Cost tracking — the UI

Three surfaces:

**A. The always-visible cost badge.** A persistent indicator in the top bar shows the current session's cumulative cost: `$0.47 this session (142K tokens)`. Clicking it opens the Cost Details panel.

**B. Cost Details panel.** A full-page view with:

- Current session total + token breakdown.
- Lifetime total (all sessions, since install).
- Per-day chart of cost over the last 30 days.
- Per-feature breakdown (how much went to briefs vs grading vs emphasis mapping).
- A searchable/filterable log of every CostEvent.
- An "Export CSV" button.
- A "Reset session counter" button (does not delete history; just starts a new session grouping).

**C. Pre-flight cost estimates.** Before any expensive operation (see list below), the UI shows a modal with the estimated cost range and requires explicit confirmation. Default threshold: any single user action estimated to cost >$0.50 triggers the modal. Threshold is configurable in Settings.

Operations that trigger pre-flight estimates:

- **Book ingestion.** Estimate = (pages × avg_tokens_per_page × marker_llm_call_factor × rate). For the user's Property casebook (~1400 source pages), a full ingestion at current Opus pricing is roughly $8–$15 — absolutely a "confirm first" event.
- **Bulk brief generation.** "Brief every case on pages 518–559" over a 40-page span is ~12 cases, each ~$0.20 → ~$2.40.
- **Outline regeneration across the entire corpus.**
- **Rubric generation from a past exam memo.**

Pre-flight estimates are explicitly approximate. The UI labels them "estimated" and shows a range (e.g., `~$2.40 (±30%)`). The estimate function lives at `apps/api/src/costs/estimator.py` and is tested with golden-input fixtures.

**D. Budget alerts (optional).** In Settings, the user can set a monthly spending cap (default: off). When 80% of the cap is reached, the cost badge turns amber and shows the remaining budget; at 100%, the app disables new non-cached LLM calls and shows a blocking modal explaining why, with a one-click "raise the cap" action. This is opt-in because some users will not want any friction on study sessions.

#### 7.7.6 Model selection and cost-tier defaults

Different features default to different Claude models based on the cost-quality tradeoff:

| Feature | Default model | Rationale |
|---|---|---|
| Case brief generation | claude-opus-4-7 | Correctness of stated rule is non-negotiable |
| IRAC grading | claude-opus-4-7 | Riskiest feature; must align with professor rubric |
| Socratic drill / cold call | claude-opus-4-7 | Interactive reasoning quality matters |
| Rubric extraction from memos | claude-opus-4-7 | One-time, high-stakes |
| Doctrinal synthesis | claude-opus-4-7 | Multi-case reasoning |
| Flashcard generation | claude-sonnet-4-6 | High volume, structured output, lower stakes |
| MC question generation | claude-sonnet-4-6 | Same as flashcards |
| Block segmentation fallback | claude-haiku-4-5 | Structural classification, cheap |
| Transcript cleanup (speaker seg) | claude-haiku-4-5 | Mechanical, high volume |
| Fuzzy case-name resolution | claude-haiku-4-5 | Small context, simple task |

Every per-feature default is overridable by the user in Settings → Models, with a UI hint showing the expected cost impact of upgrading or downgrading.

#### 7.7.7 Cost events for Voyage embeddings

Voyage calls are logged as CostEvents identically (provider="voyage"). Embedding costs are tiny (embeddings for an entire ingested casebook are typically under $0.05) so they are rolled up into the session total without special handling, but are visible per-feature in the Cost Details panel.

#### 7.7.8 Tests

- `test_keyring_roundtrip` — store a fake key, read it back, clear it.
- `test_key_validation_live` — (gated, runs only with a `TEST_ANTHROPIC_KEY` env var; normally skipped) validates against Anthropic.
- `test_key_validation_mocked` — mocks `/v1/models` to verify the valid / invalid / unreachable branches.
- `test_cost_event_recording` — after a mocked LLM call, a CostEvent is persisted with correct token counts and computed cost.
- `test_preflight_estimator_book_ingestion` — for a fixture 100-page book, estimator returns a cost in the expected range.
- `test_budget_cap_triggers_block` — at 100% of cap, next LLM call is blocked with the expected error.
- `test_pricing_config_missing_fallback` — absent or malformed `pricing.toml`, app surfaces warning and uses conservative defaults.

---

## 8. The Professor Profile for Pollack (worked example)

See Appendix A. This is referenced by §3.7 and §5.5. It demonstrates what a populated profile looks like.

---

## 9. Build Phases

Execute in order. Each phase delivers working end-to-end functionality and a test suite.

### Phase 1: The spine (week 1)

Goal: ingest a book, retrieve a page range, display it in the web UI. API key management works. No generation LLM calls yet, but the key is configured so Phase 2 can begin immediately.

- Repo scaffolding, pyproject, package layout.
- Data model: Corpus, Book, Page, Block, CostEvent (schema + SQLite migrations).
- **API key management (§7.7):** first-run wall, keyring storage, Anthropic validation via `/v1/models`, Settings → API Key page. Without a stored key, Phase 2+ features are inert but Phase 1 still works.
- **Cost tracking skeleton:** CostEvent persistence, session-id generation, top-bar cost badge (shows `$0.00` in Phase 1), Cost Details panel UI. Pricing config loaded from `config/pricing.toml`.
- Ingest primitive (§4.1.1) — Marker integration, source-page-marker extraction algorithm, block segmentation (rule-based only for now). LLM fallback for segmentation is stubbed.
- Retrieve primitive (§4.2) — page-range and case-reference modes only. Semantic retrieval stubbed.
- FastAPI endpoints for ingest, retrieve, credentials, costs.
- Next.js UI: first-run key wall, upload a book, see ingestion progress, browse a page range, see typed blocks rendered, cost badge in top bar, Settings pages.
- **Tests:** unit tests for page marker extraction (pinned to the user's batch 1 of the Property casebook, §6.2), unit tests for block segmenter, keyring roundtrip, cost event persistence, pricing config loading, e2e test for ingest + retrieve.

**Phase 1 exit criterion:** user launches the app with no key configured, sees the setup wall, pastes or uploads their key, gets validated, lands on the app home, uploads a PDF, waits for ingestion, and sees "pages 518–559" rendered correctly in the browser with case opinions and notes distinctly styled. Cost badge shows $0.00 (no generation calls yet; Marker's optional LLM passes are tracked as CostEvents with their actual cost).

### Phase 2: Generation and the first real feature (week 2)

Goal: case briefs work end-to-end. Cost tracking goes live.

- Generate primitive (§4.3) — prompt template loader, LLM call, structured output validation, cache. **Every call emits a CostEvent with real token counts.**
- Verify primitive (§4.4) — citation_grounding and rule_fidelity profiles.
- **Pre-flight estimator (§7.7.5):** `costs/estimator.py` with per-feature estimates. `PreflightCostModal` wired up to any operation estimated above the configured threshold.
- Prompt templates: `case_brief`, and the supporting `block_segmentation_fallback` for the ingest edge cases not handled by rules.
- Case brief feature (§5.2): UI to trigger brief generation on a case, view it, see sources highlighted, see the per-artifact cost attached.
- **Tests:** golden-input test for case briefs on *Shelley v. Kraemer* and *River Heights v. Batten*. LLM replay cache. Pre-flight estimator test with fixture inputs.

**Phase 2 exit criterion:** user can click a case in the page-range view, get a brief in under 30s, and inspect every claim's source.

### Phase 3: Professor profile + IRAC grading (week 3–4)

Goal: the grading feature — the riskiest and highest-value one — works.

- Professor profile ingestion (§5.13) from exam memos.
- Past exam ingestion (special artifact type).
- Rubric extraction from memos (`rubric_from_memo` template).
- IRAC grading (`irac_grade` template).
- IRAC practice UI: write answer, submit, see graded feedback with rubric breakdown and pattern flags.
- **Tests:** end-to-end test grading a deliberately-bad answer to a 2023 Pollack exam question, asserting the grader catches every anti-pattern Pollack flagged in his memo.

**Phase 3 exit criterion:** user writes a practice answer to a real past exam, gets back a grade that aligns with how Pollack actually graded similar answers.

### Phase 4: Transcript ingestion and emphasis mapping (week 5)

Goal: lectures inform study priorities.

- Transcript ingestion (text + Whisper audio).
- Transcript cleanup prompt (speaker segmentation, fragment joining).
- Case name fuzzy resolver (tested against the Gemini-mangled transcript the user provided).
- Emphasis mapper (§5.7).
- UI: upload a transcript, link to an assignment code, view the ranked emphasis output.

**Phase 4 exit criterion:** user uploads the Shelley/River Heights Gemini transcript, the system correctly resolves mangled case names, produces an emphasis ranking with justifications, and highlights that the professor ran multiple hypotheticals on the change-of-conditions doctrine.

### Phase 5: Remaining features (week 6–7)

In priority order (can be parallelized if multiple agents):

1. Flashcards + spaced repetition (§5.3).
2. Socratic drill mode (§5.4).
3. Attack sheet builder (§5.9).
4. Multi-case synthesis (§5.8).
5. "What if" variations (§5.10).
6. Outline generator (§5.11).
7. Cold call simulator (§5.6).
8. MC question practice (§5.12).
9. Global search (§5.14).

Each is a prompt template + a thin orchestration over the primitives. Each ships with its golden-input test.

### Phase 6: Polish (week 8)

- UI refinements, mobile view (not required but pleasant), keyboard shortcuts.
- Performance pass — make sure §6.5 targets are hit.
- Backup/export — user can dump their entire corpus to a portable archive.

---

## Appendix A: Professor Profile — Pollack, Property, Cardozo

Derived from the 2023 and 2024 exam memos and the 2025 exam prompt. This is what a populated `ProfessorProfile` looks like (trimmed; fields reference source lines in the ingested memos).

```
professor_name: "Pollack"   # first name not known; user to fill in
course:         "Property"
school:         "Benjamin N. Cardozo School of Law"

exam_format:
  duration:      5 hours
  word_limit:    4000
  open_book:     false
  structure:
    - part:   I
      weight: 10
      type:   multiple_choice
      count:  10
    - part:   II-IV
      weight: 30 each
      type:   issue_spotter_essay
      density: "7-10 distinct issues per fact pattern"
  prompt_conventions:
    - "Always ends with: 'If there are any factual ambiguities or unanswered
       legal questions that would affect your analysis of these issues,
       explain what they are and how they would affect that analysis.'"
    - "Prompt role varies per part: law clerk memo (neutral), client's lawyer
       (advocate), brief (advocate in persona). Wrong voice = lost points."

pet_peeves:
  - name:    "hedge_without_resolution"
    pattern: "'It depends on the jurisdiction' / 'it's ultimately a fact
              question' / 'the court would need to evaluate the facts'"
    severity: high
    quote:   "'Well, Client, it all depends on the facts' is not the kind of
              analysis that anyone will pay you very much to provide."
    source:  "2023 memo p.2, 2024 memo pp.4-5"
  - name:    "clearly_as_argument_substitution"
    pattern: "Using 'clearly' to avoid making an argument"
    severity: high
    quote:   "The word 'clearly' in a brief is a neon sign that a lawyer has
              no real argument and probably deserves to lose."
    source:  "2024 memo p.4"
  - name:    "mismatched_future_interests"
    pattern: "Inventing interests not on the numerus clausus menu; pairing
              interests that can't legally coexist (e.g., 'remainder vested
              subject to open' + 'contingent remainder')"
    severity: high
    must_know_pairings:
      - "contingent remainder ↔ alternate contingent remainder OR reversion"
      - "vested subject to open ↔ (nothing — no other future interest)"
      - "indefeasibly vested ↔ (nothing)"
      - "vested subject to complete divestment ↔ executory interest"
    source:  "2023 memo p.3, 2024 memo p.5"
  - name:    "conclusion_mismatches_analysis"
    pattern: "'In sum X. Therefore not-X.'"
    severity: medium
    source:  "2023 memo p.2"
  - name:    "rule_recited_not_applied"
    pattern: "Stating a rule without tying it to the specific facts of the hypo"
    severity: high
    source:  "2023 memo p.1 ('legal analysis always requires you to apply
              that rule to these facts')"
  - name:    "read_the_prompt"
    pattern: "Answering 'should X' when asked 'can X'; writing 'Harriet could
              argue' when instructed to write as a detached law clerk"
    severity: high
    source:  "2023 memo p.1, 2024 memo p.11"
  - name:    "ny_adverse_possession_reasonable_basis"
    pattern: "Conflating 'the claimant thought they owned it' with 'the
              claimant had a reasonable basis for thinking they owned it'"
    severity: medium
    source:  "Flagged in both 2023 and 2024 memos as a year-over-year repeat
              error"
  - name:    "no_arguing_in_the_alternative"
    pattern: "Committing to one interpretation and offering no backup when the
              prompt signaled ambiguity"
    severity: high
    source:  "2024 memo pp.5-6: 'You've got to get used to arguing in the
              alternative.'"

favored_framings:
  - "Numerus clausus — the menu of estates is closed"
  - "Penn Central three-factor balancing as the default for regulatory takings"
  - "Per se takings as carve-outs (Loretto physical occupation; Lucas
     total wipeout)"
  - "Order of operations: procedural validity before substantive reasonableness;
     nuisance determination before remedy"

commonly_tested:
  - "RAP on executory interests / contingent remainders"
  - "Recording acts (race / notice / race-notice distinctions) + Shelter Rule"
  - "Covenants running at law vs. in equity"
  - "Easement creation methods (express, implied, prescription, estoppel)"
  - "Landlord-tenant: assignment vs sublease, Kendall, duty to mitigate,
     quiet enjoyment vs habitability"
  - "Takings: Loretto / Lucas / Penn Central"
  - "Co-ownership: joint tenancy severance, lien vs title theory"
  - "Zoning: variances, nonconforming uses, special exceptions"

stable_traps:
  - name:    "deed_language_FSSEL_vs_FSD"
    desc:    "Durational language ('so long as') in a conveyance to
              third-party future-interest holder → FSSEL, not FSD."
  - name:    "shelter_rule_reconstruction"
    desc:    "Shelter Rule does not let you 'mix and match' winning halves
              across buyers. Grantee inherits grantor's whole position."
  - name:    "changed_conditions_requires_both_internal_and_external"
    desc:    "Under River Heights, changed-conditions doctrine requires
              radical change INSIDE the restricted area as well as outside."
```

## Appendix B: Prompt Template Catalog (excerpt)

Every template lives at `packages/prompts/{name}.prompt.md`. Format:

```
---
name:        case_brief
version:     1.2.0
inputs:
  case_opinion_block:  Block
  following_notes:     list[Block]
  professor_profile:   ProfessorProfile (optional)
  book_toc_context:    TocEntry (optional)
output_schema:         schemas/case_brief.json
model_defaults:
  model: claude-opus-4-7
  max_tokens: 4000
  temperature: 0.2
---

# System

You are helping a 1L law student brief a case. Your brief is the unit
they will memorize for a closed-book exam and the unit they will refer
to in class when cold-called. Correctness of the stated rule is
non-negotiable. Brevity with completeness is the goal.

# User

Case opinion:
{{ case_opinion_block.markdown }}

Relevant notes from the casebook:
{{#each following_notes}}
— Note {{number}} (source page {{source_page}}):
{{markdown}}
{{/each}}

{{#if book_toc_context}}
This case appears in the casebook under:
{{book_toc_context.breadcrumb}}
{{/if}}

{{#if professor_profile}}
Professor teaching this case: {{professor_profile.professor_name}}.
Framings this professor favors:
{{#each professor_profile.favored_framings}}
— {{this}}
{{/each}}
{{/if}}

Produce a case brief as JSON matching the provided schema. Every
material claim must cite the source page it came from. Do not invent
facts. If the opinion as given is incomplete, say so in the
limitations field rather than extrapolating.
```

Full catalog (template names only):

- `case_brief`
- `flashcards`
- `socratic_drill`
- `cold_call`
- `irac_grade`
- `rubric_from_memo`
- `hypo_generation`
- `emphasis_analysis`
- `transcript_cleanup`
- `doctrinal_synthesis`
- `attack_sheet`
- `what_if_variations`
- `hypo_from_variation`
- `outline_hierarchical`
- `mc_questions`
- `professor_profile_extraction`
- `block_segmentation_fallback`

## Appendix C: Worked Example — the North-Star Use Case End-to-End

The user has ingested the Property casebook (10 batch uploads, ~1400 source pages total) and the Pollack 2023 and 2024 exam memos (professor profile built). The user says:

> "I missed the Takings lecture. Pull pages 518–559 of the Property casebook, and I also have the Gemini transcript of the lecture in my inbox — let me paste it. Give me briefs for every case, a synthesis of how Loretto, Lucas, and Penn Central fit together, Socratic drills on each case, an attack sheet I can bring into the closed-book exam (well, memorize for it), and three IRAC hypos with graded feedback."

Orchestration:

1. `retrieve(PageRange(property_casebook, 518, 559))` → 42 source pages, segmented into: 4 case opinion blocks (*Mahon*, *Penn Central*, *Loretto*, *Lucas*, with *Tahoe-Sierra* excerpts in notes), ~30 numbered-note blocks, running commentary.
2. User pastes the Gemini transcript into the transcript-ingest UI. `ingest_transcript_text` runs cleanup, fuzzy-resolves "Pen Central" → "Penn Central Transportation Co. v. New York City" etc., persists.
3. Emphasis mapper runs automatically. Result: Penn Central three-factor balancing is the top-emphasized item (professor returned to it 4 times and ran 3 hypotheticals on investment-backed expectations); Loretto per-se rule is second; *Tahoe-Sierra* is disclaimed ("I won't hold you responsible for the temporal dimension stuff"), so it's down-ranked.
4. Case briefs generated for each of the 4 cases. The *Penn Central* brief, per emphasis, gets an expanded "Likely Emphasis" section flagging investment-backed expectations and the parcel-as-a-whole question.
5. `doctrinal_synthesis` produces the arc: Mahon's "too far" standard → Penn Central's formalization into a three-factor balancing test → Loretto's per-se carve-out for physical occupation → Lucas's per-se carve-out for total wipeout → the background-principles limit. Visual mermaid diagram.
6. `attack_sheet` produced: issue spotters ("permanent physical presence" → Loretto; "total deprivation of economic use" → Lucas; else → Penn Central), decision tree, rules with elements, traps from Pollack's profile (including "don't forget nuisance-abatement exception per Hadacheck").
7. Socratic drill sessions queued up, one per case. User runs the Loretto drill first: 8 turns, the LLM pushes back when the user gives a hedging answer ("don't tell me 'it depends on the jurisdiction' — we're in SCOTUS and *Loretto* just made this a per-se rule; commit"), escalates to hypos ("what if the occupation were 0.1 square inches of roof space?" — yes, still a taking).
8. Three IRAC hypos generated, each with rubric pre-built. User writes their answer to hypo 2 in the app. Grader produces per-rubric scores and pattern flags: "you stated the Penn Central three factors but didn't apply them to the specific facts of the vacant-lot scenario — Pollack specifically penalizes this." Score: B-. Sample paragraph showing the A version included.

Every artifact is persisted, tagged to this session, and reviewable later. When the user comes back three weeks before the exam, everything is still there and the spaced-repetition flashcards are ready.

---

*End of spec.*
