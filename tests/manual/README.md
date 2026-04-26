# Manual Test Scenarios

Per spec §6.4 — not automatable, documented here for humans to run periodically.

## M1: Full Property casebook ingestion — Takings page range

1. Ingest all 10 batches of the Property casebook (drag-to-reorder upload UI).
2. Query page range 518–559.
3. **Expected:** the rendered range starts with *Pennsylvania Coal Co. v. Mahon* and ends inside *Penn Central Transportation Co. v. New York City*. Every case header renders as a `case_opinion` block; numbered notes render as `numbered_note`.

## M2: Fresh Gemini transcript — fuzzy name resolver robustness

1. User pastes (or uploads) a new Gemini auto-transcription of a Property lecture.
2. Kick off transcript ingestion with `ingest_transcript_text`.
3. **Expected:** all case-name deformations in the transcript resolve to canonical names known to the corpus. Unresolved names are surfaced in the "needs review" panel rather than silently dropped.

## M3: Chapter 10 brief regeneration — no non-corpus citations

1. Pick a chapter (e.g., Chapter 10 of the Property casebook).
2. Regenerate all case briefs for every case in that chapter.
3. Run `verify(profile=citation_grounding)` on each.
4. **Expected:** zero briefs cite a case not present in the ingested corpus.
